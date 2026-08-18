# Сервер списания кредитов

FastAPI-сервис, который:
1. **Принимает** от функций Open WebUI (через сниппет `credit_reporter.py`)
   данные об использовании — email, функция, модель, дата/время, токены.
2. **Конвертирует** токены в кредиты по курсу модели (курс настраивается в GUI).
3. **Хранит** всё в SQLite и показывает статистику в веб-GUI.
4. **Пересылает** ту же сводку на «другой сервер» (фоновыми задачами, с retry).

## Состав

```
Сервер списание кредитов/
├── server.py            # FastAPI: endpoints, init, .env
├── db.py                # SQLite-слой (aiosqlite)
├── converter.py         # токены → кредиты + расчёт стоимости в $ ($/млн токенов)
├── forwarder.py         # пересылка на «другой сервер» + retry failed
├── templates/admin.html # веб-GUI (сводка $/кредиты/вызовы, курсы, статистика, логи, пересылка)
├── analytics/           # дашборд аналитики вызовов (/dashboard: графики, пользователи, логи, туннель)
├── credit_reporter.py   # сниппет-библиотека (для локального теста)
├── openwebui_pipe_credits.py  # ← ФУНКЦИЯ Open WebUI (один автономный файл, вставлять целиком)
├── requirements.txt
├── .env.example         # шаблон конфигурации
├── start.bat / stop.bat            # запуск/останов бэкенда на Windows
├── start-gui.bat                   # запуск бэкенда + открытие админки в браузере
├── start-system-credits.bat        # ПОЛНЫЙ запуск: бэкенд + туннель (2 окна; для START servers)
├── run-tunnel-credits.bat          # запуск только Cloudflare-туннеля (credits.dev-bim.com)
├── config-credits.yml              # копия конфига туннеля (оригинал в ~/.cloudflared)
├── test_pipe/                      # сквозной E2E тест (mock-серверы)
│   ├── fake_openrouter_server.py   # имитатор OpenRouter на :4030 (отвечает с usage)
│   ├── mock_external_server.py     # внешний сервер на :4020 (сохраняет посылки в файл)
│   ├── openrouter_pipe.py          # версия pipe для локального теста
│   └── credit_reporter.py          # копия сниппета
└── README.md
```

## Установка

```bash
cd "Models FIN/Сервер списание кредитов"
python -m venv venv            # необязательно, но рекомендуется
venv\Scripts\activate          # или: venv\Scripts\python.exe -m pip ...
pip install -r requirements.txt
```

## Конфигурация

Скопируйте `.env.example` в `.env` и заполните:

| Переменная | Назначение |
|---|---|
| `CREDITS_API_KEY` | Bearer-ключ приёма данных от сниппета. Пусто = auth выключен (только dev). |
| `ADMIN_KEY` | Bearer-ключ GUI/админки. Пусто = админка без пароля (только dev). |
| `FORWARD_URL` | Куда пересылать сводку. Пусто = пересылка выключена. |
| `FORWARD_API_KEY` | Bearer-ключ для «другого сервера». |
| `BASE_URL` | Внешний адрес этого сервера (для ссылок/логов). Должен быть публичным, если сервер публикуется через Cloudflare: `https://credits.dev-bim.com`. Локально — `http://localhost:4010`. |
| `DB_PATH` | Файл SQLite. По умолчанию `credits.db`. |
| `HOST` / `PORT` | Хост/порт uvicorn. По умолчанию `0.0.0.0:4010`. |

> Порт **4010** выбран, чтобы не конфликтовать с `doc_server` (8000).

## Запуск

На Windows (рекомендуемый способ):

```bat
start.bat                :: запуск в новом окне ("credits-server")
start-gui.bat            :: то же + открывает админку в браузере
start-dashboard.bat      :: то же + открывает дашборд аналитики (/dashboard)
start-system-credits.bat :: ПОЛНЫЙ запуск: бэкенд + туннель (два окна)
stop.bat                 :: останов (по PID слушателя порта)
```

**Единая панель START servers.** Сервер зарегистрирован в
`C:\ПРОЕКТЫ\START servers\` (slug `credits`) и управляется наравне с
остальными шестью: `start-one.bat credits`, `status.bat`, `stop-all.bat`.
Его лаунчер там — `start-system-credits.bat` (идемпотентен: если порт уже
слушается, открывает только окно туннеля).

`start.bat` / `start-gui.bat` сами находят Python (venv → `python` → `py -3`),
читают `.env` и поднимают uvicorn. Если порт уже занят — не запускают второй
экземпляр; `start-gui.bat` в этом случае просто открывает GUI в браузере.

Вручную (любая ОС):

```bash
uvicorn server:app --host 0.0.0.0 --port 4010
```

GUI: **http://localhost:4010/admin** (корень `/` тоже редиректит на `/admin`)
Дашборд аналитики: **http://localhost:4010/dashboard** (`start-dashboard.bat`)
Health: http://localhost:4010/health

## Дашборд аналитики (/dashboard)

Самодостаточный пакет `analytics/` (своя SQLite `data/analytics.db`, JSON-API `/stats/*`).
Каждый принятый `POST /api/usage` пишется в аналитику: модель, breakdown токенов,
$ от провайдера (`cost_usd`), email/GUID, имя функции (агента), latency (из
`request_date`/`response_date`), ошибки (`is_success=false`). Пересылку на
devbim.com дашборд **не** дублирует — её по-прежнему делает `forwarder.py`.

Страницы: входящие по email · обработка запросов · внешние отчёты · настройки ·
тест (отправка тестового отчёта в `/api/usage`) · живые логи + управление туннелем.

- **Локально** (`http://localhost:4010/dashboard`) — авто-логин, ключ не нужен.
- **Через туннель** (`https://credits.dev-bim.com/dashboard`) — один раз ввести
  `ANALYTICS_API_KEY` из `.env` (auth включён, т.к. сервер публичный).
- Туннель `credits` виден дашборду (статус/кнопки на странице «Логи»); туннель,
  запущенный `run-tunnel-credits.bat`, помечается как внешний (не гасится рестартом).

## Публикация через Cloudflare-туннель (для удалённого Open WebUI)

Если Open WebUI работает в Docker/на другой машине, ему нужен публичный адрес
сервера. Используется отдельный Cloudflare named-tunnel `credits`
(домен `credits.dev-bim.com` → `http://localhost:4010`).

Конфиг туннеля: `~/.cloudflared/config-credits.yml` (UUID
`db3069c5-b8e7-491d-a433-b78f96c3ae57`; копия — `config-credits.yml` в корне
проекта). Запуск:

```bat
start-system-credits.bat  :: бэкенд + туннель (два окна; стандартный способ)
run-tunnel-credits.bat    :: только туннель (если бэкенд уже поднят)
```

> 2026-08-18: туннель перенесён на текущий хост — credentials восстановлены
> через `cloudflared tunnel token credits` → `~/.cloudflared/db3069c5-….json`
> (схема миграции docx-gen). DNS `credits.dev-bim.com` уже существовал и
> указывал на правильный UUID (проверено через Cloudflare API).

> ⚠️ `run-tunnel-credits.bat` намеренно НЕ использует `where cloudflared` —
> в `C:\Windows\System32\cloudflared.exe` лежит заглушка 0 байт, которая даёт
> «Отказано в доступе». Берётся рабочий экземпляр из
> `C:\Program Files (x86)\cloudflared\`.

Проверка публичной доступности:

```bash
curl https://credits.dev-bim.com/health   # → {"status":"ok"}
```

После этого в Valves функции Open WebUI ставьте
`CREDITS_SERVER_URL=https://credits.dev-bim.com` (НЕ `localhost` — из Docker
он не видит хост).

## Подключение к Open WebUI

### Вариант A (рекомендуемый): готовая функция-агент

Файл **`openwebui_pipe_credits.py`** — это автономная функция (Pipe) Open WebUI
со встроенным списанием. Логика учёта встроена прямо в файл, ничего
дополнительно ставить не нужно (Open WebUI грузит функцию как один файл).

1. Open WebUI → **Администратор → Функции → + Новая функция**
2. Имя: `Агент со списанием кредитов`
3. Удалить шаблонный код, **вставить всё содержимое `openwebui_pipe_credits.py`**
4. Сохранить → включить функцию
5. ⚙ **Valves** — заполнить:

   | Поле | Значение |
   |---|---|
   | `API_BASE_URL` | `https://openrouter.ai/api/v1` (см. «Подключение ImageRouter» ниже) |
   | `API_KEY` | ваш ключ OpenRouter (`sk-or-v1-...`) |
   | `MODEL_NAME` | имя модели (например `google/gemini-3.5-flash`). **Это же — ключ курса** в GUI сервера (вкладка «Курсы»). |
   | `CREDITS_SERVER_URL` | `https://credits.dev-bim.com` (публичный) или `http://localhost:4010` (локально) |
   | `CREDITS_API_KEY` | ключ из `CREDITS_API_KEY` в `.env` сервера |

> ⚠️ Функция принудительно шлёт `stream: false`, чтобы получить цельный JSON с
> полем `usage` (prompt_tokens/completion_tokens). Не включайте стриминг.

> ⚠️ **Tool-calling и reasoning-модели.** Пайп — тонкий прокси: он делает один
> запрос и **не выполняет цикл tool-calling**. Если модель отвечает вызовом
> инструментов (`finish_reason: tool_calls`, `content: null`) — пользователь
> увидит понятное сообщение «ℹ️ Модель решила обратиться к инструментам...»,
> а не сырой JSON. Стоимость такого запроса всё равно учитывается.
> У reasoning-моделей (GLM-4.5/5.2, DeepSeek-R1...) текст берётся из
> `reasoning_content`, если `content` пуст. Для полноценной работы инструментов
> используйте функцию Open WebUI с поддержкой tool-calling.

### Подключение провайдера ImageRouter (imagerouter.io)

[ImageRouter](https://docs.imagerouter.io/) — шлюз по типу OpenRouter (модели
вида `openai/gpt-4o-mini`, `google/...`), полностью OpenAI-совместимый. Подходит
тем же пайпом без доработок кода:

1. Получите ключ: [imagerouter.io/api-keys](https://imagerouter.io/api-keys).
2. В Valves той же функции «Агент со списанием кредитов»:

   | Поле | Значение |
   |---|---|
   | `API_BASE_URL` | `https://api.imagerouter.io/v1/openai` — **именно `/v1/openai`**, а не `/v1` |
   | `API_KEY` | ключ ImageRouter |
   | `MODEL_NAME` | ID модели с ImageRouter (например `openai/gpt-4o-mini`). **Это же — ключ курса** в GUI сервера. Список моделей: `GET https://api.imagerouter.io/v3/models` |

3. Списание кредитов на нашей стороне работает автоматически:
   ImageRouter отдаёт `usage.cost` (число, upstream-стоимость в USD) — пайп
   пробрасывает её как `cost_usd`, сервер фиксирует `cost_source="provider"`
   (🟢 зелёная точка в GUI). Тарифы $/млн задавать **не нужно**.

> ⚠️ ImageRouter округляет списание своих кредитов вверх до 1/10 000 $
> (верхнеуровневое поле `cost` ответа) — для сверки используйте `usage.cost`,
> именно её мы и записываем.

### Вариант B: встроить списание в свою функцию

Если у вас уже есть свой Pipe и нужно только добавить отчёт — используйте
`credit_reporter.py` как библиотеку рядом с файлом функции:

```python
from credit_reporter import report_usage, estimate_tokens, extract_cost_usd

# resp — словарь ответа модели (НЕ стриминг!)
usage  = (resp or {}).get("usage", {}) or {}
model  = (resp or {}).get("model", "") or self.valves.MODEL_NAME
tokens = usage.get("total_tokens") or estimate_tokens(collected_text)
cost_usd = extract_cost_usd(usage)   # $ от провайдера (OpenRouter); для z.ai — None

report_usage(                       # fire-and-forget, НЕ блокирует
    email=__user__.get("email", ""),
    function="Название вашей функции",
    model=model,
    tokens=tokens,
    prompt_tokens=usage.get("prompt_tokens"),      # опционально, для split-тарифа
    completion_tokens=usage.get("completion_tokens"),
    cost_usd=cost_usd,                            # опционально, $ от провайдера
)
```

URL/ключ сниппет читает из переменных окружения (`CREDITS_SERVER_URL`,
`CREDITS_API_KEY`) или из атрибутов модуля `cr.SERVER_URL` / `cr.API_KEY`.

> Сниппет **никогда не роняет** работу функции: при любой ошибке (сервер
> недоступен, неверный ключ) он лишь пишет сообщение в `docker logs`.

## Как считается курс

Поддерживаются два режима тарификации:

**1. Единый курс (по умолчанию):**
- Курс хранится как **«кредитов за 1000 токенов»** (базовый) для каждой модели.
- Если модель не задана в таблице — применяется строка `__default__`.
- `кредиты = round(токены / 1000 × базовый_курс, 2)`.

**2. Раздельный курс input/output (опционально):**
- В курсе модели дополнительно задаются `credits_per_1k_input` и/или
  `credits_per_1k_output` (через GUI или API).
- Применяется, **только если** в отчёте передан breakdown
  (`prompt_tokens` + `completion_tokens`) **и** заданы оба раздельных курса.
- `кредиты = round(prompt_tokens/1000 × input + completion_tokens/1000 × output, 2)`.
- Если задан только один из input/output — недостающий берётся по базовому курсу.

Курсы задаются в GUI (вкладка «Курсы») или через `POST /api/rates`.


## Стоимость в долларах ($)

Помимо внутренних «кредитов», сервер считает **реальную стоимость запроса
в долларах** — то, что фактически списывается провайдером. Колонка $ есть в
«Логе использования», «Статистике» и «Пересылке», а в шапке — общая сумма.

**Провайдеры ведут себя по-разному**, поэтому используется гибридная стратегия:

| Провайдер | Отдаёт $ в ответе? | Откуда берём $ |
|---|---|---|
| **OpenRouter** | ✅ да, `usage.cost` | напрямую из ответа (самая точная величина) |
| **ImageRouter** | ✅ да, `usage.cost` (число) | напрямую из ответа (upstream-стоимость в USD) |
| **z.ai / GLM** | ❌ нет | считаем по тарифам $/млн токенов из GUI |

**Приоритет источника $:**
1. **От провайдера** (`cost_source = "provider"`) — если пайп передал `cost_usd`
   (OpenRouter отдал `usage.cost`). Это то, что реально списано — приоритет.
2. **Расчёт по тарифам** (`cost_source = "computed"`) — если провайдер не отдал $,
   но в курсе модели заданы `Input $/млн` / `Output $/млн` и передан breakdown
   токенов: `cost = prompt_tokens/1e6 × input + completion_tokens/1e6 × output`.
   Так работает z.ai/GLM и любой провайдер без поля cost.
3. **Нет данных** — тарифы $ не заданы → колонка $ пуста. Кредиты считаются как
   обычно, на $ это не влияет.

В интерфейсе источник виден по цвету точки рядом с суммой:
- 🟢 зелёная — реально списано провайдером (`provider`);
- ⚪ серая — посчитано по тарифам (`computed`).

### Как задать тарифы $/млн

В GUI → вкладка «Курсы» для каждой модели — поля **Input $/млн** и **Output $/млн**
(в долларах за 1 миллион токенов — именно так публикуют цены все провайдеры).

Актуальные тарифы z.ai (на 2026-08, уточняйте в [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing)):

| Модель | Input $/млн | Output $/млн |
|---|---|---|
| GLM-4.5 | 0.60 | 2.20 |
| GLM-4.5-Air | 0.20 | 1.10 |
| GLM-5.2 | 1.40 | 4.40 |

> Для OpenRouter тарифы $/млн задавать **не нужно** — он сам отдаёт `usage.cost`.


## API (кратко)

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/usage` | принять данные от сниппета (Bearer `CREDITS_API_KEY`) |
| GET  | `/admin` | веб-GUI |
| GET  | `/dashboard` | дашборд аналитики (графики/пользователи/логи/туннель; `ANALYTICS_API_KEY` при доступе через туннель) |
| GET  | `/stats/*` | JSON-API дашборда (summary, timeline, models, requests, emails, logs, tunnel) |
| GET/POST/DELETE | `/api/rates[/{model}]` | управление курсами (Bearer `ADMIN_KEY`) |
| GET | `/api/records` | лог использования (пагинация, фильтр) |
| GET | `/api/stats?group_by=email\|function\|model` | агрегаты (сумма $, кредитов, токенов) |
| GET | `/api/summary` | сводные итоги для шапки (всего $, кредитов, вызовов) |
| GET | `/api/forward?status=...` | очередь пересылки |
| GET | `/api/balances` | реестр балансов пользователей (из ответов внешнего сервера) |
| POST | `/api/retry-failed` | перепослать неудачные |
| GET/POST | `/api/settings` | URL/ключ «другого сервера» |
| GET | `/health` | проверка живости |

### Формат `/api/usage`

**Запрос (единый курс):**
```json
{
  "email": "user@example.com",
  "function": "Проверка нормативов",
  "model": "pdf-standards-parser",
  "timestamp": "2026-08-07T12:34:56+00:00",
  "tokens": 1234
}
```
`timestamp`, `model` можно опустить (`timestamp` = now UTC, `model` → `__default__`).

**Запрос (раздельный тариф — опционально):**
```json
{
  "email": "user@example.com",
  "function": "Проверка нормативов",
  "model": "gpt-4o",
  "tokens": 1500,
  "prompt_tokens": 1200,
  "completion_tokens": 300,
  "cost_usd": 0.00123
}
```
`prompt_tokens`/`completion_tokens` включает раздельный тариф input/output,
если для модели заданы такие курсы; иначе считается по базовому курсу.
`cost_usd` — стоимость в $ от провайдера (OpenRouter отдаёт `usage.cost`).
Если не передать — сервер посчитает $ сам по тарифам $/млн из курса модели.

**Ответ:**
```json
{
  "ok": true,
  "credits": 1.23,
  "rate": 1.0,
  "matched_model": "__default__",
  "rate_input": null,
  "rate_output": null,
  "pricing": "base",
  "cost_usd": 0.00123,
  "cost_source": "provider"
}
```
`pricing` = `"base"` (единый курс) или `"split"` (раздельный input/output).
`cost_usd` / `cost_source` — итоговая стоимость в $ и её источник
(`"provider"` — отдал провайдер; `"computed"` — посчитали по тарифам; `null` —
тарифов $ нет, стоимость неизвестна).

### Тело пересылки на «другой сервер»

`PUT`-запрос на `FORWARD_URL` (контракт devbim.com
`/api/OpenRouterModels/updateSubscription`), заголовок
`Authorization: Bearer <FORWARD_API_KEY>` добавляется, если ключ задан:

```json
{
  "openRouterWebUiUserId": "6cce058d-bddf-4c98-96be-bc0494fd32eb",
  "messageCost": 0.0000185,
  "modelId": "openai/gpt-4o",
  "modelName": "GPT-4o",
  "requestText": "",
  "responseText": "",
  "requestDate": "2026-08-18T12:34:56.789123",
  "responseDate": "2026-08-18T12:35:10.123456",
  "isSuccess": true,
  "errorMessage": null,
  "metadataJson": "{\"email\":\"user@example.com\",\"function\":\"Агент со списанием кредитов\",\"timestamp\":\"2026-08-18T12:35:10+00:00\",\"tokens\":185,\"prompt_tokens\":100,\"completion_tokens\":85}"
}
```

- `openRouterWebUiUserId` — GUID пользователя Open WebUI (`user_id` из отчёта
  пайпа, `__user__.id`). Поле типизировано на devbim.com как `System.Guid`:
  email или пустая строка → 400, GUID без заведённого ключа доступа → 404
  «Ключ доступа пользователя не был найден» (решение 2026-08-18: остаёмся на
  GUID; email идёт справочно в `metadataJson`).
- `requestText`/`responseText` **сознательно пустые** — содержимое диалогов
  не покидает границы Open WebUI.
- `metadataJson` — свёрнутый JSON с исходными полями нашего учёта (`email`,
  `function`, `timestamp`, `tokens` + breakdown, при наличии).

> 📄 **Полная документация для разработчика внешнего сервиса** (контракт
> запрос/ответ, идемпотентность, аутентификация, retry, эталонная реализация,
> curl-примеры, JSON-схемы): **[`docs/EXTERNAL_SERVICE_INTEGRATION.md`](docs/EXTERNAL_SERVICE_INTEGRATION.md)**.

#### Ответ внешнего сервера

Внешний сервер отвечает на каждый POST `200 OK` с JSON-контрактом:

```json
{
  "dateTime": "2026-08-08T13:59:41+00:00",
  "email": "user@example.com",
  "charged": 0.0000185,
  "balance": 9.9999815
}
```

| Поле | Назначение |
|---|---|
| `dateTime` | Дата/время списания на стороне внешней системы |
| `email` | Для сверки (от кого списали) |
| `charged` | Сколько списано внешней системой (USD) |
| `balance` | **Остаток пользователя** после списания (USD) |

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

## Быстрая проверка после установки

1. **Сервер жив:** `curl http://localhost:4010/health` → `{"status":"ok"}`
2. **GUI:** `http://localhost:4010/admin` (корень `/` тоже редиректит на `/admin`)
3. **Туннель:** `curl https://credits.dev-bim.com/health` → `{"status":"ok"}`
4. **Приём отчёта:** отправьте сообщение через функцию в Open WebUI, затем
   откройте GUI → «Лог использования» — должна появиться запись с вашим email,
   breakdown токенов (in/out), стоимостью $ и посчитанными кредитами.
5. **Пересылка:** GUI → «Пересылка» — статус записи (`ok` / `failed` / `pending`),
   колонки «Списано» / «Остаток» / «Время ответа» (из ответа внешнего сервера).
   При `failed` можно нажать «Перепослать failed».
6. **Балансы:** GUI → «Балансы» — актуальный остаток кредитов по каждому email
   (обновляется из поля `balance` в ответе внешнего сервера).

> ℹ️ **Интерфейс не обновляется автоматически** — он грузит данные один раз при
> открытии вкладки. После серии запросов нажмите «Обновить» или F5.
> Время в таблицах показывается **локальное** (часовой пояс браузера); в БД
> хранится в UTC.

## Типичные проблемы (troubleshooting)

### Функция в Open WebUI: «All connection attempts failed»
Причина: `API_BASE_URL` неверный или OpenRouter недоступен.
Решение: проверьте, что `API_BASE_URL=https://openrouter.ai/api/v1` и ключ
`API_KEY` валиден (`curl https://openrouter.ai/api/v1/auth/key -H "Authorization: Bearer ВАШ_КЛЮЧ"`).

### Функция: «No endpoints found for <model>»
Причина: модель с таким именем не существует у OpenRouter.
Решение: сверьте имя с актуальным списком `GET https://openrouter.ai/api/v1/models`.

### Функция: «Expecting value: line 1 column 1 (char 0)»
Причина: Open WebUI шлёт `stream:true`, ответ приходит SSE-потоком, а парсер ждёт JSON.
Решение: pipe-функция принудительно ставит `stream:false` (см. `openwebui_pipe_credits.py`).
Если ошибка вернулась — вы используете старую версию файла, переустановите его целиком.

### Модель ответила, но на сервере кредитов НЕТ записи
Причина: функция не достучалась до сервера кредитов (чаще всего — `localhost` из Docker).
Решение: в Valves поставьте `CREDITS_SERVER_URL=https://credits.dev-bim.com`
(публичный адрес), а **НЕ** `http://localhost:4010`. Из контейнера `localhost`
указывает на сам контейнер, а не на хост.

### В браузере `http://localhost:4010/` не открывается
Корень `/` редиректит на `/admin`. Если не открывается — сервер не запущен
(`start.bat`). Сам GUI: `http://localhost:4010/admin`.

### `run-tunnel-credits.bat`: «Отказано в доступе»
В `C:\Windows\System32\cloudflared.exe` лежит заглушка 0 байт — её запуск запрещён.
Bat-файл намеренно берёт рабочий экземпляр из
`C:\Program Files (x86)\cloudflared\cloudflared.exe`. Не используйте `where cloudflared`.

### `start.bat`: «Непредвиденное появление: .»
Старые bat-файлы с сыплющимися спецсимволами в `echo` (`^|`, скобки). Текущая
версия `start.bat` этого не содержит — обновите из репозитория.

### Записи в очереди пересылки в статусе `failed`
На подсчёт и хранение кредитов это **не влияет**. Типичные причины по коду:
- **400 + `openRouterWebUiUserId ... System.Guid`** — у записи нет `user_id`
  (старые отчёты до появления поля / тестовые). Такие записи переслать нельзя.
- **404 + «Ключ доступа пользователя не был найден»** — GUID корректен, но у
  пользователя нет ключа доступа/подписки на стороне devbim.com. Нужно
  зарегистрировать пользователя там; затем «Перепослать failed».
- Сетевые ошибки / таймауты — `FORWARD_URL` недоступен; досылается кнопкой
  «Перепослать failed» после восстановления.

### Функция: «object of type 'NoneType' has no len()»
Причина: модель вернула `content: null` (function/tool-calling или
reasoning-модель — текст лежит в `reasoning_content`), а старая версия пайпа
звала `len(answer)` от `None`.
Решение: обновите код функции в Open WebUI (вставьте `openwebui_pipe_credits.py`
целиком). Новая версия берёт `reasoning_content`, а при tool-call показывает
понятное сообщение вместо краха.

### GUI: таблицы пустые, хотя записи в БД есть
Причина: не введён `ADMIN_KEY` (или устаревшая вкладка браузера). Без ключа все
запросы к `/api/*` отклоняются как 401 → таблицы остаются пустыми.
Решение: введите `ADMIN_KEY` (значение из `.env`, по умолчанию `devbim2026`) в
поле справа вверху → «Сохранить». Затем обновите страницу (F5).

### GUI: после обновления сервера изменения не видны
Сервер держит код в памяти до перезапуска. После правки `server.py`/`db.py`
обязательно перезапустите: `stop.bat` → `start.bat`. Шаблон `admin.html`
правится «на лету» (без перезапуска) — достаточно обновить страницу (F5).

### GUI: текст вкладки исчезает при нажатии
Это известный конфликт утилитарного класса `bg-white` (Tailwind CDN) со стилем
активной вкладки. Исправлено: `.tab-active` использует `!important`. Если
воспроизводится — обновите страницу с очисткой кэша (Ctrl+F5).

## Безопасность

- Сравнение ключей — constant-time (`hmac.compare_digest`).
- `FORWARD_API_KEY` не возвращается через `GET /api/settings` (только флаг наличия).
- Для продакшена **всегда** задавайте `CREDITS_API_KEY` и `ADMIN_KEY`.
- `.env` должен быть в `.gitignore` (в проекте `.env` не коммитится).
