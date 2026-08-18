"""APIRouter: /stats/* JSON endpoints + /dashboard HTML page. Self-contained auth."""
from __future__ import annotations

import asyncio
import hmac
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response

from . import log_buffer, stats, tunnel
from .settings import settings

router = APIRouter()


def _require_auth(x_token: str | None = Header(default=None, alias="X-Analytics-Token"),
                  authorization: str | None = Header(default=None)):
    """If ANALYTICS_REQUIRE_AUTH, require Bearer ANALYTICS_API_KEY (or X-Analytics-Token)."""
    if not settings.analytics_require_auth:
        return
    expected = settings.analytics_api_key
    if not expected:
        return  # no key configured => no protection
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid analytics token")


# protect all data endpoints with the dependency
_auth = [Depends(_require_auth)]


@router.get("/stats/summary", dependencies=_auth)
async def _summary(range: str = Query("7d")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_summary(range)


@router.get("/stats/timeline", dependencies=_auth)
async def _timeline(range: str = Query("7d"), bucket: str = Query("day")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    if bucket not in stats.VALID_BUCKETS:
        raise HTTPException(400, f"bucket must be one of {stats.VALID_BUCKETS}")
    return await stats.get_timeline(range, bucket)


@router.get("/stats/models", dependencies=_auth)
async def _models(range: str = Query("30d")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_models(range)


@router.get("/stats/emails", dependencies=_auth)
async def _emails(range: str = Query("30d")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_emails(range)


@router.get("/stats/users/report", dependencies=_auth)
async def _user_report(range: str = Query("30d")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_user_report(range)


@router.get("/stats/export/xlsx", dependencies=_auth)
async def _export_xlsx(range: str = Query("30d"), email: str | None = None):
    """Excel-выгрузка за период: сводка по пользователям + детализация запросов + балансы."""
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    data = await asyncio.to_thread(stats.build_report_xlsx, range, email)
    fname = f"report_{range}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/stats/requests", dependencies=_auth)
async def _requests(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    status: str | None = None, model: str | None = None,
                    email: str | None = None, range: str = Query("24h")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_requests(limit, offset, status, model, email, range)


@router.get("/stats/credits", dependencies=_auth)
async def _credits(live: bool = Query(False)):
    return await stats.get_credits(live)


@router.get("/stats/key", dependencies=_auth)
async def _key():
    return {"key": await stats.fetch_openrouter_key_info()}


@router.get("/stats/settings", dependencies=_auth)
async def _settings():
    return stats.get_settings()


@router.get("/stats/external", dependencies=_auth)
async def _external(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    range: str = Query("24h")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_external_reports(limit, offset, range)


@router.get("/stats/external/summary", dependencies=_auth)
async def _external_summary(range: str = Query("30d")):
    if range not in stats.VALID_RANGES:
        raise HTTPException(400, f"range must be one of {stats.VALID_RANGES}")
    return await stats.get_external_summary(range)


@router.get("/stats/external/probe", dependencies=_auth)
async def _external_probe():
    """Живая проверка доступности внешнего сервиса. Кэш ~30с."""
    return await stats.probe_external()


@router.get("/stats/logs", dependencies=_auth)
async def _logs(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)):
    return log_buffer.get_logs(since, limit)


@router.get("/stats/tunnel", dependencies=_auth)
async def _tunnel():
    # to_thread: tunnel.status() опрашивает процессы ОС (PowerShell ~0.3-1с,
    # кэш 10с) — нельзя блокировать event loop.
    return await asyncio.to_thread(tunnel.status)


@router.post("/stats/tunnel/start", dependencies=_auth)
async def _tunnel_start():
    return await tunnel.start()


@router.post("/stats/tunnel/stop", dependencies=_auth)
async def _tunnel_stop():
    return await tunnel.stop()


@lru_cache
def _dashboard_html() -> str:
    path = Path(__file__).parent / "templates" / "dashboard.html"
    return path.read_text(encoding="utf-8")


# Плейсхолдер в templates/dashboard.html: при прямом локальном доступе сервер
# подставляет сюда аналитический ключ, при любом другом — пустую строку.
_AUTO_TOKEN = "__AUTO_TOKEN__"


def _is_local_direct(request: Request) -> bool:
    """True для запроса с этой же машины напрямую: loopback и без X-Forwarded-For.

    cloudflared (и любые реверс-прокси) ставят X-Forwarded-For даже когда подключаются
    с 127.0.0.1, поэтому запрос через туннель доверия не получает — только прямой заход.
    """
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1") and not request.headers.get("x-forwarded-for")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    html = _dashboard_html()
    if settings.analytics_require_auth and _is_local_direct(request):
        key = settings.analytics_api_key.replace("\\", "\\\\").replace('"', '\\"')
        html = html.replace(_AUTO_TOKEN, key)
    else:
        html = html.replace(_AUTO_TOKEN, "")
    return HTMLResponse(html)
