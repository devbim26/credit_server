"""Пересылка принятых записей на сервер списания devbim.com.

Каждая новая запись usage_records после приёма получает запись forward_log
в статусе pending. Этот модуль:
- забирает pending-записи,
- PUT'ит тело контракта updateSubscription на FORWARD_URL
  (openRouterWebUiUserId, messageCost, modelId, requestDate, responseDate,
   isSuccess, errorMessage, metadataJson),
- при успехе (HTTP < 400) отмечает ok, при ошибке — failed (с текстом и http-кодом),
- поддерживает ручной retry всех failed через GUI.
- толерантно разбирает ответ {dateTime, email, charged, balance}, если он есть:
  обновляет реестр user_balances и колонки forward_log.resp_*.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

import db

# Таймаут одной попытки пересылки.
FORWARD_TIMEOUT = 10.0


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[forwarder] {ts} {msg}", file=sys.stdout, flush=True)


def _to_float(v) -> float | None:
    """Привести значение к float либо вернуть None.

    None/bool и любые нечисловые значения дают None. bool проверяем первым,
    так как float(True) == 1.0 (в Python bool — подкласс int), а JSON true
    не является валидным балансом.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def forward_pending(db_path: str) -> int:
    """Разослать все pending-записи. Возвращает количество обработанных."""
    entries = await db.list_forward(db_path, status="pending", limit=1000)
    if not entries:
        return 0

    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
        for e in entries:
            await _send_one(db_path, client, e)
    return len(entries)


async def retry_failed(db_path: str) -> int:
    """Перепослать все failed-записи (ручной retry из GUI)."""
    entries = await db.list_forward(db_path, status="failed", limit=1000)
    if not entries:
        return 0
    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
        for e in entries:
            # перед повторной попыткой вернём статус в pending.
            # Этот вызов также обнуляет resp_* (они по умолчанию None в расширенной
            # сигнатуре) — прежний ответ от успешной попытки очищается перед retry.
            await db.update_forward_entry(
                db_path, e["id"], status="pending", http_status=None, error=None
            )
            await _send_one(db_path, client, {**e, "status": "pending"})
    return len(entries)


async def _send_one(
    db_path: str, client: httpx.AsyncClient, entry: dict
) -> None:
    """Выполнить один POST по записи forward_log."""
    usage = await db.get_usage_for_forward(db_path, entry["usage_record_id"])
    if usage is None:
        # Запись использования исчезла — отметить как failed.
        await db.update_forward_entry(
            db_path, entry["id"], status="failed",
            http_status=None, error="usage record not found",
        )
        return

    url = await db.get_setting(db_path, "forward_url", "")
    if not url:
        # URL пересылки не задан — оставляем в pending, не считаем ошибкой.
        _log("forward_url не задан — пропускаю (запись осталась в pending)")
        return

    api_key = await db.get_setting(db_path, "forward_api_key", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Контракт devbim.com: PUT /api/OpenRouterModels/updateSubscription.
    # requestText/responseText сознательно пустые — содержимое диалогов не
    # покидает границы Open WebUI (см. согласованное решение в плане).
    # metadataJson — свёрнутый JSON с исходными полями нашего учёта (email,
    # function, timestamp, tokens и breakdown), чтобы devbim.com имел контекст.
    is_success = bool(usage.get("is_success", True))
    metadata = {
        "email": usage["email"],
        "function": usage["function"],
        "timestamp": usage["op_timestamp"],
        "tokens": usage["tokens"],
    }
    if usage.get("prompt_tokens") is not None:
        metadata["prompt_tokens"] = usage["prompt_tokens"]
    if usage.get("completion_tokens") is not None:
        metadata["completion_tokens"] = usage["completion_tokens"]

    payload = {
        "openRouterWebUiUserId": str(usage.get("user_id") or ""),
        "messageCost": float(usage.get("cost_usd") or 0.0),
        "modelId": usage["model"],
        "modelName": usage["model"],
        "requestText": "",
        "responseText": "",
        "requestDate": usage.get("request_date"),
        "responseDate": usage.get("response_date"),
        "isSuccess": is_success,
        "errorMessage": usage.get("error_message"),
        "metadataJson": json.dumps(metadata, ensure_ascii=False),
    }

    try:
        resp = await client.put(url, json=payload, headers=headers)
        if resp.status_code < 400:
            # Парсим тело ответа внешнего сервера по контракту
            # {dateTime, email, charged, balance}, ЕСЛИ он есть. devbim.com
            # отвечает полем dateTime (camelCase); ради совместимости со старым
            # моком/контрактом принимаем и datetime (нижний регистр) как фолбэк.
            # Успех определяется по HTTP < 400, поля ответа остаются NULL при
            # отсутствии/ошибке разбора. Любая ошибка разбора — НЕ роняем пересылку.
            resp_datetime = resp_email = None
            resp_charged = resp_balance = None
            resp_raw = (resp.text or "")[:1024]
            try:
                body = resp.json()
                if isinstance(body, dict):
                    resp_datetime = body.get("dateTime") or body.get("datetime")
                    resp_email = body.get("email")
                    resp_charged = body.get("charged")
                    resp_balance = body.get("balance")
            except Exception:
                body = None

            # Нормализуем charged/balance к float либо None сразу после разбора:
            # и для чистой записи в forward_log, и для апсёрта реестра.
            # Нечисловые значения (строки, bool) толерируем — считаем absent.
            resp_charged = _to_float(resp_charged)
            resp_balance = _to_float(resp_balance)

            await db.update_forward_entry(
                db_path, entry["id"], status="ok",
                http_status=resp.status_code, error=None,
                resp_datetime=resp_datetime,
                resp_email=resp_email,
                resp_charged=resp_charged,
                resp_balance=resp_balance,
                resp_raw=resp_raw if body is not None else None,
            )

            # Реестр балансов обновляем только при валидном balance (не None).
            # balance=0 — валиден. Email берём из usage_records (надёжно), а не
            # из resp_email (справочное поле ответа). resp_* уже float-or-None,
            # повторный float() не нужен и не вызывается.
            if resp_balance is not None:
                await db.upsert_balance(
                    db_path,
                    email=usage["email"],
                    balance=resp_balance,
                    last_charged=resp_charged,
                    last_updated=resp_datetime,
                    forward_id=entry["id"],
                )

            _log(
                f"OK usage={entry['usage_record_id']} http={resp.status_code} "
                f"email={usage['email']!r} "
                f"charged={resp_charged} balance={resp_balance}"
            )
        else:
            body = (resp.text or "")[:300]
            await db.update_forward_entry(
                db_path, entry["id"], status="failed",
                http_status=resp.status_code, error=body,
            )
            _log(
                f"FAIL usage={entry['usage_record_id']} http={resp.status_code} "
                f"body={body!r}"
            )
    except Exception as exc:  # httpx.RequestError и прочие
        await db.update_forward_entry(
            db_path, entry["id"], status="failed",
            http_status=None, error=str(exc),
        )
        _log(f"ERROR usage={entry['usage_record_id']} {exc}")


def schedule_forward(db_path: str) -> asyncio.Task:
    """Запустить пересылку как фоновую задачу (fire-and-forget).

    Ошибки самой задачи логируются, но не роняют сервер.
    """
    async def _runner():
        try:
            await forward_pending(db_path)
        except Exception as exc:  # pragma: no cover - защитное логирование
            _log(f"background forward task crashed: {exc}")

    loop = asyncio.get_event_loop()
    task = loop.create_task(_runner())
    return task
