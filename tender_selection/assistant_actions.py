"""Thin assistant adapters for existing tender-selection services."""
from .models import Tender
from .services import archive_tender, push_to_estimate, run_pull, start_risk_assessment_in_background


def pull(_user, payload):
    run = run_pull(max_requests=int(payload.get("max_requests", 16)))
    return {"run_id": run.pk, "ok": run.ok, "created": run.created_count, "updated": run.updated_count, "error": run.error}


def push_to_calculation(user, payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id")).first()
    if not tender:
        raise ValueError("Тендер не найден")
    return {"estimate_id": push_to_estimate(tender, user), "tender_id": tender.pk}


def start_risk(_user, payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id")).first()
    if not tender:
        raise ValueError("Тендер не найден")
    start_risk_assessment_in_background(tender.pk)
    return {"tender_id": tender.pk, "status": "started"}


def archive(_user, payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id")).first()
    if not tender:
        raise ValueError("Тендер не найден")
    archive_tender(tender)
    return {"tender_id": tender.pk, "status": tender.status}
