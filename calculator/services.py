from decimal import Decimal

from .models import PriceItem


ZERO = Decimal("0")


def calculate_sheet_estimate(lines, product_quantity, work_hours, settings, calculator_type="sheet"):
    material_cost = sum((line["quantity"] * line["unit_price"] for line in lines), ZERO)
    paper_category = PriceItem.CATEGORY_WIDE_PAPER if calculator_type == "wide" else PriceItem.CATEGORY_PAPER
    paper_sheets = sum((line["quantity"] for line in lines if line["category"] == paper_category), ZERO)
    labour_cost = work_hours * settings.hourly_rate
    cost_price = material_cost + labour_cost
    small_run_coefficient = Decimal("1") + (Decimal("1") / paper_sheets if paper_sheets else ZERO)
    if calculator_type == "wide":
        standard = material_cost * Decimal("2") * small_run_coefficient + labour_cost * Decimal("2")
    else:
        standard = (material_cost * settings.material_coefficient + labour_cost * settings.time_coefficient) * small_run_coefficient
    regular = standard * (Decimal("1") - settings.regular_discount / Decimal("100"))
    partner = standard * (Decimal("1") - settings.partner_discount / Decimal("100"))
    urgent = standard * settings.urgency_multiplier
    divisor = Decimal(product_quantity or 1)
    return {
        "material_cost": material_cost,
        "labour_cost": labour_cost,
        "cost_price": cost_price,
        "small_run_coefficient": small_run_coefficient,
        "standard": standard,
        "regular": regular,
        "partner": partner,
        "urgent": urgent,
        "margin": standard - cost_price,
        "unit_standard": standard / divisor,
        "unit_regular": regular / divisor,
        "unit_partner": partner / divisor,
        "unit_urgent": urgent / divisor,
    }
