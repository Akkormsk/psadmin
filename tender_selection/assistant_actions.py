"""Adapters for existing tender-selection services; no selection logic lives here."""
from decimal import Decimal, InvalidOperation

from .models import ContractStat, FilterSettings, Tender, TenderDismissalFeedback
from .services import apply_tender_outcome, archive_tender, clear_tender_document_previews, push_to_estimate, run_pull, set_our_bid, start_risk_assessment_in_background


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


def dismiss(_user, payload):
    tender = _tender(payload)
    reason = payload.get("reason")
    if tender.review == Tender.UNREVIEWED and reason in dict(TenderDismissalFeedback.REASON_CHOICES):
        TenderDismissalFeedback.objects.create(tender=tender, reason=reason)
    clear_tender_document_previews(tender)
    archive_tender(tender)
    return {"tender_id": tender.pk, "status": tender.status}


def restore(_user, payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id"), status=Tender.DISMISSED).first()
    if not tender:
        raise ValueError("Скрытый тендер не найден")
    tender.status = Tender.PUSHED if tender.estimates.exists() else Tender.NEW
    tender.archived_at = None
    tender.archived_from_stage = ""
    tender.save(update_fields=["status", "archived_at", "archived_from_stage"])
    return {"tender_id": tender.pk, "status": tender.status}


def set_review(_user, payload):
    tender = _tender(payload)
    value = payload.get("review")
    if value not in dict(Tender.REVIEW_CHOICES):
        raise ValueError("Недопустимый статус проверки")
    was_unreviewed = tender.review == Tender.UNREVIEWED
    tender.review = value
    tender.save(update_fields=["review"])
    if was_unreviewed and value != Tender.UNREVIEWED and not tender.risk_checked_at:
        start_risk_assessment_in_background(tender.pk)
    return {"tender_id": tender.pk, "review": tender.review}


def save_bid(_user, payload):
    from tenders.models import TenderEstimate

    estimate = TenderEstimate.objects.filter(pk=payload.get("estimate_id")).first()
    if not estimate:
        raise ValueError("Просчёт тендера не найден")
    raw_price = payload.get("bid_price")
    try:
        bid_price = Decimal(str(raw_price)) if raw_price not in (None, "") else None
        if bid_price is not None and bid_price <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError("Сумма заявки должна быть положительным числом")
    set_our_bid(estimate, bid_number=str(payload.get("bid_number", "")), bid_price=bid_price)
    return {"estimate_id": estimate.pk, "tender_id": estimate.tender_id, "bid_number": estimate.tender.bid_number, "bid_price": str(estimate.tender.bid_price) if estimate.tender.bid_price is not None else None}


def set_outcome(_user, payload):
    from tenders.models import TenderEstimate

    estimate = TenderEstimate.objects.filter(pk=payload.get("estimate_id")).first()
    status = payload.get("status")
    if not estimate or status not in (Tender.OUTCOME_WON, Tender.OUTCOME_LOST, Tender.OUTCOME_NOT_PARTICIPATED):
        raise ValueError("Неверный просчёт или итог тендера")
    try:
        reduction = Decimal(str(payload["actual_reduction_percent"])) if payload.get("actual_reduction_percent") not in (None, "") else None
        if reduction is not None and not Decimal("0") <= reduction <= Decimal("100"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError("Фактическое снижение должно быть числом от 0 до 100")
    apply_tender_outcome(estimate, status=status, reduction_percent=reduction, source=Tender.OUTCOME_MANUAL)
    estimate.tender.refresh_from_db()
    return {"estimate_id": estimate.pk, "tender_id": estimate.tender_id, "outcome_status": estimate.tender.outcome_status}


def update_filter_words(_user, payload):
    settings = FilterSettings.load()
    include_words = payload.get("include_words")
    exclude_words = payload.get("exclude_words")
    if not isinstance(include_words, str) or not isinstance(exclude_words, str):
        raise ValueError("Нужны текстовые плюс- и минус-слова")
    settings.include_words = include_words.strip()
    settings.exclude_words = exclude_words.strip()
    settings.save(update_fields=["include_words", "exclude_words"])
    return {"include_words": settings.include_words, "exclude_words": settings.exclude_words}


def word_audit_run(user, _payload):
    from .word_audit import run_audit

    audit = run_audit(user)
    return {"word_audit_id": audit.pk, "spend_rub": str(audit.spend_rub) if audit.spend_rub is not None else None}


def word_audit_apply(_user, payload):
    from .word_audit import SUGGESTION_KINDS, apply_words

    chosen = {kind: payload.get(kind, []) for kind in SUGGESTION_KINDS}
    if not all(isinstance(words, list) and all(isinstance(word, str) for word in words) for words in chosen.values()):
        raise ValueError("Наборы слов должны быть списками")
    apply_words(**chosen)
    return {"applied": {kind: len(words) for kind, words in chosen.items()}}


def toggle_forecast(_user, payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id"), status=Tender.DISMISSED).first()
    if not tender:
        raise ValueError("Скрытый тендер не найден")
    stat = ContractStat.objects.filter(law=tender.law, purchase_number=tender.purchase_number, own_funnel=True).first()
    if stat is None:
        raise ValueError("Результат торгов ещё не попал в статистику прогноза")
    stat.forecast_included = not stat.forecast_included
    stat.save(update_fields=["forecast_included"])
    return {"tender_id": tender.pk, "forecast_included": stat.forecast_included}


def _tender(payload):
    tender = Tender.objects.filter(pk=payload.get("tender_id")).first()
    if not tender:
        raise ValueError("Тендер не найден")
    return tender
