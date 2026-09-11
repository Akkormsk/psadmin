from decimal import InvalidOperation

from django import template

register = template.Library()

NBSP = "\N{NO-BREAK SPACE}"  # число не переносится по разрядам


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _grouped(number) -> str:
    return f"{round(number):,}".replace(",", NBSP)


@register.filter
def rub(value):
    """Сумма полностью, разряды через неразрывный пробел: 23 966 770 ₽."""
    number = _to_float(value)
    if number is None:
        return "—"
    return f"{_grouped(number)}{NBSP}\N{RUBLE SIGN}"


@register.filter
def rub_full(value):
    """То же, что rub — для title-подсказки при наведении."""
    number = _to_float(value)
    return f"{_grouped(number)} \N{RUBLE SIGN}" if number is not None else ""
