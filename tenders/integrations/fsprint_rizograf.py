"""Ризография (fsprint.ru/rizograf.html) — статичный прайс-лист, не API.

В отличие от tenders.integrations.fsprint (calc.fsprint.ru — живой
калькулятор с формой), здесь на странице обычная таблица цен без формы
и без запросов на сервер. Снято 29.09.2026, цены — только то, что
опубликовано на странице (ссылка ниже), ничего не досчитано и не
угадано. price_source_type для этой связи — SOURCE_INTERNAL_CALCULATOR
(StageCounterpartyLink): считаем сами по этим данным, без похода в сеть.

Тираж — это ОБЩЕЕ количество копий одного листа (чем больше, тем дешевле
цена за копию); строка тарифа — минимальный тираж, с которого она
действует. Цены на странице — для формата А4, «А3 стоит в 2 раза дороже
А4» (дословно) — множитель применён к цене за копию целиком (печать +
бумага), других указаний на странице нет.

Отдельно на странице есть спецпредложение «рекламные листовки А7 1+1
чёрно-белые» с собственной шкалой цен (от 1000 до 10 000 000 экз.) — это
конкретный товар, не общий тариф ризографии, сюда сознательно не
включено, чтобы не смешивать с общей формулой."""

SOURCE_URL = "https://www.fsprint.ru/rizograf.html"
CAPTURED = "2026-09-29"

# Тираж (копий, минимум для строки) → цена одной копии А4 без бумаги, ₽.
RUN_TIERS = {
    10: 3.15,
    20: 1.66,
    30: 1.21,
    40: 1.08,
    50: 0.81,
    100: 0.54,
    200: 0.40,
    300: 0.36,
    400: 0.27,
    500: 0.26,
    1000: 0.25,
    2000: 0.25,
    5000: 0.19,
}

# Бумага → цена листа А4, ₽.
PAPER_PRICES = {
    "thin_65": 0.145,
    "standard_ru_80": 0.35,
    "finnish_80": 0.261,
    "color_ru_80": 0.4,
    "color_import_80": 0.62,
    "finnish_white_120": 0.798,
    "dense_160": 1.06,
    "self_adhesive_white_80": 3.6,
    "self_adhesive_color_80": 4.5,
}


class RizografError(Exception):
    """Неизвестная бумага или формат — не угадываем, сообщаем явно."""


def calculate_price(quantity: int, paper_key: str, *, format: str = "A4") -> dict:
    if paper_key not in PAPER_PRICES:
        raise RizografError(f"Неизвестный тип бумаги: {paper_key!r}. Известные: {sorted(PAPER_PRICES)}")
    if format not in ("A4", "A3"):
        raise RizografError(f"Неизвестный формат: {format!r}. Известны только A4/A3.")
    price_per_copy = _tier_price(quantity) + PAPER_PRICES[paper_key]
    if format == "A3":
        price_per_copy *= 2
    return {"price_per_copy": round(price_per_copy, 4), "total": round(price_per_copy * quantity, 2)}


def _tier_price(quantity: int) -> float:
    """Цена по строке тарифа, применимой к тиражу: наибольший порог,
    не превышающий тираж; для тиража ниже минимального порога (10) —
    цена этого минимального порога, ниже него на странице цен нет."""
    thresholds = sorted(RUN_TIERS)
    tier = thresholds[0]
    for threshold in thresholds:
        if quantity >= threshold:
            tier = threshold
    return RUN_TIERS[tier]
