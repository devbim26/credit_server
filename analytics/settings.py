"""Self-contained settings. No pydantic-settings dependency: reads .env + process env.

Env vars (all optional unless noted). Add these to the host .env as needed:

  ANALYTICS_DB_PATH=data/analytics.db          # SQLite path
  ANALYTICS_TRACK_USAGE=true                   # log each LLM call
  ANALYTICS_REQUIRE_AUTH=false                 # protect /stats + /dashboard with bearer
  ANALYTICS_API_KEY=                           # bearer for /stats when REQUIRE_AUTH=true
  ANALYTICS_APP_TITLE=LLM Analytics            # dashboard header title
  ANALYTICS_CREDITS_POLL_INTERVAL=600          # seconds between /credits polls

  # OpenRouter account (optional — only for the live account-balance panel):
  OPENROUTER_API_KEY=
  OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
  OPENROUTER_MODELS=model-a,model-b           # comma-separated, for the dashboard dropdown
  DEFAULT_MODEL=model-a

  # External billing webhook (optional, disabled by default):
  WEBHOOK_ENABLED=false
  WEBHOOK_URL=https://example.com/api/report
  WEBHOOK_TOKEN=                               # sent as "Authorization: Bearer <token>"
  WEBHOOK_AUTH_HEADER=Authorization
  WEBHOOK_USER_ID=                             # fallback UUID if caller sends none
  WEBHOOK_TIMEOUT=15

  # Cloudflared tunnel (optional):
  TUNNEL_NAME=                                 # e.g. my-tunnel
  TUNNEL_PUBLIC_HOST=                          # e.g. my.example.com
  TUNNEL_CONFIG=                               # path; empty => ~/.cloudflared/config-<TUNNEL_NAME>.yml
  CLOUDFLARED_BIN=                             # path; empty => auto (PATH or ~/.cloudflared/cloudflared.exe)
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    seen: set[Path] = set()
    candidates = [Path.cwd() / ".env"]
    p = Path(__file__).resolve()
    for up in (p.parents[2], p.parents[1]):
        candidates.append(up / ".env")
    for c in candidates:
        if c in seen or not c.exists():
            continue
        seen.add(c)
        try:
            for line in c.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass


_load_dotenv()


def _str(key: str, d: str = "") -> str:
    return os.environ.get(key, d)


def _bool(key: str, d: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return d
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, d: int) -> int:
    try:
        return int(os.environ.get(key, str(d)))
    except Exception:
        return d


class _Settings:
    # tracking / DB
    analytics_db_path: str = _str("ANALYTICS_DB_PATH", "data/analytics.db")
    # Основная БД сервера списания (usage_records + forward_log) — источник
    # данных для вкладки «Отправлено во внешний сервис».
    main_db_path: str = _str("DB_PATH", "credits.db")
    analytics_track_usage: bool = _bool("ANALYTICS_TRACK_USAGE", True)
    analytics_require_auth: bool = _bool("ANALYTICS_REQUIRE_AUTH", False)
    analytics_api_key: str = _str("ANALYTICS_API_KEY")
    analytics_app_title: str = _str("ANALYTICS_APP_TITLE", "LLM Analytics")
    credits_poll_interval: int = _int("ANALYTICS_CREDITS_POLL_INTERVAL", 600)

    # OpenRouter account (optional)
    openrouter_api_key: str = _str("OPENROUTER_API_KEY")
    openrouter_base_url: str = _str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_models: str = _str("OPENROUTER_MODELS")
    default_model: str = _str("DEFAULT_MODEL")

    # external webhook (optional)
    webhook_enabled: bool = _bool("WEBHOOK_ENABLED", False)
    webhook_url: str = _str("WEBHOOK_URL")
    webhook_token: str = _str("WEBHOOK_TOKEN")
    webhook_auth_header: str = _str("WEBHOOK_AUTH_HEADER", "Authorization")
    webhook_user_id: str = _str("WEBHOOK_USER_ID")
    webhook_timeout: int = _int("WEBHOOK_TIMEOUT", 15)

    # tunnel (optional)
    tunnel_name: str = _str("TUNNEL_NAME")
    tunnel_public_host: str = _str("TUNNEL_PUBLIC_HOST")
    tunnel_config: str = _str("TUNNEL_CONFIG")
    cloudflared_bin: str = _str("CLOUDFLARED_BIN")

    @property
    def models_list(self) -> list[str]:
        return [m.strip() for m in self.openrouter_models.split(",") if m.strip()]


settings = _Settings()
