"""Read-only adapter around the existing calculator service."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .models import CalculatorSettings, Estimate, PriceItem
from .services import calculate_sheet_estimate


class CalculatorActionError(ValueError):
    pass


def calculate_sheet(_user, payload):
    """Use the same price resolution and calculation function as the calculator UI."""
    try:
        calculator_type = payload.get("calculator_type", Estimate.TYPE_SHEET)
        if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
            raise ValueError
        product_quantity = max(1, int(payload["product_quantity"]))
        work_hours = Decimal(str(payload.get("work_hours", "0")))
        raw_lines = payload["lines"]
        if not isinstance(raw_lines, list) or work_hours < 0 or (work_hours * 2) % 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise CalculatorActionError("Проверьте тип расчёта, тираж, часы и строки.")

    items = PriceItem.objects.in_bulk([row.get("item_id") for row in raw_lines if isinstance(row, dict) and row.get("item_id")])
    lines = []
    for row in raw_lines:
        try:
            item = items.get(int(row["item_id"])) if row.get("item_id") else None
            custom = bool(row.get("custom"))
            quantity = Decimal(str(row["quantity"]))
            price = Decimal(str(row["unit_price"])) if custom else item.effective_unit_price
            name = str(row.get("name", "")).strip() if custom else item.name
            if quantity <= 0 or price < 0 or not name:
                raise ValueError
            lines.append({"category": row["category"], "quantity": quantity, "unit_price": price, "name": name, "item": item, "custom": custom})
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
            raise CalculatorActionError("Одна из строк расчёта заполнена неверно.")
    settings = CalculatorSettings.objects.get_or_create(pk=1)[0]
    summary = calculate_sheet_estimate(lines, product_quantity, work_hours, settings, calculator_type)
    return {"calculator_type": calculator_type, "product_quantity": product_quantity, "summary": {key: str(value) for key, value in summary.items()}}
