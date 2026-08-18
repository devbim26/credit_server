"""fake_openrouter_server.py — локальный имитатор OpenRouter для теста.

Отвечает на POST /v1/chat/completions так же, как настоящий OpenRouter:
возвращает ответ модели + usage {prompt_tokens, completion_tokens, total_tokens},
а также usage.cost {total_cost} — реальную стоимость в $, как делает OpenRouter.

Токены считаются детерминированно по длинам prompt/answer, стоимость — по
фиктивному тарифу ($0.50/млн input, $2.00/млн output), чтобы их можно было
предсказать в тестах. Запуск:
    python fake_openrouter_server.py
слушает на http://localhost:4030
"""
from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Fake OpenRouter")


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[fake-openrouter] {ts} {msg}", file=sys.stdout, flush=True)


def _count_tokens(text: str) -> int:
    """Детерминированная «токенизация»: ~1 токен на 4 символа."""
    return max(1, math.ceil(len(text or "") / 4))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "test-model")
    messages = body.get("messages", []) or []

    # Берём последний user-промпт.
    prompt = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            prompt = m.get("content", "") if isinstance(m.get("content"), str) else ""
            break

    # Ответ модели — эхо + подтверждение.
    answer = (
        f"[fake-openrouter echo] Вы спросили: «{prompt[:120]}». "
        f"Это тестовый ответ модели {model}."
    )

    prompt_tokens = _count_tokens(prompt)
    completion_tokens = _count_tokens(answer)
    total = prompt_tokens + completion_tokens

    # Стоимость в $ по фиктивному тарифу (имитация OpenRouter usage.cost).
    COST_IN = 0.50   # $/млн input
    COST_OUT = 2.00  # $/млн output
    cost_usd = round(
        (prompt_tokens / 1_000_000.0) * COST_IN
        + (completion_tokens / 1_000_000.0) * COST_OUT,
        6,
    )

    _log(
        f"model={model!r} prompt_tok={prompt_tokens} completion_tok={completion_tokens} "
        f"total={total} cost=${cost_usd}"
    )

    return JSONResponse(
        {
            "id": f"chatcmpl-fake-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total,
                "cost": {"total_cost": cost_usd},
            },
        }
    )


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4030, log_level="warning")
