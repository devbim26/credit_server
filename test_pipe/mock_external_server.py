"""mock_external_server.py — имитатор внешнего сервера devbim.com.

Принимает PUT-пересылку контракта updateSubscription от нашего сервера
списания кредитов и СОХРАНЯЕТ каждую посылку в файл, чтобы можно было
убедиться, что данные дошли именно в том виде, в котором отправлялись.

Контракт (PUT /api/OpenRouterModels/updateSubscription):
    {openRouterWebUiUserId, messageCost, modelId, modelName,
     requestText, responseText, requestDate, responseDate,
     isSuccess, errorMessage, metadataJson}

Ответ (по контракту devbim.com — {dateTime, email, charged, balance}), чтобы
наш сервер наполнял вкладку «Балансы». charged ≈ messageCost (USD); стартовый
баланс 10.0 USD на новый email.

РЕЖИМ ОТКАЗА (для проверки failed/retry на нашем сервере):
    GET /toggle-fail        — переключить режим отказа вкл/выкл
    GET /fail-status        — посмотреть текущее состояние
Когда режим отказа включён, эндпоинт отвечает HTTP 503 и файл не сохраняет.

Запуск:
    python mock_external_server.py
слушает на http://localhost:4020
Файлы посылок: ./received/<timestamp>_<email>.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock External Server (devbim.com contract)")

RECEIVED_DIR = Path(__file__).resolve().parent / "received"
RECEIVED_DIR.mkdir(exist_ok=True)

# Глобальный флаг режима отказа (имитация недоступности внешнего сервера).
FAIL_MODE = False

# In-memory балансы пользователей (для теста). Новый email стартует с 10.0 USD
# (реалистичный масштаб под боевой devbim.com: charged/balance — в USD).
START_BALANCE = 10.0
BALANCES: dict[str, float] = {}


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[mock-external] {ts} {msg}", file=sys.stdout, flush=True)


def _safe(s: str, maxlen: int = 40) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._@-]+", "_", s)[:maxlen].strip("_")
    return s or "unknown"


def _email_from_body(body: dict) -> str:
    """Email живёт в metadataJson (свёрнутый JSON). Достаём оттуда."""
    raw = body.get("metadataJson")
    if not raw:
        return "unknown"
    try:
        meta = json.loads(raw)
    except Exception:
        return "unknown"
    return meta.get("email") or "unknown"


@app.put("/api/OpenRouterModels/updateSubscription")
async def update_subscription(request: Request):
    if FAIL_MODE:
        _log("REJECTED (fail mode) — отвечаю 503")
        return JSONResponse(
            {"error": "fail mode is ON (mocked outage)"}, status_code=503
        )
    body = await request.json()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    email = _safe(_email_from_body(body))
    fname = RECEIVED_DIR / f"{ts}_{email}.json"
    with fname.open("w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)

    # Расчёт списания для in-memory баланса (детерминированно). charged — в USD,
    # как в боевом контракте devbim.com (≈ messageCost). При отсутствии/нулевом
    # messageCost — грубая оценка по токенам (~$1.85 за 1M токенов), чтобы
    # вкладка «Балансы» двигалась даже без $-тарифа на нашем сервере.
    message_cost = body.get("messageCost")
    try:
        tokens = float(json.loads(body.get("metadataJson") or "{}").get("tokens") or 0)
    except Exception:
        tokens = 0.0
    if message_cost:
        charged = round(float(message_cost), 6)
    elif tokens:
        charged = round(tokens * 1.85e-6, 6)
    else:
        charged = 0.0

    real_email = _email_from_body(body)
    BALANCES[real_email] = BALANCES.get(real_email, START_BALANCE) - charged
    balance = round(BALANCES[real_email], 6)

    resp = {
        "dateTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "email": real_email,
        "charged": charged,
        "balance": balance,
    }
    _log(
        f"SAVED {fname.name} uid={body.get('openRouterWebUiUserId')!r} "
        f"email={real_email!r} charged={charged} balance={balance}"
    )
    return JSONResponse(resp)


@app.get("/toggle-fail")
def toggle_fail():
    """Переключить режим отказа вкл/выкл. Для теста failed/retry."""
    global FAIL_MODE
    FAIL_MODE = not FAIL_MODE
    _log(f"FAIL_MODE -> {FAIL_MODE}")
    return {"fail_mode": FAIL_MODE}


@app.get("/fail-status")
def fail_status():
    return {"fail_mode": FAIL_MODE}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "fail_mode": FAIL_MODE,
        "received_count": len(list(RECEIVED_DIR.glob("*.json"))),
        "balances": dict(BALANCES),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4020, log_level="warning")
