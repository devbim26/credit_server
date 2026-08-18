"""Конвертация токенов в кредиты по курсу модели и расчёт стоимости в $.

Поддерживаются два режима тарификации КРЕДИТОВ:

1. ЕДИНЫЙ курс (по умолчанию):
   credits = total_tokens / 1000 * base
   Где base = credits_per_1k_tokens (берётся по модели, иначе __default__).

2. РАЗДЕЛЬНЫЙ курс input/output (когда задан breakdown токенов
   и в курсе модели заданы credits_per_1k_input/output):
   credits = prompt_tokens/1000 * input + completion_tokens/1000 * output
   Если раздельный курс задан только частично (например, только input),
   недостающая часть считается по base — это предсказуемый fallback.

СТОИМОСТЬ В ДОЛЛАРАХ (compute_cost_usd) — реальная цена запроса:
1. Если провайдер вернул $ в ответе (OpenRouter: usage.cost) — берём напрямую
   (source="provider"). Это самая точная величина, то что реально списано.
2. Иначе считаем по тарифам $/млн токенов из курса модели
   (source="computed"). Так работает z.ai/GLM и любой провайдер без поля cost:
   cost = prompt_tokens/1e6 * cost_in + completion_tokens/1e6 * cost_out
3. Если тарифов $ нет — стоимость неизвестна (None, None).
"""
from __future__ import annotations

import db


async def compute_credits(
    db_path: str,
    model: str,
    tokens: int,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> tuple[float, float, str, float | None, float | None]:
    """Вернуть (credits, rate_base_applied, matched_model, rate_input, rate_output).

    rate_input/rate_output — None, если раздельная тарификация не применялась.
    """
    model = (model or "").strip() or db.DEFAULT_RATE_KEY

    rate = await db.get_rate(db_path, model)
    if rate is None:
        # Нет курсов вовсе (маловероятно после init_db) — 1:1 за 1k.
        return float(tokens), 1.0, db.DEFAULT_RATE_KEY, None, None

    base = rate["base"]
    matched = rate["matched_model"]
    has_breakdown = (
        prompt_tokens is not None
        and completion_tokens is not None
        and (rate["input"] is not None or rate["output"] is not None)
    )

    if has_breakdown:
        rate_in = rate["input"] if rate["input"] is not None else base
        rate_out = rate["output"] if rate["output"] is not None else base
        credits = round(
            (prompt_tokens / 1000.0) * rate_in
            + (completion_tokens / 1000.0) * rate_out,
            2,
        )
        return credits, base, matched, rate_in, rate_out

    # Единый курс.
    credits = round((tokens / 1000.0) * base, 2)
    return credits, base, matched, None, None


async def compute_cost_usd(
    db_path: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    *,
    provider_cost: float | None = None,
) -> tuple[float | None, str | None]:
    """Вернуть (cost_usd, cost_source) — реальную стоимость запроса в долларах.

    Приоритет источников:
      1. provider_cost > 0 (провайдер отдал $ в ответе) -> source="provider"
      2. тарифы $/млн из курса модели + breakdown токенов -> source="computed"
      3. иначе -> (None, None) — стоимость неизвестна

    cost_source нужен интерфейсу, чтобы отличать «реально списано» (provider)
    от «посчитано по тарифам» (computed) — это вопрос достоверности цифры.
    """
    # 1. Стоимость от провайдера — самый надёжный источник (то что реально списано).
    if provider_cost is not None and provider_cost > 0:
        return round(float(provider_cost), 6), "provider"

    # 2. Считаем по тарифам $/млн токенов (z.ai/GLM и прочие без поля cost).
    if prompt_tokens is None or completion_tokens is None:
        return None, None

    model = (model or "").strip() or db.DEFAULT_RATE_KEY
    rate = await db.get_rate(db_path, model)
    if rate is None:
        return None, None

    cost_in = rate["cost_in"]
    cost_out = rate["cost_out"]
    if cost_in is None or cost_out is None:
        # Тарифы $/млн не заданы для модели — стоимость посчитать нельзя.
        return None, None

    cost_usd = round(
        (prompt_tokens / 1_000_000.0) * cost_in
        + (completion_tokens / 1_000_000.0) * cost_out,
        6,
    )
    return cost_usd, "computed"
