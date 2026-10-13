"""Shared saved-estimate operations for web UI and assistant channels."""
from django.db import transaction

from .models import Order, OrderEstimate, OrderLine, TenderEstimate, TenderLine


class EstimateActionError(ValueError):
    pass


def estimate_for_user(user, estimate_id, pipeline):
    model = TenderEstimate if pipeline else OrderEstimate
    estimate = model.objects.filter(pk=estimate_id).first()
    if estimate is None or (not user.is_superuser and estimate.owner_id != user.pk):
        raise EstimateActionError("Расчёт не найден или недоступен")
    return estimate


@transaction.atomic
def duplicate_estimate(user, estimate_id, pipeline):
    original = estimate_for_user(user, estimate_id, pipeline)
    model = TenderEstimate if pipeline else OrderEstimate
    line_model = TenderLine if pipeline else OrderLine
    name = f"{original.name} (копия)"[:300]
    fields = {
        "owner": user, "name": name,
        "reduction_percent": original.reduction_percent,
        "russia_delivery": original.russia_delivery,
        "vat_rate_snapshot": original.vat_rate_snapshot,
        "summary_snapshot": original.summary_snapshot,
        "document_analysis": original.document_analysis,
    }
    if pipeline:
        fields.update(tender=original.tender, tender_number=original.tender_number, result_notes="")
    else:
        fields.update(order=Order.objects.create(name=name), order_number=original.order_number, notes="")
    new_estimate = model.objects.create(**fields)
    line_model.objects.bulk_create([
        line_model(
            estimate=new_estimate, sort_order=line.sort_order, name=line.name,
            quantity=line.quantity, nmck_unit=line.nmck_unit,
            material_unit=line.material_unit, application_unit=line.application_unit,
            logistics_unit=line.logistics_unit, product_url=line.product_url,
            comment=line.comment, requirements=line.requirements,
        )
        for line in original.lines.all()
    ])
    return {"estimate_id": new_estimate.pk, "pipeline": bool(pipeline)}


@transaction.atomic
def delete_estimate(user, estimate_id, pipeline):
    estimate = estimate_for_user(user, estimate_id, pipeline)
    estimate.delete()
    return {"deleted": True, "estimate_id": estimate_id, "pipeline": bool(pipeline)}
