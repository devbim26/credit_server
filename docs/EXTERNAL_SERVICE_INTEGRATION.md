# Интеграция с внешним сервисом (биллингом кредитов)

> **Кому:** разработчику внешнего сервиса, который **принимает** от нашего сервера
> данные об использовании и **списывает кредиты** пользователей.
>
> **О чём документ:** точный контракт HTTP-взаимодействия — что наш сервер
> отправляет и какой ответ ждёт обратно. Документ самодостаточный: прочитав его,
> можно реализовать приёмную сторону без изучения кода нашего сервера.

---

## Содержание

1. [Роли и поток данных](#1-роли-и-поток-данных)
2. [Что наш сервер отправляет (запрос)](#2-что-наш-сервер-отправляет-запрос)
3. [Что наш сервер ждёт обратно (ответ)](#3-что-наш-сервер-ждёт-обратно-ответ)
4. [Как наш сервер обрабатывает ответ](#4-как-наш-сервер-обрабатывает-ответ)
5. [Коды состояния и retry](#5-коды-состояния-и-retry)
6. [Идемпотентность — ВАЖНО](#6-идемпотентность--важно)
7. [Аутентификация](#7-аутентификация)
8. [Таймауты и производительность](#8-таймауты-и-производительность)
9. [Эталонная реализация (FastAPI)](#9-эталонная-реализация-fastapi)
10. [Проверка вручную (curl)](#10-проверка-вручную-curl)
11. [JSON-схемы (для валидации/генерации кода)](#11-json-схемы-для-валидациигенерации-кода)
12. [Частые вопросы (FAQ)](#12-частые-вопросы-faq)
13. [Шпаргалка по полям](#13-шпаргалка-по-полям)

---

## 1. Роли и поток данных

В системе три участника:

```
пользователь (Open WebUI)
        │  использует модель (запрос к ИИ)
        ▼
┌──────────────────────────────┐
│  НАШ сервер списания кредитов │  ← считает токены → кредиты, хранит лог
│  (этот проект)               │
└──────────────┬───────────────┘
               │  PUT /api/OpenRouterModels/updateSubscription
               │  (openRouterWebUiUserId, messageCost, modelId, ...)
               │  ← ждёт 2xx (тело опционально)
               ▼
┌──────────────────────────────┐
│  ВНЕШНИЙ сервис devbim.com    │  ← списывает кредиты, хранит балансы
│  биллинг кредитов             │
└──────────────────────────────┘
```

**Наша обязанность:** после каждого использования модели сообщить вам
идентификатор пользователя (`openRouterWebUiUserId`), модель, стоимость в $
(`messageCost`) и результат вызова (`isSuccess`/`errorMessage`). Делаем это в
фоне, не блокируя пользователя.

**Ваша обязанность:** принять запрос, списать кредиты в своём гроссбухе и
вернуть подтверждение — сколько списано (`charged`) и какой остаток у
пользователя (`balance`).

> «Кредиты» в ответе — это **ваша** внутренняя валюта биллинга. Наш сервер
> считает свои внутренние «кредиты» по своим курсам — это **другая** величина.
> Они не обязаны совпадать: `charged` — то, что списали вы; наши кредиты —
> то, что посчитали мы для аналитики. Поле `charged` нужно именно как
> подтверждение факта списания с вашей стороны.

---

## 2. Что наш сервер отправляет (запрос)

### Метод и адрес

```
PUT  <FORWARD_URL>
```

По умолчанию `FORWARD_URL` указывает на боевой эндпоинт devbim.com:

```
https://devbim.com/api/OpenRouterModels/updateSubscription
```

`FORWARD_URL` настраивается администратором нашего сервера (через `.env` или
GUI → «Настройки»). Путь может быть любым, его задаёт принимающая сторона.

### Заголовки

| Заголовок | Значение | Всегда? |
|---|---|---|
| `Content-Type` | `application/json` | да |
| `Authorization` | `Bearer <FORWARD_API_KEY>` | **только если** администратор задал `FORWARD_API_KEY` |

Если `FORWARD_API_KEY` не задан — заголовок `Authorization` не отправляется
(используется только в dev/за защищённым периметром). В проде ключ задаётся
всегда — см. [раздел 7](#7-аутентификация).

### Тело запроса (JSON)

```json
{
  "openRouterWebUiUserId": "6cce058d-bddf-4c98-96be-bc0494fd32eb",
  "messageCost": 0.001234,
  "modelId": "google/gemini-3.5-flash",
  "modelName": "google/gemini-3.5-flash",
  "requestText": "",
  "responseText": "",
  "requestDate": "2026-08-10T11:00:00+00:00",
  "responseDate": "2026-08-10T11:00:02+00:00",
  "isSuccess": true,
  "errorMessage": null,
  "metadataJson": "{\"email\":\"user@example.com\",\"function\":\"Чат\",\"timestamp\":\"2026-08-10T11:00:00+00:00\",\"tokens\":1234,\"prompt_tokens\":1000,\"completion_tokens\":234}"
}
```

#### Описание полей запроса

| Поле | Тип | Обяз. | Описание |
|---|---|:---:|---|
| `openRouterWebUiUserId` | string (GUID) | ✅ | Внутренний ID пользователя Open WebUI (`__user__.id`). **Ключ идентификации** пользователя в вашем биллинге. ⚠️ Живой API devbim.com типизирует поле как `System.Guid`: допустим только настоящий GUID; email или пустая строка → 400, незарегистрированный GUID → 404 «Ключ доступа пользователя не был найден». Email идёт справочно внутри `metadataJson`. |
| `messageCost` | number ≥ 0 | ✅ | Стоимость запроса в долларах США. Либо отдаёт провайдер (OpenRouter: `usage.cost`), либо наш расчёт по тарифам $/млн. |
| `modelId` | string | ✅ | Имя модели (например `google/gemini-3.5-flash`). Совпадает с `modelName`. |
| `modelName` | string | ✅ | Отображаемое имя модели. Совпадает с `modelId`. |
| `requestText` | string | ✅ | Всегда пустая строка `""`. Содержимое запроса пользователя не покидает границы Open WebUI. |
| `responseText` | string | ✅ | Всегда пустая строка `""`. Содержимое ответа модели не покидает границы Open WebUI. |
| `requestDate` | string (ISO 8601) | да | Дата/время **старта** запроса к модели (UTC, `+00:00`). |
| `responseDate` | string (ISO 8601) | да | Дата/время **завершения** запроса к модели (UTC, `+00:00`). |
| `isSuccess` | boolean | ✅ | Результат вызова модели. `true` — модель ответила успешно; `false` — модель недоступна / вернула ошибку. |
| `errorMessage` | string\|null | да | Текст ошибки модели. Заполнен только при `isSuccess: false`; при успехе — `null`. |
| `metadataJson` | string | ✅ | Свёрнутый JSON со вспомогательными полями (см. ниже). |

#### Поле `metadataJson` (вспомогательные данные)

Это строка с JSON — внутри лежат поля нашего внутреннего учёта, не вошедшие в
основной контракт:

```json
{
  "email": "user@example.com",
  "function": "Чат",
  "timestamp": "2026-08-10T11:00:00+00:00",
  "tokens": 1234,
  "prompt_tokens": 1000,
  "completion_tokens": 234
}
```

| Вложенное поле | Тип | Описание |
|---|---|---|
| `email` | string | Email пользователя Open WebUI (для сверки/логов). |
| `function` | string | Название функции (агента) Open WebUI. |
| `timestamp` | string | Дата/время операции (ISO 8601, UTC). |
| `tokens` | integer | Всего токенов (input + output). |
| `prompt_tokens` | integer (опц.) | Токены input. Есть, когда провайдер отдал breakdown. |
| `completion_tokens` | integer (опц.) | Токены output. Есть, когда провайдер отдал breakdown. |

> `metadataJson` нужно парсить как JSON (это строка, не объект). Поля внутри
> опциональны, кроме `email`/`function`/`timestamp`/`tokens`.

---

## 3. Что наш сервер ждёт обратно (ответ)

### Код состояния

| Диапазон | Наша реакция |
|---|---|
| `2xx` (200–299) | **Успех.** Пытаемся разобрать тело (если есть) и обновить реестр балансов. |
| `≥ 400` (4xx, 5xx) | **Провал.** Запись помечается `failed`, тело ответа (первые 300 символов) сохраняется как текст ошибки. Будет повторная отправка (см. [раздел 5](#5-коды-состояния-и-retry)). |
| таймаут / сетевая ошибка | **Провал.** Аналогично `≥ 400` — статус `failed`, запись ошибки. |

> Возвращайте `200 OK` на успешное списание. Не используйте `200` для сообщения об
> ошибке (например «недостаточно кредитов») — для этого нужен `4xx`/`5xx`, иначе
> наш сервер сочтёт операцию успешной и не повторит.

### Тело ответа — ОПЦИОНАЛЬНО (но желательно)

Наш сервер определяет успех **по HTTP-коду**, а не по телу. Если тело пустое или
отсутствует — пересылка всё равно считается успешной. **Однако**, если вы вернёте
JSON с полями `charged`/`balance`, мы наполним вкладку «Балансы» в нашем GUI и
колонки «Списано/Остаток» в очереди пересылки. Поэтому **настоятельно просим**
возвращать тело по контракту ниже.

> ⚠️ На момент написания этого раздела devbim.com ещё **не возвращает** тело с
> `charged`/`balance`. Мы просим разработчика devbim.com добавить этот ответ —
> пока его нет, вкладка «Балансы» остаётся пустой, но пересылка работает.

### Желаемый контракт ответа (JSON)

На успешный (`2xx`) ответ возвращайте JSON с этими полями:

```json
{
  "dateTime": "2026-08-10T11:00:02+00:00",
  "email": "user@example.com",
  "charged": 0.0000185,
  "balance": 9.9999815
}
```

| Поле | Тип | Nullable | Описание |
|---|---|:---:|---|
| `dateTime` | string (ISO 8601) | да | Дата/время **выполнения списания на вашей стороне**. Покажем в GUI как «время ответа». |
| `email` | string | да | Для сверки — от какого пользователя пришёл запрос. Наш сервер использует для идентификации **свой** `openRouterWebUiUserId`/`email` из запроса, это поле только для контроля. |
| `charged` | number | да | Сколько кредитов **списано** в вашей системе. |
| `balance` | number | да | **Остаток кредитов** пользователя после списания. |

#### Правила для полей

- **Тип `charged`/`balance` — число** (integer или float). Принимаем `50`,
  `50.0`, `50.5`. **Не** строки (`"50"`), **не** булевы (`true`):
  - если придёт строка/`true`/`false` — наш сервер воспримет поле как
    отсутствующее (толерантность к нарушению контракта);
  - поэтому для корректного отображения возвращайте именно число.
- **`balance: 0` валиден** и обновит реестр (ноль — это легитимный остаток).
- **`null` или отсутствие поля** → реестр балансов не обновляем (считаем, что
  остаток неизвестен), но саму пересылку считаем успешной.
- **Дополнительные поля** в ответе игнорируются. Контракт можно расширять
  назад-совместимо: если в будущем добавите поле — наш сервер его не заметит и
  не сломается.
- **`dateTime`** — если возвращаете, формат ISO 8601 с таймзоной
  (`2026-08-10T11:00:02+00:00`). Если не смогли — верните `null`, GUI покажет
  локальное время приёма ответа.

#### Чего мы НЕ требуем

- Не нужно возвращать идентификатор транзакции — у нас нет эндпоинта для
  подтверждения/отмены.
- Не нужно возвращать курс или разбивку — только итоговые `charged` и `balance`.
- Тело на `4xx`/`5xx` — произвольное (сохраним первые 300 символов как
  описание ошибки для админки).

---

## 4. Как наш сервер обрабатывает ответ

Чтобы вы понимали последствия, вот что происходит с вашим ответом:

| Ваше поле | Куда попадает | Как используется |
|---|---|---|
| `dateTime` | `forward_log.resp_datetime` | Колонка «Время ответа» в GUI (локализованно). Если есть — `user_balances.last_updated`. |
| `email` | `forward_log.resp_email` | Справочно, только для сверки в GUI. **Не** используется для идентификации. |
| `charged` | `forward_log.resp_charged`, `user_balances.last_charged` | Колонка «Списано» в GUI. |
| `balance` | `forward_log.resp_balance`, `user_balances.balance` | Колонка «Остаток» + вкладка «Балансы». **Реестр обновляется только если `balance` — число (не null).** |

Дополнительно:
- **Сырой текст ответа** (до 1024 символов) сохраняется в `forward_log.resp_raw`
  для аудита и отладки.
- **Реестр `user_balances`** — одна строка на `email`: последний известный
  остаток. При каждом успешном ответе с валидным `balance` строка обновляется
  (upsert по email).

### Толерантность к телу ответа

Наш сервер **не падает** от некорректного ответа:

| Ситуация | Реакция нашего сервера |
|---|---|
| Тело — валидный JSON с контрактом | `ok`, поля сохранены, реестр обновлён. |
| Тело — валидный JSON, но часть полей `null`/отсутствует | `ok`, есть поля сохранены, реестр обновляется только если `balance` валиден. |
| `charged`/`balance` — строка или `true`/`false` | `ok`, эти поля приравниваются к `null` (реестр не трогаем). |
| Тело — не JSON или пустое | `ok`, все `resp_*` = null, реестр не трогаем. |
| Статус `≥ 400` | `failed`, тело (300 символов) → в описание ошибки. |

> Иначе говоря: **ошибка разбора вашего ответа никогда не превращает 2xx в
> failed.** Только HTTP-код `≥ 400` или таймаут дают `failed`.

---

## 5. Коды состояния и retry

### Что вызывает повторную отправку

Наш сервер **не делает** автоматических ретраев по расписанию. Повторная
отправка происходит только когда администратор нажимает «Перепослать failed» в
GUI (или вызывает `POST /api/retry-failed`). Тогда **все** записи в статусе
`failed` отправляются повторно — каждый тем же `PUT`-запросом, что и изначально.

### Рекомендации по кодам состояния

| Ваша ситуация | Рекомендуемый код | Поведение нашего сервера |
|---|---|---|
| Успешное списание | `200 OK` | `ok`, реестр обновлён. |
| Незнакомый `email` (нет такого пользователя) | `404` | `failed`, запись останется для ручного разбора. При retry повторится. |
| Недостаточно кредитов | `402 Payment Required` или `409 Conflict` | `failed`. Решите: давать ли долг или блокировать. |
| Неверный `Authorization` | `401` | `failed`. При retry повторится, пока админ не поправит ключ. |
| Прочая ошибка обработки | `500` / `503` | `failed`, будет ретрай. |
| Временная недоступность | `503` + (опц.) `Retry-After` | `failed`. `Retry-After` мы сейчас не учитываем, но он не помешает. |

> **Не возвращайте `200` с телом-ошибкой** (например `{"error": "no funds"}`).
> Наш сервер воспримет это как успех, сохранит `charged`/`balance` из ответа
> (если они есть) и не повторит запрос. Для ошибок — `4xx`/`5xx`.

---

## 6. Идемпотентность — ВАЖНО

Наша доставка — **«минимум один раз» (at-least-once)**. Один и тот же запрос
может прийти вам **более одного раза** в трёх случаях:

1. **Ручной retry** — админ нажал «Перепослать failed» (даже если первый раз вы
   уже списали, но ответили `5xx`/таймаут, и наш сервер счёл провал).
2. **Сетевой сбой посередине** — вы списали и ответили `200`, но ответ потерялся
   в сети → наш сервер получил таймаут → `failed` → потом retry.
3. **Перезапуск нашего сервера** во время пересылки.

**Следствие:** ваш эндпоинт обязан быть **идемпотентным** — повторный запрос с
теми же данными не должен списывать кредиты повторно.

### Как обеспечить идемпотентность

Поскольку в текущем контракте **нет отдельного idempotency-key**, дедуплицируйте
по содержимому запроса. Надёжный естественный ключ дедупликации — распарсить
`metadataJson` и взять:

```
(openRouterWebUiUserId, timestamp, function, model, tokens)
```

(где `timestamp`/`function`/`model`/`tokens` — из `metadataJson`). Эти поля
вместе однозначно идентифицируют операцию в нашей системе (`timestamp` — UTC,
точность до секунды; у двух разных операций он различается). Дополнительно можно
учитывать `prompt_tokens`/`completion_tokens` из `metadataJson`, если они есть.

**Рекомендуемая логика на вашей стороне:**

```text
получили запрос
meta = JSON.parse(body.metadataJson)
ключ = (body.openRouterWebUiUserId, meta.timestamp, meta.function, meta.model, meta.tokens)
если ключ уже обработан (есть в вашей таблице транзакций):
    вернуть сохранённый ответ {dateTime, charged, balance}  ← БЕЗ повторного списания
иначе:
    списать charged (по messageCost или tokens)
    сохранить баланс
    запомнить ключ → (charged, balance)
    вернуть {dateTime, charged, balance}
```

Так повторный запрос вернёт тот же `charged`/`balance`, что и первый, и
пользователь не потеряет кредиты дважды.

> **Если вам нужен явный idempotency-key** в запросе — сообщите, добавим поле
> в контракт (расширение назад-совместимо, поле можно сделать опциональным и
> заполнять уникальным ID на нашей стороне).

---

## 7. Аутентификация

### Что вы получаете

Если администратор задал `FORWARD_API_KEY`, каждый наш запрос содержит:

```
Authorization: Bearer <FORWARD_API_KEY>
```

`FORWARD_API_KEY` — общая секретная строка, настраивается с обеих сторон. Это
**не** токен конкретного пользователя, а сервисный ключ «наш сервер → ваш
сервис».

### Как проверять

1. Достаньте токен из заголовка (`Bearer <...>`).
2. Сравните с ожидаемым ключом **constant-time** (защита от timing-атак).
   - На Python: `hmac.compare_digest(received, expected)`.
3. Не совпал → `401 Unauthorized`, наш сервер пометит `failed`.

```python
import hmac
auth = request.headers.get("Authorization", "")
token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
if not hmac.compare_digest(token, EXPECTED_KEY):
    return JSONResponse({"error": "unauthorized"}, status_code=401)
```

> Если ключ не задан (`FORWARD_API_KEY` пустой) — заголовок `Authorization`
> **не отправляется**. Это режим только для локальной разработки. В проде
> ключ задаётся всегда, и вы обязаны его проверять.

### Дополнительно: ограничение по источнику

Можно дополнительно фильтровать по IP нашего сервера (если он статический)
или проксировать через Cloudflare с правилом — но Bearer-ключ достаточно.

---

## 8. Таймауты и производительность

| Параметр | Значение | Где |
|---|---|---|
| Таймаут нашего запроса | **10 секунд** | `FORWARD_TIMEOUT = 10.0` в `forwarder.py` |
| Параллельность | Последовательно (один запрос за раз в потоке пересылки) | `forward_pending` |

**Требование:** отвечайте **быстро**, в идеале < 2–3 секунд. Если ваш эндпоинт
дольше 10 секунд — наш сервер получит таймаут, пометит `failed` и позже
пришлёт тот же запрос повторно (а вы, если уже списали, должны будете его
дедуплицировать — см. [раздел 6](#6-идемпотентность--важно)).

Нагрузка — низкая: десятки запросов в минуту типично (по факту использования
моделей пользователями). Горизонтального масштабирования не требуется.

---

## 9. Эталонная реализация (FastAPI)

Минимальный рабочий пример приёмного эндпоинта на Python/FastAPI с **настоящим**
хранилищем (SQLite), **идемпотентностью** и **проверкой ключа**. Это не
обязательный фреймворк — любой язык/стек подходит; пример показывает логику.

```python
# external_billing.py
import hmac
import json
from datetime import datetime, timezone
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# Сервисный ключ (должен совпадать с FORWARD_API_KEY на нашем сервере).
EXPECTED_KEY = "ваш-секретный-ключ"

# Тариф списания. Пример: charged = messageCost (USD) с наценкой. Можете задать любой.
# В боевом контракте charged/balance — в USD (как messageCost).
MARKUP = 1.0  # 1.0 = charged равен messageCost; >1 = наценка

# Простое in-process SQLite-хранилище (замените на вашу БД/ORM).
# Ключ идентификации — openRouterWebUiUserId (как в основном контракте).
conn = sqlite3.connect("billing.db", check_same_thread=False)
conn.executescript("""
CREATE TABLE IF NOT EXISTS balances (
    user_id TEXT PRIMARY KEY,
    balance REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (
    key TEXT PRIMARY KEY,            -- (userId|timestamp|function|model|tokens)
    charged REAL NOT NULL,
    balance REAL NOT NULL,
    datetime TEXT NOT NULL
);
""")


def _check_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    return hmac.compare_digest(token, EXPECTED_KEY)


@app.put("/api/OpenRouterModels/updateSubscription")
async def update_subscription(request: Request):
    # 1. Авторизация.
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    user_id = body.get("openRouterWebUiUserId")
    if not user_id:
        return JSONResponse({"error": "openRouterWebUiUserId required"}, status_code=400)

    # Распаковываем metadataJson — там лежат email/function/timestamp/tokens.
    try:
        meta = json.loads(body.get("metadataJson") or "{}")
    except Exception:
        meta = {}

    # 2. Ключ идемпотентности.
    key = "|".join(str(meta.get(k, "")) for k in
                   ("timestamp", "function", "model", "tokens"))
    key = f"{user_id}|{key}"

    cur = conn.cursor()
    cur.execute("SELECT charged, balance, datetime FROM processed WHERE key = ?", (key,))
    existing = cur.fetchone()
    if existing:
        # Уже обработано — возвращаем тот же ответ без нового списания.
        charged, balance, dt = existing
        return {"dateTime": dt, "email": meta.get("email"), "charged": charged, "balance": balance}

    # 3. Расчёт списания (ваша бизнес-логика). Пример: charged в USD из messageCost.
    message_cost = body.get("messageCost")
    if message_cost is not None:
        charged = round(float(message_cost) * MARKUP, 6)
    else:
        charged = round((meta.get("tokens") or 0) * 1.85e-6, 6)  # fallback по токенам

    # 4. Списание (создаём пользователя со стартовым балансом, если нового).
    cur.execute(
        "INSERT INTO balances(user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance - ?",
        (user_id, 10.0, charged),
    )
    cur.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,))
    balance = round(cur.fetchone()[0], 6)
    conn.commit()

    # 5. Запоминаем результат для идемпотентности.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO processed(key, charged, balance, datetime) VALUES (?, ?, ?, ?)",
        (key, charged, balance, now),
    )
    conn.commit()

    # 6. Ответ по контракту (опционально, но желательно).
    return {"dateTime": now, "email": meta.get("email"), "charged": charged, "balance": balance}
```

Запуск:

```bash
pip install fastapi uvicorn
uvicorn external_billing:app --host 0.0.0.0 --port 4020
```

> Пример намеренно упрощён (in-process SQLite, без миграций). В проде используйте
> вашу основную БД и ORM. Суть — пять шагов: **авторизация → дедупликация →
> расчёт → списание → ответ по контракту**.

Рабочий (тестовый) эталон есть в нашем репозитории: `test_pipe/mock_external_server.py`.
Он реализует тот же контракт, но с in-memory хранилищем и без идемпотентности
(для тестов это допустимо).

---

## 10. Проверка вручную (curl)

### Имитация запроса от нашего сервера (проверьте свой эндпоинт)

```bash
curl -i -X PUT http://localhost:4020/api/OpenRouterModels/updateSubscription \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ваш-секретный-ключ" \
  -d '{
    "openRouterWebUiUserId": "user-abc-123",
    "messageCost": 0.001234,
    "modelId": "test-model",
    "modelName": "test-model",
    "requestText": "",
    "responseText": "",
    "requestDate": "2026-08-10T11:00:00+00:00",
    "responseDate": "2026-08-10T11:00:02+00:00",
    "isSuccess": true,
    "errorMessage": null,
    "metadataJson": "{\"email\":\"user@example.com\",\"function\":\"Тест\",\"timestamp\":\"2026-08-10T11:00:00+00:00\",\"tokens\":1234,\"prompt_tokens\":1000,\"completion_tokens\":234}"
  }'
```

Ожидаемый ответ (HTTP 200):

```http
HTTP/1.1 200 OK
content-type: application/json

{
  "dateTime": "2026-08-10T11:00:02+00:00",
  "email": "user@example.com",
  "charged": 0.001234,
  "balance": 9.998766
}
```

### Проверка идемпотентности (повторный запрос с теми же данными)

```bash
# Тот же запрос ещё раз — charged и balance должны остаться прежними,
# второй раз кредиты не списываются.
curl -s -X PUT http://localhost:4020/api/OpenRouterModels/updateSubscription \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ваш-секретный-ключ" \
  -d '{ "openRouterWebUiUserId": "user-abc-123",
        "messageCost": 0.001234,
        "modelId": "test-model", "modelName": "test-model",
        "requestText": "", "responseText": "",
        "requestDate": "2026-08-10T11:00:00+00:00",
        "responseDate": "2026-08-10T11:00:02+00:00",
        "isSuccess": true, "errorMessage": null,
        "metadataJson": "{\"email\":\"user@example.com\",\"function\":\"Тест\",\"timestamp\":\"2026-08-10T11:00:00+00:00\",\"tokens\":1234}" }'
```

`balance` не должен измениться (если оба запроса пошли по одному ключу).

### Проверка отказа (401 без ключа)

```bash
curl -i -X PUT http://localhost:4020/api/OpenRouterModels/updateSubscription \
  -H "Content-Type: application/json" \
  -d '{ "openRouterWebUiUserId": "x", "messageCost": 0, "modelId": "m",
        "modelName": "m", "requestText": "", "responseText": "",
        "requestDate": "2026-08-10T11:00:00+00:00",
        "responseDate": "2026-08-10T11:00:00+00:00",
        "isSuccess": true, "errorMessage": null,
        "metadataJson": "{\"email\":\"x@y.com\",\"function\":\"T\",\"timestamp\":\"2026-08-10T11:00:00+00:00\",\"tokens\":1}" }'
# ожидается: HTTP/1.1 401 Unauthorized
```

---

## 11. JSON-схемы (для валидации/генерации кода)

### Схема запроса (что приходит вам)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["openRouterWebUiUserId", "messageCost", "modelId", "modelName",
               "requestText", "responseText", "requestDate", "responseDate",
               "isSuccess", "errorMessage", "metadataJson"],
  "properties": {
    "openRouterWebUiUserId": { "type": "string", "description": "ID пользователя Open WebUI" },
    "messageCost":   { "type": "number", "minimum": 0, "description": "Стоимость в USD" },
    "modelId":       { "type": "string", "description": "Имя модели (= modelName)" },
    "modelName":     { "type": "string", "description": "Имя модели (= modelId)" },
    "requestText":   { "type": "string", "description": "Всегда пустая строка" },
    "responseText":  { "type": "string", "description": "Всегда пустая строка" },
    "requestDate":   { "type": "string", "format": "date-time",
                       "description": "ISO 8601, старт запроса к модели" },
    "responseDate":  { "type": "string", "format": "date-time",
                       "description": "ISO 8601, завершение запроса к модели" },
    "isSuccess":     { "type": "boolean", "description": "Успех вызова модели" },
    "errorMessage":  { "type": ["string", "null"], "description": "Текст ошибки или null" },
    "metadataJson":  { "type": "string", "description": "Свёрнутый JSON со вспомогательными полями (email, function, timestamp, tokens, prompt_tokens?, completion_tokens?)" }
  },
  "additionalProperties": true
}
```

### Схема ответа (что наш сервер ждёт от вас)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "dateTime": { "type": ["string", "null"], "format": "date-time",
                  "description": "Момент списания на вашей стороне (ISO 8601)" },
    "email":    { "type": ["string", "null"], "description": "Для сверки" },
    "charged":  { "type": ["number", "null"], "description": "Списано кредитов" },
    "balance":  { "type": ["number", "null"], "description": "Остаток кредитов" }
  },
  "additionalProperties": true
}
```

> `additionalProperties: true` — мы игнорируем лишние поля, так что расширять
> ответ можно свободно.

---

## 12. Частые вопросы (FAQ)

**В: Какой кредит у пользователя изначально? Чья это ответственность?**
О: Полностью ваша. Наш сервер не знает и не хранит начальные балансы — он только
записывает `balance` из ваших ответов. Заведение пользователя, пополнения,
стартовый баланс — на вашей стороне.

**В: Может ли `model` быть пустой строкой?**
О: Да. Если upstream не сообщил модель — придёт `""`. Рассчитывайте на это.

**В: Обязательно ли возвращать `email`?**
О: Нет. Наш сервер берёт `email` из **своего** запроса (это надёжно), поле в
ответе — чисто для контроля/дебага. Можете вернуть `null` или вообще не
включать.

**В: Что если я не знаю остаток (`balance`) на момент ответа?**
О: Верните `balance: null` (или не включайте поле). Пересылка считается
успешной, но реестр балансов у нас не обновится — просто колонка «Остаток»
останется пустой для этой записи.

**В: Что если у пользователя не хватило кредитов?**
О: Это ваше бизнес-решение. Варианты:
- отказать (`402`/`409`) — наш сервер пометит `failed`;
- списать в минус и вернуть отрицательный `balance` (`200`) — на ваше усмотрение;
- частичное списание — верните фактический `charged` и итоговый `balance`.

**В: Нужно ли поддерживать несколько одновременных запросов?**
О: Наш сервер шлёт запросы последовательно в одном потоке пересылки, но ретраи и
пики могут идти плотнее. Делайте обработку потокобезопасной (БД с транзакциями),
но горизонтальная нагрузка невелика.

**В: Меняется ли контракт со временем?**
О: Расширения — назад-совместимы (новые опциональные поля). Любое изменение
**существующих** полей согласуется с вами заранее. Поэтому стройте парсер так,
чтобы неизвестные поля игнорировались (а не вызывали ошибку).

**В: Как нам тестировать вместе с вашим сервером?**
О: Поднимите свой эндпоинт, сообщите URL и `FORWARD_API_KEY`. Мы пропишем в
`.env` или через GUI → «Настройки». После — каждое использование модели в Open
WebUI будет триггерить ваш эндпоинт. В GUI нашего сервера на вкладке «Пересылка»
виден статус (`ok`/`failed`), колонки «Списано/Остаток/Время ответа», а на
вкладке «Балансы» — реестр по email.

**В: Можно ли нам инициировать запрос к вашему серверу (обратное направление)?**
О: Сейчас — нет. Контракт однонаправленный: мы → вы (запрос), вы → мы (ответ).
Если нужно присылать корректировки/пополнения — обсудим отдельный эндпоинт.

---

## 13. Шпаргалка по полям

### Запрос (мы → вы), `PUT <FORWARD_URL>`

```
openRouterWebUiUserId  string          обязательно   ID пользователя Open WebUI
messageCost            number ≥ 0      обязательно   стоимость в USD
modelId                string          обязательно   имя модели (= modelName)
modelName              string          обязательно   имя модели (= modelId)
requestText            string          обязательно   всегда ""
responseText           string          обязательно   всегда ""
requestDate            string (ISO)    обязательно   старт запроса к модели
responseDate           string (ISO)    обязательно   завершение запроса к модели
isSuccess              boolean         обязательно   успех вызова модели
errorMessage           string|null     обязательно   текст ошибки или null
metadataJson           string (JSON)   обязательно   свёрнутый JSON: email, function,
                                                       timestamp, tokens, [breakdown]
```

### Ответ (вы → мы), `200 OK`

```
datetime   string|null   момент списания у вас (ISO 8601)  [поле ответа: dateTime]
email      string|null   для сверки
charged    number|null   списано (USD)
balance    number|null   остаток (USD)  ← обновляет реестр, если число
```

### Ключевые правила

- ✅ `2xx` + JSON-контракт → `ok`, реестр обновлён (если `balance` — число).
- ✅ `balance: 0` валиден.
- ⚠️ `charged`/`balance` должны быть **числом** (строка/`true` считаются отсутствующими).
- ❌ `≥ 400` или таймаут → `failed`, будет ретрай → **делайте идемпотентность**.
- 🔑 `Authorization: Bearer <FORWARD_API_KEY>` — проверяйте constant-time.
- ⏱ Таймаут нашего запроса — 10 секунд.

---

*Документ соответствует версии сервера на 2026-08-10 (контракт devbim.com
`updateSubscription`: метод PUT, поля `openRouterWebUiUserId`/`messageCost`/
`isSuccess`/`metadataJson`). При изменениях контракта версионность ведётся в
`README.md` (раздел «Ответ внешнего сервера»).*
