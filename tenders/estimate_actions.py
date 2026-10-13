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

@transaction.atomic
def set_tender_estimate_status(user, estimate_id, status):
    from tender_selection.models import Tender
    from tender_selection.services import archive_tender

    estimate = estimate_for_user(user, estimate_id, True)
    if not estimate.tender_id or status not in dict(Tender.OUTCOME_STATUS_CHOICES):
        raise EstimateActionError("Неверный статус или тендер недоступен")
    if status == Tender.OUTCOME_PENDING and (estimate.summary_snapshot or {}).get("is_incomplete", True):
        raise EstimateActionError("Завершите расчёт перед переходом в торги")
    tender = estimate.tender
    tender.outcome_status = status
    tender.save(update_fields=["outcome_status"])
    if status == Tender.OUTCOME_NOT_PARTICIPATED:
        archive_tender(tender)
    return {"status": status, "label": tender.get_outcome_status_display(), "tender_id": tender.pk,
            "requires_result": status in {Tender.OUTCOME_LOST, Tender.OUTCOME_WON}}


@transaction.atomic
def set_order_estimate_status(user, estimate_id, status):
    estimate = estimate_for_user(user, estimate_id, False)
    if status not in dict(Order.STATUS_CHOICES):
        raise EstimateActionError("Недопустимый статус заказа")
    if not estimate.order_id:
        estimate.order = Order.objects.create(name=estimate.name)
        estimate.save(update_fields=["order"])
    order = estimate.order
    order.status = status
    order.save(update_fields=["status", "updated_at"])
    return {"status": status, "label": order.get_status_display(), "order_id": order.pk}

import json
from decimal import Decimal, InvalidOperation
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from .models import TenderSettings
from .services import calculate_tender

def _number(value, default="0"):
    return Decimal(str(value or default).replace(",", "."))

@transaction.atomic
def persist_estimate(user, data, estimate, settings, *, pipeline=False):
    """Parse the posted tender state and write it to ``estimate`` (creating
    one when it is ``None``). Returns ``(estimate, error, meta)`` — ``error``
    is a message string when nothing was saved, ``meta`` carries
    ``incomplete`` and the parsed ``posted_lines`` / ``posted_analysis``
    (kept even on a validation error so the form can re-render them).
    Shared by the plain form POST and the autosave endpoint."""
    meta = {"incomplete": False, "posted_lines": None, "posted_analysis": None}
    try:
        raw_lines = json.loads(data.get("lines_json", "[]"))
        raw_analysis = json.loads(data.get("document_analysis_json", "{}"))
    except json.JSONDecodeError:
        return estimate, "Проверьте товарные позиции.", meta
    meta["posted_lines"] = raw_lines if isinstance(raw_lines, list) else []
    meta["posted_analysis"] = raw_analysis if isinstance(raw_analysis, dict) else {}
    try:
        tender_number = data.get("tender_number", "").strip()
        name = data.get("name", "").strip()
        reduction_percent = _number(data.get("reduction_percent"), str(settings.default_reduction_percent))
        russia_delivery = _number(data.get("russia_delivery"))
        if not tender_number or not name or not Decimal("0") <= reduction_percent <= Decimal("100") or russia_delivery < 0:
            raise ValueError
        lines = []
        calculation_complete = True
        for row in meta["posted_lines"]:
            line = {
                "name": str(row.get("name", "")).strip(),
                "quantity": _number(row.get("quantity")),
                "nmck_unit": _number(row.get("nmck_unit")),
                "material_unit": _number(row.get("material_unit")),
                "application_unit": _number(row.get("application_unit")),
                "logistics_unit": _number(row.get("logistics_unit")),
                "product_url": str(row.get("product_url", "")).strip(),
                "comment": str(row.get("comment", "")).strip(),
                "requirements": row.get("requirements") if isinstance(row.get("requirements"), dict) else {},
            }
            has_expense = any(line[key] > 0 for key in ("material_unit", "application_unit", "logistics_unit"))
            if min(line["quantity"], line["nmck_unit"], line["material_unit"], line["application_unit"], line["logistics_unit"]) < 0:
                raise ValueError
            if not line["name"] or line["quantity"] <= 0 or line["nmck_unit"] <= 0 or not has_expense:
                calculation_complete = False
            lines.append(line)
        if not lines:
            lines.append({"name": "", "quantity": Decimal("0"), "nmck_unit": Decimal("0"), "material_unit": Decimal("0"), "application_unit": Decimal("0"), "logistics_unit": Decimal("0"), "product_url": "", "comment": "", "requirements": {}})
            calculation_complete = False
    except (ValueError, TypeError, InvalidOperation):
        return estimate, "Проверьте реквизиты тендера и товарные позиции.", meta

    calculated, summary = calculate_tender(lines, reduction_percent, russia_delivery, settings.vat_rate)
    is_new = estimate is None
    estimate = estimate or (TenderEstimate(owner=user) if pipeline else OrderEstimate(owner=user))
    if user.is_superuser and data.get("owner_id"):
        estimate.owner = get_object_or_404(get_user_model(), pk=data["owner_id"])
    estimate.tender_number = tender_number[:100]
    estimate.name = name[:300]
    estimate.reduction_percent = reduction_percent
    estimate.russia_delivery = russia_delivery
    estimate.result_notes = data.get("result_notes", "").strip()[:5000]
    estimate.vat_rate_snapshot = settings.vat_rate
    estimate.summary_snapshot = {key: str(value) for key, value in summary.items()}
    estimate.summary_snapshot["is_incomplete"] = not calculation_complete
    estimate.document_analysis = meta["posted_analysis"] or {}
    if not pipeline and not estimate.order_id:
        estimate.order = Order.objects.create(name=estimate.name)
    elif not pipeline:
        estimate.order.name = estimate.name
        estimate.order.save(update_fields=["name", "updated_at"])
    estimate.save()
    estimate.lines.all().delete()
    line_model = TenderLine if pipeline else OrderLine
    line_model.objects.bulk_create([line_model(estimate=estimate, sort_order=index, **line) for index, line in enumerate(lines)])
    meta["incomplete"] = not calculation_complete
    return estimate, None, meta
