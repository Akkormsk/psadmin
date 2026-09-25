"""Прозрачные правила цвета оценки риска."""
from __future__ import annotations


_RANK = {"low": 0, "medium": 1, "high": 2}


def classify_risk(facts: dict | None, *, warning_days: int = 14, critical_days: int = 7) -> dict:
    """Возвращает итог и факторы, не принимая решений вместо извлечённых фактов."""
    facts = facts or {}
    if not facts.get("documents_sufficient"):
        return {"risk_level": "unknown", "risk_factors": []}
    factors = []

    def add(level, code, text):
        factors.append({"level": level, "code": code, "text": text})

    days = facts.get("execution_days")
    if isinstance(days, int) and days >= 0:
        if days <= critical_days:
            add("high", "short_deadline", f"Срок исполнения {days} дн. — критически короткий.")
        elif days <= warning_days:
            add("medium", "short_deadline", f"Срок исполнения {days} дн. — короткий.")

    national_regime = facts.get("national_regime")
    if national_regime == "blocked":
        add("high", "national_regime", "Нацрежим требует недоступного обязательного подтверждения.")
    elif national_regime == "confirmation_required":
        add("medium", "national_regime", "Нацрежим требует подтвердить происхождение товара.")

    samples = facts.get("samples")
    if samples == "impossible_deadline":
        add("high", "samples", "Образцы нужны в нереальный срок.")
    elif samples == "required":
        add("medium", "samples", "Требуются образцы или испытания.")

    delivery_mode = facts.get("delivery_mode")
    if delivery_mode == "requests_open_ended":
        add("high", "delivery_by_requests", "Поставка по заявкам без ясного общего срока или объёма.")
    elif delivery_mode == "requests_with_end":
        add("medium", "delivery_by_requests", "Поставка по заявкам заказчика.")

    level = max((factor["level"] for factor in factors), key=lambda value: _RANK[value], default="low")
    return {"risk_level": level, "risk_factors": factors}
