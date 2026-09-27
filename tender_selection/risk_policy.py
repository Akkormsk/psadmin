"""Прозрачные правила цвета оценки риска."""
from __future__ import annotations


_RANK = {"low": 0, "medium": 1, "high": 2}
DEFAULT_FACTOR_LEVELS = {
    "samples_required": "medium",
    "samples_impossible": "high",
    "national_confirmation": "medium",
    "national_blocked": "high",
    "delivery_requests": "medium",
    "delivery_open_ended": "high",
}


def classify_risk(facts: dict | None, *, warning_days: int = 14, critical_days: int = 7, levels: dict | None = None) -> dict:
    """Возвращает итог и факторы, не принимая решений вместо извлечённых фактов."""
    facts = facts or {}
    if not facts.get("documents_sufficient"):
        return {"risk_level": "unknown", "risk_factors": []}
    factors = []
    level_of = {**DEFAULT_FACTOR_LEVELS, **(levels or {})}

    def add(level, code, text):
        factors.append({"level": level, "code": code, "text": text})

    def check_days(value, code, label):
        if isinstance(value, int) and value >= 0:
            if value <= critical_days:
                add("high", code, f"{label} {value} дн. — критически короткий.")
            elif value <= warning_days:
                add("medium", code, f"{label} {value} дн. — короткий.")

    # Поставка по заявкам: контракт длится месяцы, а жмёт срок каждой партии — это два разных срока.
    check_days(facts.get("execution_days"), "short_deadline", "Срок исполнения всего контракта")
    check_days(facts.get("batch_days"), "short_batch", "Срок поставки по одной заявке")

    national_regime = facts.get("national_regime")
    if national_regime == "blocked":
        add(level_of["national_blocked"], "national_regime", "Нацрежим требует недоступного обязательного подтверждения.")
    elif national_regime == "confirmation_required":
        add(level_of["national_confirmation"], "national_regime", "Нацрежим требует подтвердить происхождение товара.")

    samples = facts.get("samples")
    if samples == "impossible_deadline":
        add(level_of["samples_impossible"], "samples", "Образцы нужны в нереальный срок.")
    elif samples == "required":
        add(level_of["samples_required"], "samples", "Требуются образцы или испытания.")

    delivery_mode = facts.get("delivery_mode")
    if delivery_mode == "requests_open_ended":
        add(level_of["delivery_open_ended"], "delivery_by_requests", "Поставка по заявкам без ясного общего срока или объёма.")
    elif delivery_mode == "requests_with_end":
        add(level_of["delivery_requests"], "delivery_by_requests", "Поставка по заявкам заказчика.")

    level = max((factor["level"] for factor in factors), key=lambda value: _RANK[value], default="low")
    return {"risk_level": level, "risk_factors": factors}
