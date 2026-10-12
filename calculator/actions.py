"""Business actions shared by calculator views and assistant adapters."""
from __future__ import annotations

from .models import Estimate, EstimateLine


class CalculatorActionError(Exception):
    pass


def estimate_for_user(actor, estimate_id):
    estimate = Estimate.objects.filter(pk=estimate_id).first()
    if estimate is None or (not actor.is_superuser and estimate.owner_id != actor.pk):
        raise CalculatorActionError("Расчёт не найден или недоступен")
    return estimate


def duplicate_estimate(actor, estimate_id):
    original = estimate_for_user(actor, estimate_id)
    lines = list(original.lines.all())
    copy = Estimate.objects.create(
        owner=actor,
        calculator_type=original.calculator_type,
        name=f"{original.name} (копия)"[:200],
        comment=original.comment,
        product_quantity=original.product_quantity,
        work_hours=original.work_hours,
        settings_snapshot=original.settings_snapshot,
        summary_snapshot=original.summary_snapshot,
    )
    EstimateLine.objects.bulk_create([
        EstimateLine(
            estimate=copy, category=line.category, price_item=line.price_item,
            name_snapshot=line.name_snapshot, unit_name=line.unit_name,
            unit_price_snapshot=line.unit_price_snapshot, quantity=line.quantity,
            is_custom=line.is_custom,
        ) for line in lines
    ])
    return {"estimate_id": copy.pk, "source_estimate_id": original.pk, "name": copy.name}


def delete_estimate(actor, estimate_id):
    estimate = estimate_for_user(actor, estimate_id)
    estimate.delete()
    return {"estimate_id": estimate_id, "deleted": True}
