"""credit_reporter.py — сниппет учёта токенов для Open WebUI функций (Pipe).

КУДА ВСТАВЛЯТЬ
-------------
Положите этот файл рядом с вашей функцией-агентом (или внутрь того же модуля,
если функция одна). В коде функции (обычно в методе pipe) — после того, как
получен ответ модели и известно число токенов — импортируйте и вызовите:

    from credit_reporter import report_usage

    # ... вы получили resp (dict) от модели ...
    usage  = resp.get("usage", {}) or {}
    model  = resp.get("model", "") or self.valves.MODEL_NAME
    tokens = usage.get("total_tokens", 0)

    await report_usage(
        email=__user__.get("email", ""),
        function="Название вашей функции",   # любая строка-метка
        model=model,
        tokens=tokens,
    )

Раздельная тарификация input/output (опционально) — если на сервере для модели
заданы курсы input/output, передавайте breakdown:

    await report_usage(
        email=__user__.get("email", ""),
        function="Название вашей функции",
        model=model,
        tokens=usage.get("total_tokens", 0),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )

Для СТРИМИНГА (когда usage приходит в последнем SSE-чанке) — вызывайте в конце
stream_response(), когда usage уже известен. Если usage отсутствует — можно
передать estimate_estimate_tokens(text) в качестве tokens (см. ниже).

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (задаются в .env Open WebUI или переменными среды контейнера)
--------------------------------------------------------------------------------
CREDITS_SERVER_URL   — адрес сервера списания, напр. http://localhost:4010
CREDITS_API_KEY      — Bearer-ключ (совпадает с CREDITS_API_KEY на сервере)
CREDITS_FUNCTION     — (необязательно) имя функции «по умолчанию», если не передать явно
CREDITS_TIMEOUT      — (необязательно) таймаут в секундах, по умолчанию 5

ПОВЕДЕНИЕ
---------
- Fire-and-forget: отправка идёт фоновой задачей и НЕ блокирует ответ функции.
- Любая ошибка (сервер недоступен, неверный ключ, сбой сети) глушится — работа
  функции Open WebUI НЕ ломается. Ошибка лишь пишется в консоль (docker logs).
- httpx используется как зависимость (уже есть в большинстве Open WebUI пайпов).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Optional

try:
    import httpx
except ImportError:  # pragma: no cover - httpx обязателен в Open WebUI
    httpx = None  # type: ignore


SERVER_URL = os.environ.get("CREDITS_SERVER_URL", "").rstrip("/")
API_KEY = os.environ.get("CREDITS_API_KEY", "")
DEFAULT_FUNCTION = os.environ.get("CREDITS_FUNCTION", "")
TIMEOUT = float(os.environ.get("CREDITS_TIMEOUT", "5"))


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[credit_reporter] {ts} {msg}", file=sys.stdout, flush=True)


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов: ~4 символа на токен.

    Используйте, только если модель не вернула usage.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def extract_cost_usd(usage):
    """Извлечь стоимость в USD из usage ответа провайдера.

    OpenRouter возвращает usage.cost как объект {"total_cost": 0.000123}
    (старые версии — как число). z.ai/GLM поля cost не возвращают — вернём None,
    тогда сервер посчитает $ по тарифам $/млн токенов.
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


async def _send(
    email: str,
    function: str,
    model: str,
    tokens: int,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    user_id: str = "",
    request_date: Optional[str] = None,
    response_date: Optional[str] = None,
    is_success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """Реальная HTTP-отправка на сервер. Никогда не падает наружу."""
    if not SERVER_URL:
        _log("CREDITS_SERVER_URL не задан — пропуск отчёта")
        return
    if httpx is None:
        _log("httpx не установлен — отчёт невозможен")
        return

    payload = {
        "email": email,
        "function": function or DEFAULT_FUNCTION or "unknown",
        "model": model or "",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tokens": int(tokens or 0),
    }
    # Breakdown input/output — только если реально передан.
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
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    url = f"{SERVER_URL}/api/usage"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            _log(f"server returned HTTP {resp.status_code}: {resp.text[:200]}")
            return
        try:
            data = resp.json()
            pricing = data.get("pricing", "base")
            _log(
                f"reported email={email!r} func={payload['function']!r} "
                f"model={model!r} tokens={tokens} pricing={pricing} "
                f"credits={data.get('credits')}"
                + (f" cost=${data.get('cost_usd')}({data.get('cost_source')})"
                   if data.get("cost_usd") is not None else "")
            )
        except Exception:
            _log(f"reported tokens={tokens} (no json body)")
    except Exception as exc:  # сеть/таймаут/DNS — глушим
        _log(f"failed to report usage: {exc}")


def report_usage(
    *,
    email: str,
    function: str,
    model: str = "",
    tokens: int = 0,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    user_id: str = "",
    request_date: Optional[str] = None,
    response_date: Optional[str] = None,
    is_success: bool = True,
    error_message: Optional[str] = None,
) -> Optional[asyncio.Task]:
    """Отправить отчёт об использовании (НЕ блокирует вызывающего).

    Возвращает созданную asyncio.Task (fire-and-forget) или None, если запустить
    задачу не удалось (нет работающего event loop — например, вызов из синхронного
    контекста). В asyncio-контексте Open WebUI всегда есть loop.

    prompt_tokens/completion_tokens — опциональный breakdown для раздельной
    тарификации input/output (если на сервере заданы такие курсы).
    cost_usd — стоимость в $ от провайдера (напр. usage.cost у OpenRouter);
    если None, сервер посчитает $ сам по тарифам $/млн токенов.
    user_id — внутренний ID пользователя Open WebUI (openRouterWebUiUserId),
    пробрасывается в пересылку на devbim.com.
    request_date/response_date — время старта/завершения вызова модели.
    is_success/error_message — результат вызова модели; шлём отчёт в обоих
    случаях (успех и ошибка), чтобы devbim.com фиксировал все попытки.

    Безопасно вызывать и как `await report_usage(...)`, и без await — в последнем
    случае отправка выполнится фоновой задачей.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # Нет loop — пробуем отправить синхронно через asyncio.run (best-effort).
        try:
            asyncio.run(
                _send(email, function, model, tokens, prompt_tokens,
                      completion_tokens, cost_usd,
                      user_id=user_id, request_date=request_date,
                      response_date=response_date, is_success=is_success,
                      error_message=error_message)
            )
        except Exception as exc:
            _log(f"sync fallback failed: {exc}")
        return None

    return loop.create_task(
        _send(email, function, model, tokens, prompt_tokens,
              completion_tokens, cost_usd,
              user_id=user_id, request_date=request_date,
              response_date=response_date, is_success=is_success,
              error_message=error_message)
    )


async def areport_usage(
    *,
    email: str,
    function: str,
    model: str = "",
    tokens: int = 0,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    user_id: str = "",
    request_date: Optional[str] = None,
    response_date: Optional[str] = None,
    is_success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """Альтернатива: явно дождаться отправки (await report_usage(...))."""
    await _send(email, function, model, tokens, prompt_tokens,
                completion_tokens, cost_usd,
                user_id=user_id, request_date=request_date,
                response_date=response_date, is_success=is_success,
                error_message=error_message)
