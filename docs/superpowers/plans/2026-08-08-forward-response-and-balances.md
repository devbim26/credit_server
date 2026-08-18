# Приём ответа внешнего сервера + реестр балансов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Принимать от внешнего сервера ответ `{datetime, email, charged, balance}` на каждый POST пересылки, сохранять его и вести реестр актуальных балансов по email — с показом в GUI.

**Architecture:** `forwarder.py` парсит тело 2xx-ответа, сохраняет поля в новые nullable-колонки `forward_log` (`resp_*`) и upsert-ит запись в новую таблицу `user_balances`. `server.py` отдаёт реестр через `GET /api/balances`; GUI добавляет колонки во вкладку «Пересылка» и новую вкладку «Балансы». Mock внешнего сервера приводится к контракту.

**Tech Stack:** Python 3, FastAPI, aiosqlite, httpx, Jinja2, HTML/Tailwind CDN. Без pytest — тестирование через E2E mock-серверы `test_pipe/` и мини-скрипты.

**Спека:** `docs/superpowers/specs/2026-08-08-forward-response-and-balances-design.md`

## Global Constraints

- **Контракт ответа внешнего сервера** — `200 OK` с JSON `{datetime, email, charged, balance}`. Доп. поля игнорировать. Отсутствующие/`null` → `NULL`. `balance: 0` валиден. Любой статус `≥ 400` → `failed`. Не-JSON/пустое 2xx-тело → `ok`, поля `NULL`, сервер не падает.
- **Реестр `user_balances` обновляется только при валидном (не null) `balance`.** Email для upsert берётся из `usage_records` (надёжно), не из `resp_email` (он справочный).
- **Миграция БД** — через существующий механизм `MIGRATION_COLUMNS` (ALTER TABLE, подавлять `duplicate column`) + `CREATE TABLE IF NOT EXISTS`. Старые БД не ломаются.
- **Безопасность** — `GET /api/balances` под Bearer `ADMIN_KEY` (как все `/api/*`).
- **Без pytest в проекте** — каждый шаг проверки это либо короткий inline-скрипт (`python -c "..."` / временный `.py`), либо E2E через моки. Не добавлять pytest в requirements.
- **Кодировка файлов UTF-8, отступы 4 пробела, тип — match окружающего кода.** Все `_log()`-функции следуют существующему паттерну (`[module] {UTC-ts} {msg}`).
- **Коммиты частые** — после каждой задачи.

---

## File Structure

| Файл | Ответственность | Изменение |
|---|---|---|
| `db.py` | Схема/миграции + доступ к `forward_log`, `user_balances` | Modify |
| `forwarder.py` | Парсинг ответа, сохранение `resp_*`, upsert баланса | Modify |
| `server.py` | `GET /api/balances` | Modify |
| `templates/admin.html` | Колонки в «Пересылке» + вкладка «Балансы» | Modify |
| `test_pipe/mock_external_server.py` | Контрактный ответ + in-memory балансы | Modify |
| `README.md` | Контракт ответа, вкладка «Балансы», `GET /api/balances` | Modify |

Декомпозиция: каждый слой (БД → пересылка → API → UI → mock → docs) — отдельная задача со своим циклом проверки. Граница выбрана так, что рецензент может одобрить БД-слой, отклонив UI-слой, и vice versa.

---

## Task 1: Схема БД — колонки `forward_log` + таблица `user_balances`

**Files:**
- Modify: `db.py` (блок `SCHEMA` ~стр.23, `MIGRATION_COLUMNS` ~стр.77, `update_forward_entry` ~стр.390, добавить новые функции в конец блока Forward log ~стр.428)

**Interfaces:**
- Consumes: ничего (базовый слой)
- Produces:
  - Колонки `forward_log`: `resp_datetime TEXT`, `resp_email TEXT`, `resp_charged REAL`, `resp_balance REAL`, `resp_raw TEXT` (создаются миграцией у старых БД; входят в `SCHEMA` для свежих).
  - Таблица `user_balances(email PK, balance REAL, last_charged REAL, last_updated TEXT, forward_id INTEGER, updated_at TEXT NOT NULL)`.
  - `update_forward_entry(db_path, forward_id, *, status, http_status=None, error=None, resp_datetime=None, resp_email=None, resp_charged=None, resp_balance=None, resp_raw=None) -> None` — расширенная сигнатура (старые вызовы остаются рабочими: новые параметры keyword-only со значениями по умолчанию).
  - `upsert_balance(db_path, *, email, balance, last_charged=None, last_updated=None, forward_id=None) -> None`
  - `list_balances(db_path) -> list[dict]` → колонки `email, balance, last_charged, last_updated, forward_id, updated_at`, отсортированы по `email`.

- [ ] **Step 1: Добавить колонки в SCHEMA (для свежих БД)**

В `db.py`, в блоке `CREATE TABLE IF NOT EXISTS forward_log (...)` добавь 5 колонок перед закрывающей скобкой. Найди:

```python
    sent_at          TEXT,                  -- время последней попытки
    FOREIGN KEY (usage_record_id) REFERENCES usage_records(id)
);
```

Замени на:

```python
    sent_at          TEXT,                  -- время последней попытки
    resp_datetime    TEXT,                  -- datetime из ответа внешнего сервера
    resp_email       TEXT,                  -- email из ответа (сверка)
    resp_charged     REAL,                  -- charged из ответа (списано кредитов)
    resp_balance     REAL,                  -- balance из ответа (остаток пользователя)
    resp_raw         TEXT,                  -- сырой JSON-ответ (обрезанный, для аудита)
    FOREIGN KEY (usage_record_id) REFERENCES usage_records(id)
);
```

- [ ] **Step 2: Добавить таблицу user_balances в SCHEMA**

В `db.py`, сразу после блока `forward_log` (после строки `CREATE INDEX IF NOT EXISTS idx_forward_status ON forward_log(status);`) и перед `CREATE TABLE IF NOT EXISTS settings`, вставь:

```python
CREATE TABLE IF NOT EXISTS user_balances (
    email        TEXT PRIMARY KEY,
    balance      REAL,                      -- последний известный остаток
    last_charged REAL,                      -- последнее списание
    last_updated TEXT,                      -- datetime из ответа внешнего сервера
    forward_id   INTEGER,                   -- какая запись forward_log обновила баланс
    updated_at   TEXT NOT NULL              -- локальное время последнего upsert (UTC)
);
```

- [ ] **Step 3: Добавить новые колонки в MIGRATION_COLUMNS (для старых БД)**

В `db.py`, в список `MIGRATION_COLUMNS` (после строки `("usage_records", "cost_source", "TEXT"),`) добавь 5 записей:

```python
    ("forward_log", "resp_datetime", "TEXT"),
    ("forward_log", "resp_email",    "TEXT"),
    ("forward_log", "resp_charged",  "REAL"),
    ("forward_log", "resp_balance",  "REAL"),
    ("forward_log", "resp_raw",      "TEXT"),
```

- [ ] **Step 4: Расширить update_forward_entry**

В `db.py` найди функцию `update_forward_entry` (примерно стр.390) и замени целиком:

```python
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
```

- [ ] **Step 5: Добавить upsert_balance и list_balances**

В `db.py`, в конец блока `# --- Forward log -----------------------------------------------------------` (сразу после функции `get_usage_for_forward`, перед `# --- Settings ---`), добавь:

```python
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
```

- [ ] **Step 6: Проверить миграцию инлайн-скриптом**

Сохрани текущую `credits.db` (если жалко), затем прогони миграцию на копии и проверь, что колонки и таблица появились. Из корня проекта:

```bash
cp credits.db credits.db.bak 2>/dev/null || true
python -c "
import asyncio, aiosqlite, db
async def main():
    await db.init_db('credits.db')
    async with aiosqlite.connect('credits.db') as c:
        async with c.execute('PRAGMA table_info(forward_log)') as cur:
            cols = [r[1] for r in await cur.fetchall()]
        async with c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='user_balances'\") as cur:
            t = await cur.fetchall()
    print('forward_log cols:', [c for c in cols if c.startswith('resp_')])
    print('user_balances table exists:', bool(t))
    assert 'resp_balance' in cols and 'resp_raw' in cols
    assert t, 'user_balances not created'
    print('OK')
asyncio.run(main())
"
```

Expected: `forward_log cols: ['resp_datetime', 'resp_email', 'resp_charged', 'resp_balance', 'resp_raw']` / `user_balances table exists: True` / `OK`.

- [ ] **Step 7: Проверить свежую БД (с нуля)**

```bash
python -c "
import asyncio, os, aiosqlite, db
p='_tmp_fresh.db'
if os.path.exists(p): os.remove(p)
async def main():
    await db.init_db(p)
    async with aiosqlite.connect(p) as c:
        async with c.execute('PRAGMA table_info(forward_log)') as cur:
            cols=[r[1] for r in await cur.fetchall()]
        async with c.execute('PRAGMA table_info(user_balances)') as cur:
            bcols=[r[1] for r in await cur.fetchall()]
    print('fresh forward_log resp_*:', [c for c in cols if c.startswith('resp_')])
    print('fresh user_balances:', bcols)
    assert {'resp_datetime','resp_email','resp_charged','resp_balance','resp_raw'} <= set(cols)
    assert {'email','balance','last_charged','last_updated','forward_id','updated_at'} <= set(bcols)
    print('OK')
asyncio.run(main())
os.remove(p)
"
```

Expected: оба списка содержат все поля, `OK`.

- [ ] **Step 8: Проверить upsert_balance через инлайн-скрипт**

```bash
python -c "
import asyncio, os, db
p='_tmp_bal.db'
if os.path.exists(p): os.remove(p)
async def main():
    await db.init_db(p)
    await db.upsert_balance(p, email='a@x.com', balance=950, last_charged=50, forward_id=1)
    await db.upsert_balance(p, email='a@x.com', balance=900, last_charged=50, forward_id=2)  # update
    rows = await db.list_balances(p)
    print(rows)
    assert len(rows)==1 and rows[0]['email']=='a@x.com' and rows[0]['balance']==900 and rows[0]['forward_id']==2
    print('OK')
asyncio.run(main())
os.remove(p)
"
```

Expected: одна запись `a@x.com` с `balance=900, forward_id=2`, затем `OK`.

- [ ] **Step 9: Коммит**

```bash
git add db.py
git commit -m "feat(db): колонки ответа forward_log + таблица user_balances"
```

---

## Task 2: Парсинг ответа в forwarder.py

**Files:**
- Modify: `forwarder.py` (функция `_send_one` ~стр.56)

**Interfaces:**
- Consumes (from Task 1):
  - `db.update_forward_entry(..., resp_datetime, resp_email, resp_charged, resp_balance, resp_raw)`
  - `db.upsert_balance(*, email, balance, last_charged, last_updated, forward_id)`
  - `db.get_usage_for_forward(db_path, usage_record_id)` — уже есть, возвращает `email` (используется для upsert).
- Produces: ничего нового (внутренняя логика пересылки).

- [ ] **Step 1: Расширить _send_one — парсинг 2xx-ответа**

В `forwarder.py` найди функцию `_send_one`. Найди блок обработки успеха:

```python
        if resp.status_code < 400:
            await db.update_forward_entry(
                db_path, entry["id"], status="ok",
                http_status=resp.status_code, error=None,
            )
            _log(
                f"OK usage={entry['usage_record_id']} http={resp.status_code} "
                f"email={usage['email']!r}"
            )
```

Замени на:

```python
        if resp.status_code < 400:
            # Парсим тело ответа внешнего сервера по контракту
            # {datetime, email, charged, balance}. Любая ошибка разбора —
            # НЕ роняем пересылку: поля ответа остаются NULL.
            resp_datetime = resp_email = None
            resp_charged = resp_balance = None
            resp_raw = (resp.text or "")[:1024]
            try:
                body = resp.json()
                if isinstance(body, dict):
                    resp_datetime = body.get("datetime")
                    resp_email = body.get("email")
                    resp_charged = body.get("charged")
                    resp_balance = body.get("balance")
            except Exception:
                body = None

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
            # из resp_email (справочное поле ответа).
            if resp_balance is not None:
                await db.upsert_balance(
                    db_path,
                    email=usage["email"],
                    balance=float(resp_balance),
                    last_charged=(
                        float(resp_charged) if resp_charged is not None else None
                    ),
                    last_updated=resp_datetime,
                    forward_id=entry["id"],
                )

            _log(
                f"OK usage={entry['usage_record_id']} http={resp.status_code} "
                f"email={usage['email']!r} "
                f"charged={resp_charged} balance={resp_balance}"
            )
```

> Примечание: `resp_raw` сохраняем только если тело распарсилось как JSON (`body is not None`) — для не-JSON 2xx оставляем NULL (мусор в аудите ни к чему). Остальные `resp_*` поля при не-JSON тоже NULL.

- [ ] **Step 2: Импорт_db (проверить наличие)**

`forwarder.py` уже начинается с `import db` (стр.18). Ничего добавлять не надо. Проверь, что строки `import asyncio`, `from datetime import datetime, timezone`, `import httpx`, `import db` присутствуют — это всё уже есть.

- [ ] **Step 3: Синтакcис-проверка**

```bash
python -c "import ast; ast.parse(open('forwarder.py', encoding='utf-8').read()); print('syntax OK')"
```

Expected: `syntax OK`.

- [ ] **Step 4: Сквозной E2E: пересылка → парсинг ответа → баланс**

Запусти три процесса (в отдельных терминалах или фоне). Проверь, что пересылка доходит, ответ парсится, баланс записывается.

4a. Подними mock внешнего сервера в фоне (временно оставим старый контракт — он вернёт `{ok, saved, got}`, без charged/balance; это проверит толерантность к «не-контрактному» ответу):

```bash
cd test_pipe && python mock_external_server.py &
MOCK_PID=$!
```

4b. Подними наш сервер с тестовым .env:

```bash
python server.py &
SRV_PID=$!
sleep 2
```

4c. Отправь отчёт через `/api/usage` и дай пересылке отработать:

```bash
python -c "
import asyncio, httpx
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post('http://localhost:4010/api/usage',
            headers={'Authorization':'Bearer devbim2026'},
            json={'email':'e2e@x.com','function':'T','model':'test-model','tokens':1234,'prompt_tokens':1000,'completion_tokens':234})
        print('usage:', r.status_code, r.json())
asyncio.run(main())
"
sleep 2
```

Expected: `usage: 200 {...}` с `credits`.

4d. Проверь forward_log и балансы напрямую в БД:

```bash
python -c "
import asyncio, aiosqlite, db
async def main():
    fwd = await db.list_forward('credits.db', limit=5)
    bal = await db.list_balances('credits.db')
    print('last forward:', fwd[0] if fwd else None)
    print('balances:', bal)
asyncio.run(main())
"
```

Expected: последняя запись `forward_log` в `status='ok'`, `http_status=200`, `resp_*` = NULL (старый mock не отдаёт charged/balance). `balances` пустой список `[]` (баланс не обновился — корректно, т.к. resp_balance=None).

> Это проверяет толерантность. Полный контракт проверяется в Task 5 после обновления mock.

4e. Останови процессы:

```bash
kill $SRV_PID $MOCK_PID 2>/dev/null
```

- [ ] **Step 5: Коммит**

```bash
git add forwarder.py
git commit -m "feat(forwarder): парсинг ответа внешнего сервера + обновление баланса"
```

---

## Task 3: API — GET /api/balances

**Files:**
- Modify: `server.py` (добавить эндпоинт после блока `# GUI API: очередь пересылки` ~стр.334, рядом с `/api/forward`)

**Interfaces:**
- Consumes (from Task 1): `db.list_balances(db_path) -> list[dict]`
- Produces: HTTP-эндпоинт `GET /api/balances` → `{rows: [...]}` под Bearer `ADMIN_KEY`.

- [ ] **Step 1: Добавить эндпоинт**

В `server.py`, после функции `api_retry_failed` (конец блока «GUI API: очередь пересылки», примерно стр.351) и перед блоком `# GUI API: настройки`, вставь:

```python
# --------------------------------------------------------------------------- #
# GUI API: реестр балансов
# --------------------------------------------------------------------------- #
@app.get("/api/balances")
async def api_list_balances(request: Request):
    """Актуальные балансы пользователей из ответов внешнего сервера."""
    _check_bearer(request, ADMIN_KEY, what="ADMIN")
    return {"rows": await db.list_balances(DB_PATH)}
```

- [ ] **Step 2: Синтакcис-проверка**

```bash
python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read()); print('syntax OK')"
```

Expected: `syntax OK`.

- [ ] **Step 3: Проверка эндпоинта (401 без ключа / 200 с ключом)**

```bash
python server.py &
SRV_PID=$!
sleep 2
echo "--- без ключа (ожидаем 401) ---"
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4010/api/balances
echo "--- с ключом (ожидаем 200 + rows) ---"
curl -s http://localhost:4010/api/balances -H "Authorization: Bearer devbim2026"
echo
kill $SRV_PID 2>/dev/null
```

Expected: `401`, затем `200` с телом `{"rows":[...]}`.

- [ ] **Step 4: Коммит**

```bash
git add server.py
git commit -m "feat(api): GET /api/balances — реестр балансов"
```

---

## Task 4: GUI — колонки в «Пересылке» + вкладка «Балансы»

**Files:**
- Modify: `templates/admin.html` (кнопка таба ~стр.51, таблица «Пересылка» ~стр.205, новая вкладка ~стр.223, JS `switchTab` ~стр.295, JS `loadForward` ~стр.472, добавить `loadBalances`)

**Interfaces:**
- Consumes: `GET /api/forward` (уже возвращает `f.*` → поля `resp_*` придут автоматически), `GET /api/balances` (from Task 3).
- Produces: UI-изменения в браузере.

> Шаблон правится «на лету» — без перезапуска сервера (это уже задокументировано в README troubleshooting). После правки достаточно F5.

- [ ] **Step 1: Добавить кнопку вкладки «Балансы»**

Найди строку (≈стр.51):

```html
    <button data-tab="forward" class="tab-btn px-4 py-2 rounded-lg bg-white border">Пересылка</button>
```

Сразу после неё добавь:

```html
    <button data-tab="balances" class="tab-btn px-4 py-2 rounded-lg bg-white border">Балансы</button>
```

- [ ] **Step 2: Добавить колонки ответа в таблицу «Пересылка»**

Найди шапку таблицы пересылки (≈стр.207-218) и замени весь блок `<thead>...</thead>`:

Было:

```html
        <thead class="bg-slate-50 text-slate-500 uppercase text-xs">
          <tr>
            <th class="text-left px-4 py-2">Статус</th>
            <th class="text-left px-4 py-2">Email</th>
            <th class="text-left px-4 py-2">Функция</th>
            <th class="text-left px-4 py-2">Модель</th>
            <th class="text-right px-4 py-2">Токены</th>
            <th class="text-right px-4 py-2">$</th>
            <th class="text-left px-4 py-2">HTTP</th>
            <th class="text-left px-4 py-2">Попытки</th>
            <th class="text-left px-4 py-2">Ошибка</th>
          </tr>
        </thead>
```

Стало:

```html
        <thead class="bg-slate-50 text-slate-500 uppercase text-xs">
          <tr>
            <th class="text-left px-4 py-2">Статус</th>
            <th class="text-left px-4 py-2">Email</th>
            <th class="text-left px-4 py-2">Функция</th>
            <th class="text-left px-4 py-2">Модель</th>
            <th class="text-right px-4 py-2">Токены</th>
            <th class="text-right px-4 py-2">$</th>
            <th class="text-right px-4 py-2">Списано</th>
            <th class="text-right px-4 py-2">Остаток</th>
            <th class="text-left px-4 py-2">Время ответа</th>
            <th class="text-left px-4 py-2">HTTP</th>
            <th class="text-left px-4 py-2">Попытки</th>
            <th class="text-left px-4 py-2">Ошибка</th>
          </tr>
        </thead>
```

- [ ] **Step 3: Добавить новую вкладку «Балансы»**

Найди конец секции «Пересылка» (≈стр.222-223):

```html
      </table>
    </div>
  </section>

  <!-- ============ Настройки ============ -->
```

Между `</section>` пересылки и комментарием «Настройки» вставь новую секцию:

```html
  <!-- ============ Балансы ============ -->
  <section id="tab-balances" class="tab-panel hidden">
    <div class="flex flex-wrap items-end gap-3 mb-4">
      <button onclick="loadBalances()" class="px-4 py-1.5 rounded bg-blue-600 text-white">Обновить</button>
    </div>
    <div class="bg-white rounded-lg shadow overflow-hidden overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-500 uppercase text-xs">
          <tr>
            <th class="text-left px-4 py-2">Email</th>
            <th class="text-right px-4 py-2">Остаток</th>
            <th class="text-right px-4 py-2">Посл. списание</th>
            <th class="text-left px-4 py-2">Обновлено</th>
          </tr>
        </thead>
        <tbody id="balancesBody" class="divide-y divide-slate-100"></tbody>
      </table>
    </div>
  </section>

```

- [ ] **Step 4: Подключить вкладку в switchTab**

Найди функцию `switchTab` (≈стр.295-305) и добавь строку для balances. Замени блок:

```javascript
  if (name === 'forward') loadForward();
  if (name === 'settings') loadSettings();
```

на:

```javascript
  if (name === 'forward') loadForward();
  if (name === 'balances') loadBalances();
  if (name === 'settings') loadSettings();
```

- [ ] **Step 5: Расширить loadForward — новые колонки**

Найди функцию `loadForward` (≈стр.472-497) и замени её целиком:

```javascript
// ---------- Forward ----------
async function loadForward() {
  const status = document.getElementById('fwdStatus').value;
  const qs = status ? ('?status=' + status + '&limit=200') : '?limit=200';
  try {
    const data = await api('/api/forward' + qs);
    const body = document.getElementById('forwardBody');
    body.innerHTML = (data.rows || []).map(r => {
      const color = r.status === 'ok' ? 'text-green-600' : r.status === 'failed' ? 'text-red-600' : 'text-amber-600';
      const costCell = (r.cost_usd !== null && r.cost_usd !== undefined)
        ? `<span class="text-emerald-700">${fmtMoney(r.cost_usd)}</span>`
        : '<span class="text-slate-300">—</span>';
      const chargedCell = (r.resp_charged !== null && r.resp_charged !== undefined)
        ? `<span class="mono">${fmt(r.resp_charged)}</span>`
        : '<span class="text-slate-300">—</span>';
      const balanceCell = (r.resp_balance !== null && r.resp_balance !== undefined)
        ? `<span class="mono font-semibold">${fmt(r.resp_balance)}</span>`
        : '<span class="text-slate-300">—</span>';
      return `
      <tr>
        <td class="px-4 py-2 font-semibold ${color}">${r.status}</td>
        <td class="px-4 py-2">${esc(r.email)}</td>
        <td class="px-4 py-2">${esc(r.function)}</td>
        <td class="px-4 py-2 mono text-xs">${esc(r.model)}</td>
        <td class="px-4 py-2 text-right mono">${fmt(r.tokens)}</td>
        <td class="px-4 py-2 text-right mono">${costCell}</td>
        <td class="px-4 py-2 text-right">${chargedCell}</td>
        <td class="px-4 py-2 text-right">${balanceCell}</td>
        <td class="px-4 py-2 text-xs text-slate-500">${esc(fmtLocal(r.resp_datetime))}</td>
        <td class="px-4 py-2 mono text-xs">${r.http_status ?? '—'}</td>
        <td class="px-4 py-2 text-right mono">${r.attempts}</td>
        <td class="px-4 py-2 text-xs text-slate-500 max-w-xs truncate" title="${esc(r.error || '')}">${esc(r.error || '')}</td>
      </tr>`;
    }).join('') || emptyRow(12, 'нет записей');
  } catch (e) { console.error(e); }
}
```

> `emptyRow(12, ...)` — число колонок теперь 12 (было 9).

- [ ] **Step 6: Добавить loadBalances**

Вставь сразу после функции `retryFailed` (≈стр.504, перед комментарием `// ---------- Settings ----------`):

```javascript
// ---------- Balances ----------
async function loadBalances() {
  try {
    const data = await api('/api/balances');
    const body = document.getElementById('balancesBody');
    body.innerHTML = (data.rows || []).map(r => {
      const bal = (r.balance !== null && r.balance !== undefined)
        ? `<span class="mono font-semibold">${fmt(r.balance)}</span>`
        : '<span class="text-slate-300">—</span>';
      const charged = (r.last_charged !== null && r.last_charged !== undefined)
        ? `<span class="mono">${fmt(r.last_charged)}</span>`
        : '<span class="text-slate-300">—</span>';
      // last_updated — datetime из ответа; updated_at — локальное время upsert.
      const upd = r.last_updated ? fmtLocal(r.last_updated) : fmtLocal(r.updated_at);
      return `
      <tr>
        <td class="px-4 py-2">${esc(r.email)}</td>
        <td class="px-4 py-2 text-right">${bal}</td>
        <td class="px-4 py-2 text-right">${charged}</td>
        <td class="px-4 py-2 text-xs text-slate-500">${esc(upd)}</td>
      </tr>`;
    }).join('') || emptyRow(4, 'нет записей');
  } catch (e) { console.error(e); }
}
```

- [ ] **Step 7: Проверка шаблона — вкладки рендерятся**

Подними сервер и открой GUI в браузере (или через curl проверь, что шаблон отдаётся без ошибок и содержит новые элементы):

```bash
python server.py &
SRV_PID=$!
sleep 2
echo "--- вкладка Балансы есть? ---"
curl -s http://localhost:4010/admin | grep -c "data-tab=\"balances\""
echo "--- колонка Остаток есть? ---"
curl -s http://localhost:4010/admin | grep -c ">Остаток<"
echo "--- loadBalances определена? ---"
curl -s http://localhost:4010/admin | grep -c "async function loadBalances"
kill $SRV_PID 2>/dev/null
```

Expected: все три `grep -c` дают `1` (или больше).

- [ ] **Step 8: Коммит**

```bash
git add templates/admin.html
git commit -m "feat(ui): колонки ответа в Пересылке + вкладка Балансы"
```

---

## Task 5: Mock внешнего сервера — контрактный ответ + in-memory балансы

**Files:**
- Modify: `test_pipe/mock_external_server.py` (функция `ingest` ~стр.49, добавить in-memory балансы)

**Interfaces:**
- Produces: `POST /ingest` теперь отвечает `200 OK` с `{"datetime", "email", "charged", "balance"}`; ведёт in-memory балансы (старт 1000 на новый email), списывает `charged`. Сохранение файла-посылки и режим отказа (`/toggle-fail`) остаются.

> Замечание: `charged` — сколько кредитов списать. Mock получает в нашем payload `cost_usd` ($). Для детерминизма mock считает `charged` от `cost_usd` (если есть): `charged = round(cost_usd * 1000)` (имитация курса 1000 кр/$); иначе `charged = round(tokens / 1000)` (1 кр за 1000 токенов). Так баланс предсказуем для теста.

- [ ] **Step 1: Добавить in-memory балансы и обновить ingest**

В `test_pipe/mock_external_server.py` найди блок:

```python
# Глобальный флаг режима отказа (имитация недоступности внешнего сервера).
FAIL_MODE = False
```

Сразу после него добавь:

```python
# In-memory балансы пользователей (для теста). Новый email стартует с 1000.
START_BALANCE = 1000.0
BALANCES: dict[str, float] = {}
```

- [ ] **Step 2: Обновить ingest — расчёт charged, обновление баланса, контрактный ответ**

Найди функцию `ingest` (≈стр.49-67) и замени её целиком:

```python
@app.post("/ingest")
async def ingest(request: Request):
    if FAIL_MODE:
        _log(f"REJECTED (fail mode) — отвечаю 503")
        return JSONResponse(
            {"error": "fail mode is ON (mocked outage)"}, status_code=503
        )
    body = await request.json()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    email = _safe(body.get("email", ""))
    fname = RECEIVED_DIR / f"{ts}_{email}.json"
    with fname.open("w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)

    # Расчёт списания для in-memory баланса (детерминированно).
    cost_usd = body.get("cost_usd")
    tokens = body.get("tokens") or 0
    if cost_usd is not None:
        charged = round(float(cost_usd) * 1000)  # имитация: 1000 кр за $
    else:
        charged = round(tokens / 1000)           # 1 кр за 1000 токенов

    real_email = body.get("email") or "unknown"
    BALANCES[real_email] = BALANCES.get(real_email, START_BALANCE) - charged
    balance = round(BALANCES[real_email], 2)

    resp = {
        "datetime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "email": real_email,
        "charged": charged,
        "balance": balance,
    }
    _log(
        f"SAVED {fname.name} email={real_email!r} "
        f"charged={charged} balance={balance}"
    )
    return JSONResponse(resp)
```

- [ ] **Step 3: Добавить баланс в /health (удобно для теста)**

Найди функцию `health` (≈стр.84-90) и замени:

```python
@app.get("/health")
def health():
    return {
        "status": "ok",
        "fail_mode": FAIL_MODE,
        "received_count": len(list(RECEIVED_DIR.glob("*.json"))),
        "balances": dict(BALANCES),
    }
```

- [ ] **Step 4: Синтакcис-проверка**

```bash
python -c "import ast; ast.parse(open('test_pipe/mock_external_server.py', encoding='utf-8').read()); print('syntax OK')"
```

Expected: `syntax OK`.

- [ ] **Step 5: Проверка контракта mock напрямую**

```bash
cd test_pipe && python mock_external_server.py &
MOCK_PID=$!
sleep 2
echo "--- первый запрос (старт 1000) ---"
curl -s http://localhost:4020/ingest -H "Content-Type: application/json" \
  -d '{"email":"t@x.com","tokens":1234,"cost_usd":0.001234}'
echo
echo "--- второй запрос (баланс должен упасть) ---"
curl -s http://localhost:4020/ingest -H "Content-Type: application/json" \
  -d '{"email":"t@x.com","tokens":1234,"cost_usd":0.001234}'
echo
echo "--- балансы в /health ---"
curl -s http://localhost:4020/health
echo
kill $MOCK_PID 2>/dev/null
```

Expected: первый ответ `{"datetime":...,"email":"t@x.com","charged":1,"balance":999.0}`; второй — `charged=1`, `balance=998.0`. `/health` показывает `"balances":{"t@x.com":998.0}`.

- [ ] **Step 6: Коммит**

```bash
git add test_pipe/mock_external_server.py
git commit -m "feat(mock): контрактный ответ {datetime,email,charged,balance} + in-memory балансы"
```

---

## Task 6: Полный E2E + документация

**Files:**
- Modify: `README.md` (секция «Тело пересылки на другой сервер» ~стр.303, «Быстрая проверка» ~стр.310, таблица API ~стр.238)

**Interfaces:** нет (финальная проверка + docs).

- [ ] **Step 1: Полный сквозной E2E через моки**

Подними три компонента и прогони полный цикл: pipe → наш сервер → mock (контракт) → forward_log с `resp_*` → баланс в реестре → failed/retry.

```bash
# Из корня проекта.
cd test_pipe && python fake_openrouter_server.py &  FAKE=$!
python mock_external_server.py &                    MOCK=$!
cd .. && python server.py &                         SRV=$!
sleep 3
```

Отправь отчёт (имитация pipe — прямой POST на наш сервер):

```bash
python -c "
import asyncio, httpx
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post('http://localhost:4010/api/usage',
            headers={'Authorization':'Bearer devbim2026'},
            json={'email':'e2e@x.com','function':'E2E','model':'test-model',
                  'tokens':1234,'prompt_tokens':1000,'completion_tokens':234,'cost_usd':0.001234})
        print('usage:', r.status_code, r.json())
asyncio.run(main())
"
sleep 2
```

Проверь результат в БД:

```bash
python -c "
import asyncio, db
async def main():
    fwd = await db.list_forward('credits.db', limit=1)
    bal = await db.list_balances('credits.db')
    f = fwd[0] if fwd else {}
    print('forward:', f.get('status'), 'http=', f.get('http_status'),
          'charged=', f.get('resp_charged'), 'balance=', f.get('resp_balance'),
          'datetime=', f.get('resp_datetime'))
    print('balances:', bal)
    assert f.get('status')=='ok'
    assert f.get('resp_balance') is not None, 'balance not parsed from response'
    assert any(b['email']=='e2e@x.com' for b in bal), 'balance not in registry'
    print('E2E OK')
asyncio.run(main())
"
```

Expected: `forward: ok http= 200 charged= 1 balance= 999.0 datetime= ...`, `balances` содержит `e2e@x.com` с `balance=999.0`, `E2E OK`.

- [ ] **Step 2: Проверка failed + retry не ломают реестр**

```bash
cd test_pipe && curl -s http://localhost:4020/toggle-fail; echo
cd ..
python -c "
import asyncio, httpx
async def main():
    async with httpx.AsyncClient() as c:
        await c.post('http://localhost:4010/api/usage',
            headers={'Authorization':'Bearer devbim2026'},
            json={'email':'e2e@x.com','function':'E2E-fail','model':'test-model','tokens':2000})
asyncio.run(main())
"
sleep 2
python -c "
import asyncio, db
async def main():
    bal = await db.list_balances('credits.db')
    e2e = [b for b in bal if b['email']=='e2e@x.com'][0]
    print('balance after failed attempt:', e2e['balance'])
    assert e2e['balance']==999.0, 'registry must NOT change on failed forward'
    print('REGISTRY-UNCHANGED OK')
asyncio.run(main())
"
# Возвращаем mock в рабочий режим и перепосылаем failed.
cd test_pipe && curl -s http://localhost:4020/toggle-fail; echo
cd ..
curl -s -X POST http://localhost:4010/api/retry-failed -H "Authorization: Bearer devbim2026"; echo
sleep 2
python -c "
import asyncio, db
async def main():
    bal = await db.list_balances('credits.db')
    e2e = [b for b in bal if b['email']=='e2e@x.com'][0]
    print('balance after retry:', e2e['balance'])
    assert e2e['balance'] < 999.0, 'registry should update after successful retry'  # 2000 токенов → charged=2 → 997.0
    print('RETRY OK')
asyncio.run(main())
"
```

Expected: после `failed` баланс остаётся `999.0` (`REGISTRY-UNCHANGED OK`); после retry — уменьшается (`RETRY OK`).

- [ ] **Step 3: Остановить процессы**

```bash
kill $SRV $MOCK $FAKE 2>/dev/null
```

- [ ] **Step 4: Обновить README — таблица API**

В `README.md`, в таблице API (≈стр.238-251), после строки `| GET | /api/forward?status=... | очередь пересылки |` добавь:

```markdown
| GET | `/api/balances` | реестр балансов пользователей (из ответов внешнего сервера) |
```

- [ ] **Step 5: Обновить README — секция «Тело пересылки»**

Найди секцию «### Тело пересылки на «другой сервер»» (≈стр.303-308). После абзаца, заканчивающегося «...с заголовком `Authorization: Bearer <FORWARD_API_KEY>` (если ключ задан).», добавь новый подраздел:

```markdown

#### Ответ внешнего сервера

Внешний сервер отвечает на каждый POST `200 OK` с JSON-контрактом:

```json
{
  "datetime": "2026-08-08T13:59:41+00:00",
  "email": "user@example.com",
  "charged": 50,
  "balance": 950
}
```

| Поле | Назначение |
|---|---|
| `datetime` | Дата/время списания на стороне внешней системы |
| `email` | Для сверки (от кого списали) |
| `charged` | Сколько кредитов списано внешней системой |
| `balance` | **Остаток кредитов пользователя** после списания |

Сервер парсит ответ, сохраняет поля в очередь пересылки (колонки «Списано» /
«Остаток» / «Время ответа») и поддерживает **реестр балансов** — последний
известный остаток по каждому email (вкладка «Балансы», `GET /api/balances`).

- Отсутствующие/`null` поля сохраняются пустыми; `balance: 0` валиден.
- Не-JSON или пустое 2xx-тело → пересылка считается успешной, поля ответа пусты.
- Реестр обновляется **только** при валидном `balance`; при `failed`/таймауте —
  не трогается, а при перепосылке (`Перепослать failed`) обновляется после успеха.
- «Кредиты» в ответе — валюта внешнего биллинга; внутренние кредиты
  (`usage_records.credits`) считаются как раньше. `charged` может с ними не
  совпадать — это полезно для сверки расхождений.
```

- [ ] **Step 6: Обновить README — «Быстрая проверка»**

В секции «## Быстрая проверка после установки» найди пункт 5 (≈стр.318-319):

```markdown
5. **Пересылка:** GUI → «Пересылка» — статус записи (`ok` / `failed` / `pending`).
   При `failed` можно нажать «Перепослать failed`.
```

Замени на:

```markdown
5. **Пересылка:** GUI → «Пересылка» — статус записи (`ok` / `failed` / `pending`),
   колонки «Списано» / «Остаток» / «Время ответа» (из ответа внешнего сервера).
   При `failed` можно нажать «Перепослать failed`.
6. **Балансы:** GUI → «Балансы» — актуальный остаток кредитов по каждому email
   (обновляется из поля `balance` в ответе внешнего сервера).
```

- [ ] **Step 7: Финальная проверка — GUI в браузере (по возможности)**

Если есть доступ к браузеру: `python server.py` + мок, отправь отчёт, открой `http://localhost:4010/admin`, проверь: вкладки «Пересылка» (новые колонки заполнены) и «Балансы» (запись с остатком). Если браузер недоступен — шаг 1-2 уже покрыли проверку на уровне БД/API.

- [ ] **Step 8: Коммит**

```bash
git add README.md
git commit -m "docs: контракт ответа внешнего сервера + вкладка Балансы"
```

---

## Self-Review (выполнено автором плана)

- **Spec coverage:** ✅ Контракт ответа → Task 2 (forwarder) + Task 5 (mock). Колонки `forward_log` → Task 1. Таблица `user_balances` → Task 1. `upsert_balance`/`list_balances` → Task 1. Реестр обновляется только при валидном balance → Task 2 (`if resp_balance is not None`). `GET /api/balances` → Task 3. GUI колонки + вкладка → Task 4. Контракт в README → Task 6. E2E → Task 6 (вкл. failed/retry не ломают реестр).
- **Placeholder scan:** TBD/TODO/«добавить обработку ошибок» — нет. Все шаги содержат конкретный код и команды.
- **Type consistency:** `update_forward_entry` (Task 1) ↔ вызов в Task 2 — сигнатуры совпадают (`resp_datetime, resp_email, resp_charged, resp_balance, resp_raw`). `upsert_balance(email, balance, last_charged, last_updated, forward_id)` ↔ вызов в Task 2 — совпадает. `list_balances` ↔ `GET /api/balances` ↔ `loadBalances` — поля `email/balance/last_charged/last_updated/forward_id/updated_at` согласованы. `list_forward` выбирает `f.*` → `resp_*` приходят в `loadForward` как `r.resp_*` — согласовано с Task 4.
- **Зависимости:** Task 2 зависит от Task 1 (новые функции db). Task 3 зависит от Task 1 (`list_balances`). Task 4 зависит от Task 3 (`/api/balances`) и Task 1 (`resp_*` в `/api/forward`). Task 5 независим. Task 6 интегрирует всё. Порядок 1→2→3→4→5→6 корректен; Task 5 можно делать в любом месте, но поставлен после UI чтобы финальный E2E (Task 6) шёл последним.

---

## Execution Handoff

План сохранён в `docs/superpowers/plans/2026-08-08-forward-response-and-balances.md`. Два варианта выполнения:

1. **Subagent-Driven (рекомендуется)** — диспетчер свежего subagent на каждую задачу, ревью между задачами, быстрая итерация.
2. **Inline Execution** — выполнение задач в этой сессии через executing-plans, батчами с контрольными точками.

Какой подход?
