"""SQLite-слой сервера списания кредитов (aiosqlite).

Хранит:
- usage_records  — принятые от сниппетов записи (email, функция, модель, токены, кредиты)
- token_rates    — курсы конвертации токенов в кредиты по модели (PK = имя модели),
                   плюс зарезервированная строка "__default__" как fallback
- forward_log    — статус пересылки каждой записи на «другой сервер»
- settings       — ключ/значение (URL и ключ пересылки, и пр.)

Схема создаётся при старте (init_db), миграции — простые (CREATE TABLE IF NOT EXISTS).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

# Зарезервированное имя «модели» — курс по умолчанию, если модель не задана в GUI.
DEFAULT_RATE_KEY = "__default__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT NOT NULL,
    function          TEXT NOT NULL,
    model             TEXT NOT NULL,
    op_timestamp      TEXT NOT NULL,
    tokens            INTEGER NOT NULL,          -- total tokens (legacy / fallback)
    prompt_tokens     INTEGER,                   -- breakdown: input (nullable)
    completion_tokens INTEGER,                   -- breakdown: output (nullable)
    rate_applied      REAL NOT NULL,             -- единый курс (для отображения)
    rate_input        REAL,                      -- раздельный курс input (nullable)
    rate_output       REAL,                      -- раздельный курс output (nullable)
    credits           REAL NOT NULL,
    cost_usd          REAL,                      -- стоимость в долларах (nullable)
    cost_source       TEXT,                      -- откуда $: "provider" | "computed" | NULL
    user_id           TEXT,                      -- openRouterWebUiUserId из Open WebUI (nullable)
    request_date      TEXT,                      -- ISO-время старта запроса к модели (nullable)
    response_date     TEXT,                      -- ISO-время получения ответа модели (nullable)
    is_success        INTEGER NOT NULL DEFAULT 1,-- флаг успеха вызова модели (0/1)
    error_message     TEXT,                      -- текст ошибки модели или NULL
    received_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_email     ON usage_records(email);
CREATE INDEX IF NOT EXISTS idx_usage_function  ON usage_records(function);
CREATE INDEX IF NOT EXISTS idx_usage_model     ON usage_records(model);
CREATE INDEX IF NOT EXISTS idx_usage_optime    ON usage_records(op_timestamp);

CREATE TABLE IF NOT EXISTS token_rates (
    model_name              TEXT PRIMARY KEY,
    credits_per_1k_tokens   REAL NOT NULL,       -- единый курс (всегда)
    credits_per_1k_input    REAL,                -- раздельный курс input (nullable)
    credits_per_1k_output   REAL,                -- раздельный курс output (nullable)
    cost_per_1m_input_usd   REAL,                -- $ за 1М input-токенов (nullable)
    cost_per_1m_output_usd  REAL,                -- $ за 1М output-токенов (nullable)
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_record_id  INTEGER NOT NULL,
    status           TEXT NOT NULL,         -- pending / ok / failed
    http_status      INTEGER,               -- код ответа или NULL
    error            TEXT,                  -- текст ошибки или NULL
    attempts         INTEGER NOT NULL DEFAULT 0,
    sent_at          TEXT,                  -- время последней попытки
    resp_datetime    TEXT,                  -- datetime из ответа внешнего сервера
    resp_email       TEXT,                  -- email из ответа (сверка)
    resp_charged     REAL,                  -- charged из ответа (списано кредитов)
    resp_balance     REAL,                  -- balance из ответа (остаток пользователя)
    resp_raw         TEXT,                  -- сырой JSON-ответ (обрезанный, для аудита)
    FOREIGN KEY (usage_record_id) REFERENCES usage_records(id)
);
CREATE INDEX IF NOT EXISTS idx_forward_status ON forward_log(status);

CREATE TABLE IF NOT EXISTS user_balances (
    email        TEXT PRIMARY KEY,
    balance      REAL,                      -- последний известный остаток
    last_charged REAL,                      -- последнее списание
    last_updated TEXT,                      -- datetime из ответа внешнего сервера
    forward_id   INTEGER,                   -- какая запись forward_log обновила баланс
    updated_at   TEXT NOT NULL              -- локальное время последнего upsert (UTC)
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

# Колонки, добавляемые миграцией к уже существующим таблицам (старые БД).
# SQLite не имеет IF NOT EXISTS для ADD COLUMN, поэтому ошибки "duplicate column"
# подавляем в _migrate ниже. Каждая запись: (table, column, sqlite-type-decl).
MIGRATION_COLUMNS = [
    ("usage_records", "prompt_tokens",     "INTEGER"),
    ("usage_records", "completion_tokens", "INTEGER"),
    ("usage_records", "rate_input",        "REAL"),
    ("usage_records", "rate_output",       "REAL"),
    ("usage_records", "cost_usd",          "REAL"),
    ("usage_records", "cost_source",       "TEXT"),
    ("usage_records", "user_id",           "TEXT"),
    ("usage_records", "request_date",      "TEXT"),
    ("usage_records", "response_date",     "TEXT"),
    # is_success — NOT NULL DEFAULT 1: старые записи (до миграции) считаются успешными.
    ("usage_records", "is_success",        "INTEGER NOT NULL DEFAULT 1"),
    ("usage_records", "error_message",     "TEXT"),
    ("forward_log", "resp_datetime", "TEXT"),
    ("forward_log", "resp_email",    "TEXT"),
    ("forward_log", "resp_charged",  "REAL"),
    ("forward_log", "resp_balance",  "REAL"),
    ("forward_log", "resp_raw",      "TEXT"),
    ("token_rates",   "credits_per_1k_input",  "REAL"),
    ("token_rates",   "credits_per_1k_output", "REAL"),
    ("token_rates",   "cost_per_1m_input_usd",  "REAL"),
    ("token_rates",   "cost_per_1m_output_usd", "REAL"),
]

# Значения по умолчанию для settings, если строки ещё нет.
# Могут быть переопределены значениями из .env при init_db(..., env_overrides=...).
DEFAULT_SETTINGS: dict[str, str] = {
    "forward_url": "",
    "forward_api_key": "",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_db(db_path: str, *, env_overrides: Optional[dict[str, str]] = None) -> None:
    """Создать схему и заполнить дефолты. Вызывается один раз при старте.

    env_overrides: значения из .env (forward_url, forward_api_key). Логика:
      - БД свежая          -> пишем значение из .env (как стартовое);
      - в БД уже задано     -> НЕ перетираем (ручная настройка через GUI важнее).
      - в БД пусто, в .env нет -> остаётся дефолт DEFAULT_SETTINGS.
    Это даёт удобное поведение: .env разворачивает начальную конфигурацию,
    а дальнейшие правки через GUI сохраняются между перезапусками.
    """
    env_overrides = env_overrides or {}

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)

        # Миграция старых БД: добавляем новые nullable-колонки, если их нет.
        for table, column, decl in MIGRATION_COLUMNS:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except Exception:
                pass  # колонка уже существует — это нормально

        # Курс по умолчанию — 1 кредит за 1000 токенов, если ещё не задан.
        await db.execute(
            "INSERT OR IGNORE INTO token_rates(model_name, credits_per_1k_tokens, updated_at) "
            "VALUES (?, ?, ?)",
            (DEFAULT_RATE_KEY, 1.0, _now_iso()),
        )
        # Дефолтные настройки (INSERT OR IGNORE — не трогает существующие).
        for k, v in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v)
            )
        # Применяем .env-переопределения только в пустые поля.
        for k, v in env_overrides.items():
            if not v:
                continue
            await db.execute(
                "UPDATE settings SET value = ? "
                "WHERE key = ? AND (value IS NULL OR value = '')",
                (v, k),
            )
        await db.commit()


async def insert_usage(
    db_path: str,
    *,
    email: str,
    function: str,
    model: str,
    op_timestamp: str,
    tokens: int,
    rate_applied: float,
    credits: float,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    rate_input: Optional[float] = None,
    rate_output: Optional[float] = None,
    cost_usd: Optional[float] = None,
    cost_source: Optional[str] = None,
    user_id: Optional[str] = None,
    request_date: Optional[str] = None,
    response_date: Optional[str] = None,
    is_success: bool = True,
    error_message: Optional[str] = None,
) -> int:
    """Записать факт использования и вернуть id новой строки.

    prompt_tokens/completion_tokens и rate_input/rate_output — опциональны и
    используются только при раздельной тарификации input/output.
    cost_usd/cost_source — реальная стоимость в долларах и её источник
    ("provider" — отдал провайдер; "computed" — посчитали по тарифам $/млн).
    user_id — openRouterWebUiUserId из Open WebUI (пробрасывается в пересылку
    на devbim.com как openRouterWebUiUserId).
    request_date/response_date — время старта и завершения вызова модели.
    is_success/error_message — результат вызова модели (для errorMessage в пересылке).
    """
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO usage_records"
            " (email, function, model, op_timestamp, tokens, "
            "  prompt_tokens, completion_tokens, "
            "  rate_applied, rate_input, rate_output, credits, "
            "  cost_usd, cost_source, "
            "  user_id, request_date, response_date, is_success, error_message, "
            "  received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, function, model, op_timestamp, tokens,
             prompt_tokens, completion_tokens,
             rate_applied, rate_input, rate_output, credits,
             cost_usd, cost_source,
             user_id, request_date, response_date, 1 if is_success else 0, error_message,
             _now_iso()),
        )
        await db.commit()
        return cur.lastrowid


async def create_forward_entry(db_path: str, usage_record_id: int) -> int:
    """Создать запись forward_log в статусе pending и вернуть её id."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO forward_log(usage_record_id, status, attempts) VALUES (?, 'pending', 0)",
            (usage_record_id,),
        )
        await db.commit()
        return cur.lastrowid


async def get_rate(db_path: str, model: str) -> Optional[dict[str, Any]]:
    """Вернуть курс(ы) для модели, иначе для __default__, иначе None.

    Возвращаемый словарь:
        base   -> credits_per_1k_tokens  (всегда)
        input  -> credits_per_1k_input   (float | None)
        output -> credits_per_1k_output  (float | None)
        cost_in  -> cost_per_1m_input_usd   (float | None) — $ за 1М input
        cost_out -> cost_per_1m_output_usd  (float | None) — $ за 1М output
        matched_model -> по какому ключу найден курс
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT credits_per_1k_tokens, credits_per_1k_input, credits_per_1k_output, "
            "       cost_per_1m_input_usd, cost_per_1m_output_usd "
            "FROM token_rates WHERE model_name = ?",
            (model,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            async with db.execute(
                "SELECT credits_per_1k_tokens, credits_per_1k_input, credits_per_1k_output, "
                "       cost_per_1m_input_usd, cost_per_1m_output_usd "
                "FROM token_rates WHERE model_name = ?",
                (DEFAULT_RATE_KEY,),
            ) as cur:
                row = await cur.fetchone()
            matched = DEFAULT_RATE_KEY
        else:
            matched = model

        if row is None:
            return None
        return {
            "base": float(row[0]),
            "input": float(row[1]) if row[1] is not None else None,
            "output": float(row[2]) if row[2] is not None else None,
            "cost_in": float(row[3]) if row[3] is not None else None,
            "cost_out": float(row[4]) if row[4] is not None else None,
            "matched_model": matched,
        }


# --- Управление курсами (для GUI) -----------------------------------------

async def list_rates(db_path: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT model_name, credits_per_1k_tokens, credits_per_1k_input, "
            "       credits_per_1k_output, cost_per_1m_input_usd, cost_per_1m_output_usd, "
            "       updated_at "
            "FROM token_rates ORDER BY "
            "  CASE WHEN model_name = ? THEN 0 ELSE 1 END, model_name",
            (DEFAULT_RATE_KEY,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def upsert_rate(
    db_path: str,
    model_name: str,
    credits_per_1k: float,
    *,
    credits_per_1k_input: Optional[float] = None,
    credits_per_1k_output: Optional[float] = None,
    cost_per_1m_input_usd: Optional[float] = None,
    cost_per_1m_output_usd: Optional[float] = None,
) -> None:
    """Создать/обновить курс.

    credits_per_1k_input/output — раздельные курсы в кредитах (NULL = выключено).
    cost_per_1m_input_usd/output — тарифы в $ за 1М токенов (NULL = не считать $).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO token_rates"
            " (model_name, credits_per_1k_tokens, credits_per_1k_input, credits_per_1k_output, "
            "  cost_per_1m_input_usd, cost_per_1m_output_usd, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(model_name) DO UPDATE SET "
            "  credits_per_1k_tokens=excluded.credits_per_1k_tokens, "
            "  credits_per_1k_input=excluded.credits_per_1k_input, "
            "  credits_per_1k_output=excluded.credits_per_1k_output, "
            "  cost_per_1m_input_usd=excluded.cost_per_1m_input_usd, "
            "  cost_per_1m_output_usd=excluded.cost_per_1m_output_usd, "
            "  updated_at=excluded.updated_at",
            (model_name, float(credits_per_1k),
             credits_per_1k_input, credits_per_1k_output,
             cost_per_1m_input_usd, cost_per_1m_output_usd, _now_iso()),
        )
        await db.commit()


async def delete_rate(db_path: str, model_name: str) -> None:
    if model_name == DEFAULT_RATE_KEY:
        # Удалять курс по умолчанию нельзя — сломает конвертер.
        raise ValueError("Нельзя удалить курс по умолчанию (__default__).")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "DELETE FROM token_rates WHERE model_name = ?", (model_name,)
        )
        await db.commit()


# --- Записи использования и статистика (для GUI) --------------------------

async def list_records(
    db_path: str,
    *,
    limit: int = 50,
    offset: int = 0,
    email: Optional[str] = None,
    function: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Лог использования с пагинацией и фильтром. Возвращает (rows, total)."""
    where: list[str] = []
    params: list[Any] = []
    if email:
        where.append("email LIKE ?")
        params.append(f"%{email}%")
    if function:
        where.append("function LIKE ?")
        params.append(f"%{function}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) FROM usage_records {where_sql}", params
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            f"SELECT * FROM usage_records {where_sql} "
            "ORDER BY received_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return rows, total


async def get_stats(
    db_path: str, *, group_by: str = "email"
) -> list[dict[str, Any]]:
    """Агрегаты по выбранному измерению: сумма токенов, кредитов и $."""
    allowed = {"email", "function", "model"}
    if group_by not in allowed:
        raise ValueError(f"group_by должен быть одним из {allowed}")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {group_by} AS dim, "
            "       COUNT(*) AS calls, "
            "       SUM(tokens) AS tokens, "
            "       SUM(credits) AS credits, "
            "       SUM(cost_usd) AS cost_usd "
            "FROM usage_records GROUP BY {group_by} "
            "ORDER BY credits DESC".replace("{group_by}", group_by),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_summary(db_path: str) -> dict[str, Any]:
    """Сводные итоги по всем записям: всего $, кредитов, вызовов."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(credits), 0) AS credits, "
            "       COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM usage_records"
        ) as cur:
            row = await cur.fetchone()
        return {
            "total_calls": row[0] if row else 0,
            "total_credits": float(row[1]) if row else 0.0,
            "total_cost_usd": float(row[2]) if row else 0.0,
        }


# --- Forward log -----------------------------------------------------------

async def get_forward_entry(db_path: str, forward_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM forward_log WHERE id = ?", (forward_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_forward_entry(
    db_path: str,
    forward_id: int,
    *,
    status: str,
    http_status: Optional[int] = None,
    error: Optional[str] = None,
    resp_datetime: Optional[str] = None,
    resp_email: Optional[str] = None,
    resp_charged: Optional[float] = None,
    resp_balance: Optional[float] = None,
    resp_raw: Optional[str] = None,
) -> None:
    """Отметить результат попытки пересылки (ok/failed).

    resp_* — поля ответа внешнего сервера (заполняются только при 2xx с JSON-телом).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE forward_log SET status = ?, http_status = ?, error = ?, "
            "attempts = attempts + 1, sent_at = ?, "
            "resp_datetime = ?, resp_email = ?, resp_charged = ?, "
            "resp_balance = ?, resp_raw = ? WHERE id = ?",
            (status, http_status, error, _now_iso(),
             resp_datetime, resp_email, resp_charged,
             resp_balance, resp_raw, forward_id),
        )
        await db.commit()


async def list_forward(
    db_path: str, *, status: Optional[str] = None, limit: int = 50
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if status:
            q = (
                "SELECT f.*, u.email, u.function, u.model, u.tokens, u.op_timestamp "
                "FROM forward_log f JOIN usage_records u ON u.id = f.usage_record_id "
                "WHERE f.status = ? ORDER BY f.id DESC LIMIT ?"
            )
            params: list[Any] = [status, limit]
        else:
            q = (
                "SELECT f.*, u.email, u.function, u.model, u.tokens, u.op_timestamp "
                "FROM forward_log f JOIN usage_records u ON u.id = f.usage_record_id "
                "ORDER BY f.id DESC LIMIT ?"
            )
            params = [limit]
        async with db.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_usage_for_forward(db_path: str, usage_record_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT email, function, model, op_timestamp, tokens, "
            "       prompt_tokens, completion_tokens, cost_usd, "
            "       user_id, request_date, response_date, is_success, error_message "
            "FROM usage_records WHERE id = ?",
            (usage_record_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_balance(
    db_path: str,
    *,
    email: str,
    balance: float,
    last_charged: Optional[float] = None,
    last_updated: Optional[str] = None,
    forward_id: Optional[int] = None,
) -> None:
    """Записать/обновить последний известный баланс пользователя.

    last_updated — datetime из ответа внешнего сервера (может быть None);
    updated_at   — локальное время upsert (всегда задаётся здесь).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO user_balances"
            " (email, balance, last_charged, last_updated, forward_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "  balance=excluded.balance, "
            "  last_charged=excluded.last_charged, "
            "  last_updated=excluded.last_updated, "
            "  forward_id=excluded.forward_id, "
            "  updated_at=excluded.updated_at",
            (email, float(balance), last_charged, last_updated, forward_id, _now_iso()),
        )
        await db.commit()


async def list_balances(db_path: str) -> list[dict[str, Any]]:
    """Реестр актуальных балансов пользователей, отсортированный по email."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT email, balance, last_charged, last_updated, forward_id, updated_at "
            "FROM user_balances ORDER BY email"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# --- Settings --------------------------------------------------------------

async def get_setting(db_path: str, key: str, default: str = "") -> str:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(db_path: str, key: str, value: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_all_settings(db_path: str) -> dict[str, str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            return {k: v for k, v in await cur.fetchall()}


# Защита от случайной передачи sqlite3-объекта туда, где ждали значение.
def _coerce(v: Any) -> Any:
    return v if not isinstance(v, sqlite3.Row) else dict(v)
