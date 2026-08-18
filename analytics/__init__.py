"""Self-contained LLM analytics + monitoring package.

Public surface used by the host project:
    from .analytics import router, init_analytics, install_log_buffer, tracker
"""
from __future__ import annotations

from . import tracker
from .routes import router
from .log_buffer import install as install_log_buffer
from .settings import settings


async def init_analytics() -> None:
    """Create DB tables and start the background credits poller. Idempotent & safe."""
    import asyncio
    import logging
    from . import db, stats
    log = logging.getLogger("analytics")
    try:
        await db.init_db()
        log.info("analytics DB ready at %s", settings.analytics_db_path)
    except Exception:
        log.exception("analytics DB init failed")
    if settings.openrouter_api_key and settings.credits_poll_interval > 0:
        asyncio.create_task(stats.credits_poll_loop())
        log.info("credits poller started (interval=%ss)", settings.credits_poll_interval)


__all__ = ["router", "init_analytics", "install_log_buffer", "tracker", "settings"]
