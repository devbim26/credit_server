"""
title: Агент со списанием кредитов
author: credits-system
version: 0.2.2
required_open_webui_version: 0.5.0
requirements: httpx
"""

# =============================================================================
# Функция Open WebUI (Pipe) со встроенным списанием кредитов.
#
# ОДИН автономный файл — ничего дополнительно ставить не нужно. Логика учёта
# (бывший credit_reporter.py) встроена прямо сюда, т.к. Open WebUI грузит
# функцию как один файл и отдельные модули в PYTHONPATH не попадают.
#
# Что делает функция:
#   1. Берёт сообщение пользователя, шлёт на /chat/completions (по умолчанию
#      локальный fake-OpenRouter; для боевого — поставьте api.openrouter.ai).
#   2. Из ответа берёт usage {prompt_tokens, completion_tokens, total_tokens}.
#   3. Сообщает об использовании серверу списания кредитов — НЕ блокирует и
#      НЕ роняет функцию при сбое сервера (ошибка только пишется в лог).
#
# ВЕНТИЛИ (Valves, настраиваются в ⚙ Open WebUI):
#   - API_BASE_URL, API_KEY, MODEL_NAME, REQUEST_TIMEOUT  — подключение к модели;
#   - CREDITS_SERVER_URL, CREDITS_API_KEY                 — административный
#                                                           вентиль доступа к
#                                                           нашему серверу.
#   - SHOW_CREDITS_LOG_IN_CHAT                            — показывать лог обмена
#                                                           с сервером списания
#                                                           (→ запрос / ← ответ)
#                                                           блоком прямо в чате.
# =============================================================================

import asyncio
import copy
import json
import sys
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Встроенная логика учёта (бывший credit_reporter.py).
# Внешняя точка: report_usage(...) — fire-and-forget, безопасная.
# --------------------------------------------------------------------------- #
_CREDITS_TIMEOUT = 5.0  # сек


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[credits] {ts} {msg}", file=sys.stdout, flush=True)


def _estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов: ~4 символа на токен. Только при отсутствии usage."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_cost_usd(usage: dict | None) -> float | None:
    """Извлечь стоимость в USD из usage ответа провайдера.

    OpenRouter возвращает usage.cost как объект {"total_cost": 0.000123}
    (а в старых версиях — как число). z.ai/GLM поля cost не возвращают —
    тогда вернём None, и сервер посчитает $ по тарифам $/млн токенов.
    """
    if not usage:
        return None
    cost_val = usage.get("cost")
    if isinstance(cost_val, dict):
        c = cost_val.get("total_cost")
        if c is None:
            c = cost_val.get("total")
        return float(c) if isinstance(c, (int, float)) and c > 0 else None
    if isinstance(cost_val, (int, float)) and cost_val > 0:
        return float(cost_val)
    return None


async def _credits_send(
    server_url: str,
    api_key: str,
    *,
    email: str,
    function: str,
    model: str,
    tokens: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    user_id: str = "",
    request_date: str | None = None,
    response_date: str | None = None,
    is_success: bool = True,
    error_message: str | None = None,
) -> dict:
    """Реальная HTTP-отправка на сервер списания. Никогда не падает наружу.

    Возвращает {"ok": bool, "lines": [строки]} — lines пайп показывает
    блоком в чате (вентиль SHOW_CREDITS_LOG_IN_CHAT).
    """
    server_url = (server_url or "").rstrip("/")
    if not server_url:
        _log("CREDITS_SERVER_URL не задан — пропуск отчёта")
        return {"ok": False,
                "lines": ["✗ CREDITS_SERVER_URL не задан — отчёт не отправлен"]}

    payload = {
        "email": email,
        "function": function or "unknown",
        "model": model or "",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tokens": int(tokens or 0),
    }
    if prompt_tokens is not None:
        payload["prompt_tokens"] = int(prompt_tokens)
    if completion_tokens is not None:
        payload["completion_tokens"] = int(completion_tokens)
    if cost_usd is not None:
        payload["cost_usd"] = float(cost_usd)
    # Жизненный цикл вызова модели (пробрасывается в пересылку на devbim.com).
    if user_id:
        payload["user_id"] = user_id
    if request_date:
        payload["request_date"] = request_date
    if response_date:
        payload["response_date"] = response_date
    # is_success шлём всегда; error_message — только при неудаче.
    payload["is_success"] = bool(is_success)
    if not is_success and error_message:
        payload["error_message"] = error_message

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Лог отправки: что и куда уходит. Payload секретов не содержит
    # (Bearer-ключ живёт в headers и в лог не попадает).
    url = f"{server_url}/api/usage"
    send_line = (
        f"→ POST {url} · tokens={int(tokens or 0)}"
        + (f" (in={prompt_tokens}/out={completion_tokens})"
           if prompt_tokens is not None and completion_tokens is not None else "")
        + f" · model={model or '—'}"
    )
    _log(f"→ POST {url} payload={json.dumps(payload, ensure_ascii=False)}")

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_CREDITS_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        elapsed_ms = (time.monotonic() - started) * 1000
        # Лог получения: HTTP-код + тело ответа сервиса + длительность обмена.
        _log(f"← HTTP {resp.status_code} за {elapsed_ms:.0f} мс body={resp.text[:500]}")
        recv_line = f"← HTTP {resp.status_code} · {elapsed_ms:.0f} мс · {resp.text[:200]}"
        if resp.status_code >= 400:
            return {"ok": False, "lines": [send_line, recv_line]}
        try:
            data = resp.json()
            _log(
                f"reported email={email!r} func={function!r} model={model!r} "
                f"tokens={tokens} pricing={data.get('pricing')} credits={data.get('credits')}"
                + (f" cost=${data.get('cost_usd')}({data.get('cost_source')})"
                   if data.get("cost_usd") is not None else "")
            )
            recv_line = (
                f"← HTTP {resp.status_code} · {elapsed_ms:.0f} мс · "
                f"credits={data.get('credits')} · pricing={data.get('pricing')}"
                + (f" · cost=${data.get('cost_usd')} ({data.get('cost_source')})"
                   if data.get("cost_usd") is not None else "")
            )
            return {"ok": True, "lines": [send_line, recv_line]}
        except Exception:
            _log(f"reported tokens={tokens} (no json body)")
            return {"ok": True, "lines": [send_line, recv_line]}
    except Exception as exc:  # сеть/таймаут/DNS — глушим, функция Open WebUI живёт
        elapsed_ms = (time.monotonic() - started) * 1000
        _log(f"failed to report usage after {elapsed_ms:.0f} мс: "
             f"{type(exc).__name__}: {exc}")
        return {"ok": False,
                "lines": [send_line,
                          f"✗ {type(exc).__name__} после {elapsed_ms:.0f} мс: {exc}"]}


def _report_usage_bg(
    server_url: str,
    api_key: str,
    *,
    email: str,
    function: str,
    model: str,
    tokens: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    user_id: str = "",
    request_date: str | None = None,
    response_date: str | None = None,
    is_success: bool = True,
    error_message: str | None = None,
) -> None:
    """Запустить отправку фоновой задачей (fire-and-forget)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        _log("no event loop — отчёт пропущен")
        return
    loop.create_task(
        _credits_send(
            server_url, api_key,
            email=email, function=function, model=model, tokens=tokens,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            user_id=user_id, request_date=request_date, response_date=response_date,
            is_success=is_success, error_message=error_message,
        )
    )


# --------------------------------------------------------------------------- #
# Pipe
# --------------------------------------------------------------------------- #
class Pipe:
    """Прокси к OpenAI-совместимому API (OpenRouter) со списанием кредитов."""

    class Valves(BaseModel):
        # --- подключение к модели (OpenRouter / любой OpenAI-совместимый эндпоинт) ---
        API_BASE_URL: str = Field(
            default="https://openrouter.ai/api/v1",
            description="Базовый URL API (OpenAI-совместимый). "
            "Боевой OpenRouter: https://openrouter.ai/api/v1",
        )
        API_KEY: str = Field(
            default="",
            description="API-ключ модели (Authorization: Bearer). Для OpenRouter: sk-or-v1-...",
            json_schema_extra={"input": {"type": "password"}},
        )
        MODEL_NAME: str = Field(
            default="google/gemini-3.5-flash",
            description="Имя модели. ВАЖНО: оно же используется как ключ курса "
            "на сервере списания (вкладка Курсы).",
        )
        REQUEST_TIMEOUT: int = Field(
            default=60,
            description="Таймаут запроса к модели, сек.",
        )

        # --- административный вентиль: доступ на наш сервер списания кредитов ---
        CREDITS_SERVER_URL: str = Field(
            default="https://credits.dev-bim.com",
            description="Адрес сервера списания кредитов (публичный через Cloudflare "
            "или http://localhost:4010 при локальном запуске).",
        )
        CREDITS_API_KEY: str = Field(
            default="devbim2026",
            description="Ключ доступа на сервер списания (Bearer). Должен совпадать "
            "с CREDITS_API_KEY в .env сервера. Без него отчёт не пройдёт (401).",
            json_schema_extra={"input": {"type": "password"}},
        )

        ENABLE_LOGGING: bool = Field(
            default=True,
            description="Писать лог в консоль Open WebUI (docker logs).",
        )
        SHOW_CREDITS_LOG_IN_CHAT: bool = Field(
            default=True,
            description="Показывать в чате (блоком после ответа) лог обмена с "
            "сервером списания кредитов: → запрос, ← ответ с кодом/временем. "
            "При False — только в docker logs.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ----------------------------- helpers ----------------------------------
    def _log(self, msg: str) -> None:
        if not self.valves.ENABLE_LOGGING:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"[pipe] {ts} {msg}", file=sys.stdout, flush=True)

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
        # Внутренний ID пользователя Open WebUI — пробрасываем в пересылку на
        # devbim.com как openRouterWebUiUserId.
        user_id = str((__user__ or {}).get("id") or "")
        prompt = self._last_user_text(body.get("messages", []))
        self._log(
            f"REQUEST user={user_email!r} uid={user_id!r} "
            f"model={self.valves.MODEL_NAME!r} prompt={prompt[:80]!r}"
        )

        # Жизненный цикл вызова модели: фиксируем старт до запроса, финиш —
        # в каждой точке возврата (успех и любая ошибка). Рапортуем на сервер
        # списания В ОБОНХ случаях, чтобы devbim.com фиксировал все попытки.
        request_date = datetime.now(timezone.utc).isoformat()

        # Задачи отправки отчётов. При SHOW_CREDITS_LOG_IN_CHAT они ждутся
        # перед возвратом ответа (_with_logs) — лог обмена попадает в чат.
        report_tasks: list[asyncio.Task] = []

        def _report(
            *,
            tokens: int,
            prompt_tokens: int | None,
            completion_tokens: int | None,
            cost_usd: float | None,
            is_success: bool,
            error_message: str | None = None,
        ) -> None:
            """Запланировать отправку отчёта на сервер списания.

            Общие параметры (email/user_id/dates/model) замыкаются из внешней
            области; переменные — передаются аргументами. При включённом вентиле
            чата задача сохраняется в report_tasks, чтобы перед возвратом
            дождаться её и собрать строки лога.
            """
            response_date = datetime.now(timezone.utc).isoformat()
            kwargs = dict(
                email=user_email,
                function="Агент со списанием кредитов",
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
            if self.valves.SHOW_CREDITS_LOG_IN_CHAT:
                report_tasks.append(
                    asyncio.ensure_future(
                        _credits_send(self.valves.CREDITS_SERVER_URL,
                                      self.valves.CREDITS_API_KEY, **kwargs)
                    )
                )
            else:
                # Прежнее поведение: fire-and-forget, лог только в docker logs.
                _report_usage_bg(self.valves.CREDITS_SERVER_URL,
                                 self.valves.CREDITS_API_KEY, **kwargs)
            self._log(
                f"reported to credits server: tokens={tokens} "
                f"(in={prompt_tokens}/out={completion_tokens}) "
                f"cost={cost_usd if cost_usd is not None else '—'} "
                f"success={is_success} user={user_email!r}"
            )

        async def _with_logs(answer: str) -> str:
            """Дождаться отправки отчётов и приложить к ответу блок лога обмена.

            Никогда не меняет и не роняет ответ: при любых проблемах с задачами
            возвращается исходный текст.
            """
            if not report_tasks:
                return answer
            try:
                await asyncio.wait(report_tasks, timeout=_CREDITS_TIMEOUT + 3)
            except Exception:
                pass
            lines: list[str] = []
            for t in report_tasks:
                try:
                    if t.done() and not t.cancelled() and t.exception() is None:
                        res = t.result()
                        if isinstance(res, dict):
                            lines.extend(res.get("lines", []))
                except Exception:
                    continue
            if not lines:
                return answer
            return (answer + "\n\n---\n**Отчёт списания кредитов**\n```\n"
                    + "\n".join(lines) + "\n```")

        payload = copy.deepcopy(body)
        payload["model"] = self.valves.MODEL_NAME
        # Принудительно НЕ стримим: нам нужен цельный JSON-ответ с полем usage,
        # из которого мы берём prompt_tokens/completion_tokens для списания.
        # Open WebUI по умолчанию шлёт stream:true → OpenRouter отдаёт SSE, и
        # resp.json() падает с "Expecting value... line 1 column 1".
        payload["stream"] = False
        payload.pop("stream_options", None)
        # Authorization добавляем только при непустом ключе: httpx не принимает
        # "Bearer " с пустым значением (Illegal header value).
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        url = f"{self.valves.API_BASE_URL.rstrip('/')}/chat/completions"

        # --- запрос к модели ---
        try:
            async with httpx.AsyncClient(timeout=self.valves.REQUEST_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except Exception as e:
            self._log(f"ERROR request to model failed: {e}")
            _report(tokens=0, prompt_tokens=None, completion_tokens=None,
                    cost_usd=None, is_success=False, error_message=str(e))
            return await _with_logs(f"❌ Не удалось подключиться к модели: {e}")

        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            self._log(f"ERROR model {err}")
            _report(tokens=0, prompt_tokens=None, completion_tokens=None,
                    cost_usd=None, is_success=False, error_message=err)
            return await _with_logs(f"❌ Модель вернула {err}")

        # Безопасный парсинг: если пришёл не JSON (например, SSE-поток из-за
        # stream:true где-то в пути), дадим читаемую ошибку вместо "Expecting value".
        try:
            data = resp.json()
        except Exception:
            err = f"non-JSON response: {resp.text[:200]!r}"
            self._log(f"ERROR {err}")
            _report(tokens=0, prompt_tokens=None, completion_tokens=None,
                    cost_usd=None, is_success=False, error_message=err)
            return await _with_logs(
                "❌ Модель вернула не-JSON ответ (возможно, SSE-стрим). "
                f"Первые 200 символов: {resp.text[:200]!r}"
            )
        usage = data.get("usage", {}) or {}
        tokens = int(usage.get("total_tokens") or 0)
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        # Стоимость в $ от провайдера (OpenRouter); для z.ai/GLM — None.
        cost_usd = _extract_cost_usd(usage)

        # Извлечь текст ответа (для показа пользователю).
        # content может отсутствовать или быть None в нескольких случаях:
        #   1. tool/function-calling — модель вызвала инструменты (finish_reason
        #      = "tool_calls"), а текста ещё нет. Этот пайп — тонкий прокси и не
        #      выполняет цикл tool-calling, поэтому честно скажем об этом.
        #   2. reasoning-модели (GLM-4.5/5.2, DeepSeek-R1...) — текст лежит в
        #      reasoning_content, а content=null. Возьмём reasoning_content.
        try:
            choice = data["choices"][0] or {}
        except (KeyError, IndexError, TypeError):
            choice = {}
        msg = choice.get("message") or {}
        answer = msg.get("content")

        # reasoning-модели: основной content пуст, но есть reasoning_content.
        if not isinstance(answer, str):
            answer = msg.get("reasoning_content")

        # tool-calling: модель вызвала инструменты вместо текстового ответа.
        # Список имён вызовов покажем пользователю, чтобы было понятно.
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
                    "tool-calling — поэтому текстового ответа нет. "
                    "Стоимость запроса уже учтена. Попробуйте переформулировать "
                    "запрос или используйте функцию, поддерживающую инструменты."
                )

        # Всё ещё не строка — приведём весь ответ к строке как последний рубеж.
        if not isinstance(answer, str):
            self._log(
                f"WARN no text content in response; message keys="
                f"{list(msg.keys()) if isinstance(msg, dict) else type(msg).__name__}"
            )
            answer = "⚠️ Модель не вернула текстовый ответ."

        # Если модель не вернула usage — грубо оценим по сумме prompt+answer.
        if not tokens:
            tokens = _estimate_tokens(prompt + " " + answer)
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
        return await _with_logs(answer)
