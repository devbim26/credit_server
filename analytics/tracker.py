"""The single integration point for the host: tracker.track(...).

Call it at every LLM call site, on success AND error. It flattens the provider
`usage` dict, writes the SQLite row, and (if WEBHOOK_ENABLED) fires an async
fire-and-forget PUT to the billing webhook. Never raises.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from . import db
from .settings import settings

log = logging.getLogger("analytics.tracker")


def now_ts() -> str:
    """UTC ts for the DB (sortable, strftime-friendly)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_us() -> str:
    """UTC ts with microseconds, no zone suffix (for webhook request/response dates)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def resolve_user_id(header_val: str | None, body_val: str | None) -> str | None:
    """Precedence: header -> body -> WEBHOOK_USER_ID fallback."""
    return header_val or body_val or settings.webhook_user_id or None


def _flatten_usage(usage: dict | None) -> dict[str, Any]:
    """Pull token fields + cost out of an OpenAI/OpenRouter-style usage dict."""
    out: dict[str, Any] = {}
    if usage:
        out["prompt_tokens"] = usage.get("prompt_tokens")
        out["completion_tokens"] = usage.get("completion_tokens")
        out["total_tokens"] = usage.get("total_tokens")
        out["cost"] = usage.get("cost")
        pt = usage.get("prompt_tokens_details") or {}
        ct = usage.get("completion_tokens_details") or {}
        out["cached_tokens"] = pt.get("cached_tokens")
        out["reasoning_tokens"] = ct.get("reasoning_tokens")
    return out


async def track(
    *,
    model_requested: str | None = None,
    model_actual: str | None = None,
    usage: dict[str, Any] | None = None,
    email: str | None = None,
    user_id: str | None = None,
    request_text: str | None = None,
    response_text: str | None = None,
    latency_ms: int | None = None,
    status: str = "success",
    error: str | None = None,
    task: str | None = None,
    num_images: int | None = None,
    image_bytes: int | None = None,
    schema_valid: bool | None = None,
    generation_id: str | None = None,
    request_date: str | None = None,
    response_date: str | None = None,
) -> int | None:
    """Record one LLM call. Returns the new request rowid (or None if tracking off/failed)."""
    rowid: int | None = None
    if settings.analytics_track_usage:
        flat = _flatten_usage(usage)
        record = {
            "ts": now_ts(),
            "email": email,
            "task": task,
            "model_requested": model_requested,
            "model_actual": model_actual,
            "num_images": num_images,
            "image_bytes": image_bytes,
            "latency_ms": latency_ms,
            "status": status,
            "schema_valid": None if schema_valid is None else int(bool(schema_valid)),
            "error": error,
            "generation_id": generation_id,
            **flat,
        }
        rowid = await db.log_request(record)

    if settings.webhook_enabled and user_id:
        asyncio.create_task(_forward(
            request_rowid=rowid, user_id=user_id, model_id=model_actual or model_requested,
            usage=usage, request_text=request_text, response_text=response_text,
            request_date=request_date or now_us(), response_date=response_date or now_us(),
            is_success=(status == "success"), error_message=error,
        ))
    return rowid


def _model_name(model_id: str | None) -> str:
    if not model_id:
        return ""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def _build_payload(*, user_id, model_id, usage, request_text, response_text,
                   request_date, response_date, is_success, error_message) -> dict[str, Any]:
    usage = usage or {}
    meta = {
        "model": model_id,
        "tokens": usage.get("total_tokens"),
        "cost_usd": usage.get("cost"),
        "streaming": False,
    }
    return {
        "openRouterWebUiUserId": user_id,
        "messageCost": usage.get("cost"),
        "modelId": model_id,
        "modelName": _model_name(model_id),
        "requestText": request_text or "",
        "responseText": response_text or "",
        "requestDate": request_date,
        "responseDate": response_date,
        "isSuccess": bool(is_success),
        "errorMessage": error_message,
        "metadataJson": json.dumps(meta, ensure_ascii=False),
    }


async def _send_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if settings.webhook_token:
        headers[settings.webhook_auth_header] = f"Bearer {settings.webhook_token}"
    timeout = httpx.Timeout(settings.webhook_timeout, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.put(settings.webhook_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("webhook PUT failed (transport): %s", e)
        return {"ok": False, "http_status": None, "body": None, "error": str(e)}
    try:
        body: Any = resp.json()
    except (ValueError, json.JSONDecodeError):
        body = resp.text[:1000]
    ok = 200 <= resp.status_code < 300
    if not ok:
        log.warning("webhook PUT %s: %s", resp.status_code, str(body)[:300])
    return {"ok": ok, "http_status": resp.status_code, "body": body, "error": None}


async def _forward(*, request_rowid, user_id, model_id, usage, request_text, response_text,
                   request_date, response_date, is_success, error_message) -> None:
    try:
        payload = _build_payload(user_id=user_id, model_id=model_id, usage=usage,
                                 request_text=request_text, response_text=response_text,
                                 request_date=request_date, response_date=response_date,
                                 is_success=is_success, error_message=error_message)
        result = await _send_webhook(payload)
        body = result.get("body")
        ext = {}
        if isinstance(body, dict):
            ext = {"ext_email": body.get("email"), "ext_charged": body.get("charged"),
                   "ext_balance": body.get("balance"), "ext_datetime": body.get("dateTime")}
        record = {
            "ts": now_us(), "request_id": request_rowid,
            "open_router_web_ui_user_id": user_id, "model_id": model_id,
            "model_name": _model_name(model_id),
            "message_cost": (usage or {}).get("cost"), "total_tokens": (usage or {}).get("total_tokens"),
            "request_date": request_date, "response_date": response_date,
            "is_success": int(bool(is_success)), "error_message": error_message,
            "metadata_json": payload["metadataJson"], "request_text": request_text,
            "response_text": response_text, "ext_ok": int(bool(result.get("ok"))),
            "ext_status": result.get("http_status"), "ext_error": result.get("error"), **ext,
        }
        await db.log_external_report(record)
        if ext.get("ext_email") and request_rowid is not None:
            await db.update_request_email(request_rowid, ext["ext_email"])
    except Exception:
        log.exception("forward unexpectedly failed")
