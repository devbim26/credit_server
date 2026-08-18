"""SQLite storage: LLM-call log + account-credit snapshots + external-webhook reports.

All writes are async (asyncio.to_thread); log_request never raises.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import settings

log = logging.getLogger("analytics.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    email TEXT,
    task TEXT,
    model_requested TEXT,
    model_actual TEXT,
    num_images INTEGER,
    image_bytes INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost REAL,
    latency_ms INTEGER,
    status TEXT,
    schema_valid INTEGER,
    error TEXT,
    generation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model_actual);

CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    total_credits REAL,
    total_usage REAL,
    remaining REAL,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_credits_ts ON credits(ts);

CREATE TABLE IF NOT EXISTS external_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    request_id INTEGER,
    open_router_web_ui_user_id TEXT,
    model_id TEXT,
    model_name TEXT,
    message_cost REAL,
    total_tokens INTEGER,
    request_date TEXT,
    response_date TEXT,
    is_success INTEGER,
    error_message TEXT,
    metadata_json TEXT,
    request_text TEXT,
    response_text TEXT,
    ext_ok INTEGER,
    ext_status INTEGER,
    ext_email TEXT,
    ext_charged REAL,
    ext_balance REAL,
    ext_datetime TEXT,
    ext_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ext_ts ON external_reports(ts);
"""

_REQUEST_COLUMNS = (
    "ts", "email", "task", "model_requested", "model_actual", "num_images", "image_bytes",
    "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
    "reasoning_tokens", "cost", "latency_ms", "status", "schema_valid", "error", "generation_id",
)
_EXT_COLUMNS = (
    "ts", "request_id", "open_router_web_ui_user_id", "model_id", "model_name", "message_cost",
    "total_tokens", "request_date", "response_date", "is_success", "error_message", "metadata_json",
    "request_text", "response_text", "ext_ok", "ext_status", "ext_email", "ext_charged",
    "ext_balance", "ext_datetime", "ext_error",
)


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.analytics_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_dir() -> None:
    parent = Path(settings.analytics_db_path).parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def _init_db_sync() -> None:
    _ensure_dir()
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _log_request_sync(record: dict[str, Any]) -> int | None:
    cols = [c for c in _REQUEST_COLUMNS if c in record]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO requests ({','.join(cols)}) VALUES ({placeholders})"
    with _connect() as conn:
        cur = conn.execute(sql, [record[c] for c in cols])
        return cur.lastrowid


def _save_credits_sync(total, usage, remaining, raw: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO credits (ts, total_credits, total_usage, remaining, raw) VALUES (?, ?, ?, ?, ?)",
            (now_ts(), total, usage, remaining, raw),
        )


def _log_external_report_sync(record: dict[str, Any]) -> None:
    cols = [c for c in _EXT_COLUMNS if c in record]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO external_reports ({','.join(cols)}) VALUES ({placeholders})"
    with _connect() as conn:
        conn.execute(sql, [record[c] for c in cols])


def _update_request_email_sync(rowid: int, email: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE requests SET email = ? WHERE id = ? AND (email IS NULL OR email = '')",
            [email, rowid],
        )


def _where(since_ts, status, model, email=None):
    clauses, params = [], []
    if since_ts:
        clauses.append("ts >= ?"); params.append(since_ts)
    if status:
        clauses.append("status = ?"); params.append(status)
    if model:
        clauses.append("(model_actual = ? OR model_requested = ?)"); params.extend([model, model])
    if email:
        clauses.append("email = ?"); params.append(email)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _get_summary_sync(since_ts):
    where, params = _where(since_ts, None, None)
    sql = f"""SELECT COUNT(*) AS total_requests,
        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
        COALESCE(SUM(cost),0) AS total_cost,
        COALESCE(SUM(prompt_tokens),0) AS total_prompt_tokens,
        COALESCE(SUM(completion_tokens),0) AS total_completion_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        COALESCE(SUM(cached_tokens),0) AS total_cached_tokens,
        COALESCE(SUM(reasoning_tokens),0) AS total_reasoning_tokens,
        COALESCE(AVG(latency_ms),0) AS avg_latency_ms,
        COALESCE(SUM(num_images),0) AS total_images FROM requests{where}"""
    with _connect() as conn:
        d = dict(conn.execute(sql, params).fetchone())
        rng = conn.execute(f"SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM requests{where}", params).fetchone()
    d["first_ts"], d["last_ts"] = rng["first_ts"], rng["last_ts"]
    return d


def _get_latencies_sync(since_ts):
    where, params = _where(since_ts, None, None)
    q = f"SELECT latency_ms FROM requests{where} AND latency_ms IS NOT NULL" if where else \
        "SELECT latency_ms FROM requests WHERE latency_ms IS NOT NULL"
    with _connect() as conn:
        return [r["latency_ms"] for r in conn.execute(q, params).fetchall() if r["latency_ms"] is not None]


def _get_timeline_sync(since_ts, bucket):
    fmt = "%Y-%m-%dT%H:00" if bucket == "hour" else "%Y-%m-%d"
    where, params = _where(since_ts, None, None)
    sql = f"""SELECT strftime('{fmt}', ts) AS bucket, COUNT(*) AS requests,
        COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(total_tokens),0) AS tokens
        FROM requests{where} GROUP BY bucket ORDER BY bucket"""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _get_models_sync(since_ts):
    where, params = _where(since_ts, None, None)
    sql = f"""SELECT COALESCE(NULLIF(model_actual,''), model_requested,'unknown') AS model,
        COUNT(*) AS requests,
        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
        COALESCE(SUM(cost),0) AS total_cost,
        COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
        COALESCE(SUM(completion_tokens),0) AS completion_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        COALESCE(AVG(latency_ms),0) AS avg_latency_ms
        FROM requests{where} GROUP BY model ORDER BY total_cost DESC"""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _get_emails_sync(since_ts):
    where, params = _where(since_ts, None, None)
    sql = f"""SELECT COALESCE(NULLIF(email,''),'(без email)') AS email, COUNT(*) AS requests,
        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
        COALESCE(SUM(cost),0) AS total_cost, COALESCE(SUM(total_tokens),0) AS total_tokens,
        MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM requests{where} GROUP BY COALESCE(NULLIF(email,''),'(без email)') ORDER BY total_cost DESC"""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _get_email_balances_sync():
    """Последний известный баланс (ext_balance, $) и сумма списаний по каждому email."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT ext_email AS email, ext_balance AS balance, ext_datetime AS balance_ts, ts
            FROM external_reports e
            WHERE ext_email IS NOT NULL AND ext_email != '' AND ext_balance IS NOT NULL
              AND id = (SELECT id FROM external_reports e2
                        WHERE e2.ext_email = e.ext_email AND e2.ext_balance IS NOT NULL
                        ORDER BY id DESC LIMIT 1)
        """).fetchall()
        out = {r["email"]: {"balance": r["balance"],
                            "balance_ts": r["ext_datetime"] or r["ts"]} for r in rows}
        charged = conn.execute("""
            SELECT ext_email AS email, COALESCE(SUM(ext_charged),0) AS charged
            FROM external_reports
            WHERE ext_email IS NOT NULL AND ext_email != '' AND ext_charged IS NOT NULL
            GROUP BY ext_email
        """).fetchall()
        for r in charged:
            out.setdefault(r["email"], {})["charged"] = r["charged"]
    return out


def _get_user_report_sync(since_ts):
    """Пер-пользовательская сводка за период (для акта выполненных работ + Excel)."""
    where, params = _where(since_ts, None, None)
    sql = f"""SELECT COALESCE(NULLIF(email,''),'(без email)') AS email,
        COUNT(*) AS requests,
        SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
        SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
        COALESCE(SUM(cost),0) AS total_cost,
        COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
        COALESCE(SUM(completion_tokens),0) AS completion_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        COALESCE(SUM(num_images),0) AS total_images,
        MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM requests{where} GROUP BY COALESCE(NULLIF(email,''),'(без email)') ORDER BY total_cost DESC"""
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    balances = _get_email_balances_sync()
    for r in rows:
        b = balances.get(r["email"]) or {}
        r["balance"] = b.get("balance")
        r["charged"] = b.get("charged")
        r["balance_ts"] = b.get("balance_ts")
    return rows


def _get_requests_sync(limit, offset, status, model, email, since_ts):
    where, params = _where(since_ts, status, model, email)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM requests{where}", params).fetchone()["c"]
        rows = conn.execute(f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("schema_valid") is not None:
            d["schema_valid"] = bool(d["schema_valid"])
        items.append(d)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _get_external_summary_sync(since_ts):
    where, params = (" WHERE ts >= ?", [since_ts]) if since_ts else ("", [])
    with _connect() as conn:
        d = dict(conn.execute(f"""SELECT COUNT(*) AS reports,
            COALESCE(SUM(ext_charged),0) AS total_charged,
            SUM(CASE WHEN ext_ok=1 THEN 1 ELSE 0 END) AS sent_ok,
            SUM(CASE WHEN is_success=1 THEN 1 ELSE 0 END) AS success_reports
            FROM external_reports{where}""", params).fetchone())
        row = conn.execute(f"""SELECT ext_balance, ext_email, ext_datetime FROM
            external_reports{where} AND ext_balance IS NOT NULL ORDER BY id DESC LIMIT 1""", params).fetchone()
    d["latest_balance"] = dict(row) if row else None
    return d


def _get_external_reports_sync(limit, offset, since_ts):
    where, params = (" WHERE ts >= ?", [since_ts]) if since_ts else ("", [])
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM external_reports{where}", params).fetchone()["c"]
        rows = conn.execute(f"SELECT * FROM external_reports{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        for k in ("is_success", "ext_ok"):
            if d.get(k) is not None:
                d[k] = bool(d[k])
        items.append(d)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _get_latest_credit_sync():
    with _connect() as conn:
        row = conn.execute("SELECT * FROM credits ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---- async API ----
async def init_db(): await asyncio.to_thread(_init_db_sync)


async def log_request(record):
    try:
        return await asyncio.to_thread(_log_request_sync, record)
    except Exception:
        log.exception("log_request failed"); return None


async def save_credits(total, usage, remaining, raw=""):
    try:
        await asyncio.to_thread(_save_credits_sync, total, usage, remaining, raw)
    except Exception:
        log.exception("save_credits failed")


async def log_external_report(record):
    try:
        await asyncio.to_thread(_log_external_report_sync, record)
    except Exception:
        log.exception("log_external_report failed")


async def update_request_email(rowid, email):
    try:
        await asyncio.to_thread(_update_request_email_sync, rowid, email)
    except Exception:
        log.exception("update_request_email failed")


async def get_summary(since_ts): return await asyncio.to_thread(_get_summary_sync, since_ts)
async def get_latencies(since_ts): return await asyncio.to_thread(_get_latencies_sync, since_ts)
async def get_timeline(since_ts, bucket): return await asyncio.to_thread(_get_timeline_sync, since_ts, bucket)
async def get_models(since_ts): return await asyncio.to_thread(_get_models_sync, since_ts)
async def get_emails(since_ts): return await asyncio.to_thread(_get_emails_sync, since_ts)
async def get_email_balances(): return await asyncio.to_thread(_get_email_balances_sync)
async def get_user_report(since_ts): return await asyncio.to_thread(_get_user_report_sync, since_ts)
async def get_requests(limit, offset, status, model, email, since_ts): return await asyncio.to_thread(_get_requests_sync, limit, offset, status, model, email, since_ts)
async def get_external_summary(since_ts): return await asyncio.to_thread(_get_external_summary_sync, since_ts)
async def get_external_reports(limit, offset, since_ts): return await asyncio.to_thread(_get_external_reports_sync, limit, offset, since_ts)
async def get_latest_credit(): return await asyncio.to_thread(_get_latest_credit_sync)
