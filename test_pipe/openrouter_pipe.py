"""
title: Тест OpenRouter (со списанием)
author: credits-test
version: 0.1.0
required_open_webui_version: 0.5.0
requirements: httpx
"""

# =============================================================================
# Тестовая функция Open WebUI (Pipe) для сквозной проверки списания кредитов.
#
# Поток:
#   1. Берёт сообщение пользователя, шлёт его на /chat/completions (по умолчанию
#      локальный fake-OpenRouter, но в Valves можно поставить api.openrouter.ai).
#   2. Из ответа берёт usage.total_tokens.
#   3. Сообщает об использовании нашему серверу списания кредитов через
#      credit_reporter.report_usage(...) — НЕ блокирует и НЕ роняет функцию.
#
# ВАЖНО: credit_reporter.py должен лежать рядом (в той же папке). При установке
# в Open WebUI оба файла (openrouter_pipe.py + credit_reporter.py) загружаются
# как одна функция, либо credit_reporter кладётся в PYTHONPATH контейнера.
# =============================================================================

import copy
import sys
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from credit_reporter import report_usage, estimate_tokens, extract_cost_usd


class Pipe:
    """Прокси к OpenAI-совместимому API (OpenRouter / fake) со списанием кредитов."""

    class Valves(BaseModel):
        # --- подключение к модели (OpenRouter / любой OpenAI-совместимый эндпоинт) ---
        API_BASE_URL: str = Field(
            default="http://localhost:4030/v1",
            description="Базовый URL API (OpenAI-совместимый). "
            "Для боевого OpenRouter: https://openrouter.ai/api/v1",
        )
        API_KEY: str = Field(
            default="test-key",
            description="API-ключ модели (Authorization: Bearer).",
            json_schema_extra={"input": {"type": "password"}},
        )
        MODEL_NAME: str = Field(
            default="test-model",
            description="Имя модели. ВАЖНО: оно же используется как ключ курса "
            "на сервере списания (вкладка Курсы).",
        )
        REQUEST_TIMEOUT: int = Field(
            default=60,
            description="Таймаут запроса к модели, сек.",
        )

        # --- административный вентиль: доступ на наш сервер списания кредитов ---
        CREDITS_SERVER_URL: str = Field(
            default="http://localhost:4010",
            description="Адрес сервера списания кредитов.",
        )
        CREDITS_API_KEY: str = Field(
            default="",
            description="Ключ доступа на сервер списания (Bearer). Должен совпадать "
            "с CREDITS_API_KEY в .env сервера. Без него отчёт не пройдёт.",
            json_schema_extra={"input": {"type": "password"}},
        )

        ENABLE_LOGGING: bool = Field(
            default=True,
            description="Писать лог в консоль Open WebUI (docker logs).",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ----------------------------- helpers ----------------------------------
    def _log(self, msg: str) -> None:
        if not self.valves.ENABLE_LOGGING:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"[pipe-test] {ts} {msg}", file=sys.stdout, flush=True)

    @staticmethod
    def _last_user_text(messages) -> str:
        for m in reversed(messages or []):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                return m["content"]
        return ""

    # ----------------------------- main -------------------------------------
    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__=None,
    ):
        user_email = (__user__ or {}).get("email", "") or "unknown@example.com"
        user_id = str((__user__ or {}).get("id") or "")
        prompt = self._last_user_text(body.get("messages", []))
        self._log(
            f"REQUEST user={user_email!r} uid={user_id!r} "
            f"model={self.valves.MODEL_NAME!r} prompt={prompt[:80]!r}"
        )

        # Жизненный цикл вызова модели: фиксируем старт до запроса, финиш —
        # в каждой точке возврата (успех и любая ошибка). Рапортуем на сервер
        # списания В ОБОИХ случаях, чтобы devbim.com фиксировал все попытки.
        request_date = datetime.now(timezone.utc).isoformat()

        def _report(
            *,
            tokens: int,
            prompt_tokens,
            completion_tokens,
            cost_usd,
            is_success: bool,
            error_message: str | None = None,
        ) -> None:
            """Отправить отчёт на сервер списания (fire-and-forget)."""
            import credit_reporter as cr
            cr.SERVER_URL = self.valves.CREDITS_SERVER_URL.rstrip("/")
            cr.API_KEY = self.valves.CREDITS_API_KEY
            response_date = datetime.now(timezone.utc).isoformat()
            try:
                report_usage(
                    email=user_email,
                    function="Тест OpenRouter (со списанием)",
                    model=self.valves.MODEL_NAME,
                    tokens=tokens,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                    user_id=user_id,
                    request_date=request_date,
                    response_date=response_date,
                    is_success=is_success,
                    error_message=error_message,
                )
                self._log(
                    f"reported to credits server: tokens={tokens} "
                    f"(in={prompt_tokens}/out={completion_tokens}) "
                    f"cost={cost_usd if cost_usd is not None else '—'} "
                    f"success={is_success} user={user_email!r}"
                )
            except Exception as e:
                # Сниппет глушит свои ошибки сам, но подстрахуемся.
                self._log(f"WARN report_usage raised (ignored): {e}")

        payload = copy.deepcopy(body)
        payload["model"] = self.valves.MODEL_NAME
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.valves.API_KEY}",
        }
        url = f"{self.valves.API_BASE_URL.rstrip('/')}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self.valves.REQUEST_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except Exception as e:
            self._log(f"ERROR request to model failed: {e}")
            _report(tokens=0, prompt_tokens=None, completion_tokens=None,
                    cost_usd=None, is_success=False, error_message=str(e))
            return f"❌ Не удалось подключиться к модели: {e}"

        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            self._log(f"ERROR model {err}")
            _report(tokens=0, prompt_tokens=None, completion_tokens=None,
                    cost_usd=None, is_success=False, error_message=err)
            return f"❌ Модель вернула {err}"

        data = resp.json()
        usage = data.get("usage", {}) or {}
        tokens = int(usage.get("total_tokens") or 0)
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        cost_usd = extract_cost_usd(usage)

        # Извлечь текст ответа (для показа пользователю).
        # content может быть None при tool-calling или у reasoning-моделях.
        try:
            choice = data["choices"][0] or {}
        except (KeyError, IndexError, TypeError):
            choice = {}
        msg = choice.get("message") or {}
        answer = msg.get("content")
        if not isinstance(answer, str):
            answer = msg.get("reasoning_content")
        if not isinstance(answer, str):
            tool_calls = msg.get("tool_calls") or []
            finish_reason = choice.get("finish_reason") or ""
            if tool_calls or finish_reason == "tool_calls":
                names = []
                for tc in tool_calls:
                    try:
                        names.append(tc["function"]["name"])
                    except (KeyError, TypeError):
                        pass
                names_str = ", ".join(names) if names else "(имена недоступны)"
                self._log(f"WARN tool_calls without text content: {names_str}")
                answer = (
                    "ℹ️ Модель решила обратиться к инструментам "
                    f"({names_str}), но эта функция-прокси не выполняет "
                    "tool-calling — поэтому текстового ответа нет."
                )
            else:
                answer = "⚠️ Модель не вернула текстовый ответ."

        # Если модель не вернула usage — грубо оценим по сумме prompt+answer.
        if not tokens:
            tokens = estimate_tokens(prompt + " " + answer)
            self._log(f"no usage in response, estimated tokens={tokens}")

        # --- сообщить на сервер списания кредитов (fire-and-forget, успех) ---
        _report(
            tokens=tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            is_success=True,
        )

        self._log(f"DONE tokens={tokens} answer_chars={len(answer)}")
        return answer
