"""Storage adapters only: no network, filtering decisions, calculations or AI."""
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from .models import ContractStat, Tender, TenderHistory


TENDER_FIELDS = (
    "id", "source", "law", "purchase_number", "title", "object_info", "okpd2", "max_price",
    "customer_inn", "currency_code", "region", "stage", "purchase_type", "eis_url", "published_at",
    "collecting_finished_at", "first_seen_at", "last_pulled_at", "status", "review", "archived_at",
    "archived_from_stage", "opened_at", "outcome_status", "bid_number", "bid_price", "contract_price",
    "contract_reduction_percent", "contract_reg_num", "contract_winner_inn", "outcome_source",
    "outcome_checked_at", "market_forecast_percent", "market_forecast_sample_count", "market_forecast_at",
    "protocol__participants", "protocol__applications_total",
)
RENAMED = {
    "eis_url": "source_url", "last_pulled_at": "last_seen_at", "contract_price": "final_price",
    "contract_reduction_percent": "discount_pct", "contract_winner_inn": "winner_inn",
    "market_forecast_percent": "forecast_percent", "market_forecast_sample_count": "forecast_sample_count",
    "market_forecast_at": "forecast_at",
}


def sync_tenders(ids, *, using="default", detach=False):
    """Retain identity and summary; never copy raw packages or document previews."""
    for row in Tender.objects.using(using).filter(pk__in=ids).values(*TENDER_FIELDS).iterator(chunk_size=500):
        values = {RENAMED.get(key, key): value for key, value in row.items()
                  if key not in {"id", "title", "object_info", "opened_at", "protocol__participants", "protocol__applications_total"}}
        values.update(title=row["title"] or row["object_info"], own_funnel=True, updated_at=timezone.now())
        identity = {key: values[key] for key in ("source", "law", "purchase_number")}
        histories = TenderHistory.objects.using(using)
        with transaction.atomic(using=using):
            history = histories.select_for_update().filter(tender_id=row["id"]).first()
            if history is None:
                history, _ = histories.select_for_update().get_or_create(**identity)
            from .protocols import find_ours
            participants = row["protocol__participants"]
            participants = [{**p, "id": str(p["id"] or "")} for p in participants if isinstance(p, dict) and "id" in p] if isinstance(participants, list) else []
            ours = find_ours({"participants": participants},
                             bid_number=row["bid_number"], bid_price=None)
            rank = ours.get("rank") if ours else None
            if isinstance(rank, str) and rank.strip().isdigit():
                rank = int(rank.strip())
            if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0:
                values["our_place"] = rank
            total = row["protocol__applications_total"]
            if isinstance(total, str) and total.strip().isdigit():
                total = int(total.strip())
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                values["participants_count"] = total
            for key, value in values.items():
                # An unavailable fact is not a retraction of a preserved result.
                if value is None and key != "archived_at":
                    continue
                if value in ("", []) and key in {"title", "okpd2", "currency_code", "customer_inn", "source_url", "purchase_type", "bid_number", "contract_reg_num", "winner_inn", "outcome_source"}:
                    continue
                setattr(history, key, value)
            history.was_opened = history.was_opened or row["opened_at"] is not None
            history.tender_id = None if detach else row["id"]
            history.save(using=using)


def sync_contract_stat(stat, *, using="default"):
    """Keep the old forecast reader as a linked compatibility projection."""
    if not stat.own_funnel:
        return
    history = None
    if stat.history_id:
        history = TenderHistory.objects.using(using).filter(pk=stat.history_id).first()
    if history is None:
        tender_id = Tender.objects.using(using).filter(law=stat.law, purchase_number=stat.purchase_number).values_list("pk", flat=True).first()
        if tender_id is not None:
            sync_tenders([tender_id], using=using)
            history = TenderHistory.objects.using(using).get(tender_id=tender_id)
        else:
            history, _ = TenderHistory.objects.using(using).get_or_create(
                source=TenderHistory.UNKNOWN, law=stat.law, purchase_number=stat.purchase_number,
                defaults={"review": ""},
            )
    mapping = {"subject": "title", "nmck": "max_price", "final_price": "final_price",
               "discount_pct": "discount_pct", "okpd2": "okpd2", "region": "region",
               "customer_inn": "customer_inn", "winner_inn": "winner_inn",
               "contract_reg_num": "contract_reg_num", "contract_date": "contract_date",
               "participants_count": "participants_count", "category": "category"}
    for field, destination in mapping.items():
        value = getattr(stat, field)
        # Working facts take priority; the linked compatibility projection retains other contract rows.
        if value not in (None, "", []) and (not history.tender_id or getattr(history, destination) in (None, "", [])):
            setattr(history, destination, value)
    history.forecast_included = stat.forecast_included
    history.shared_purchase = stat.shared_purchase
    history.own_funnel = True
    legacy = history.legacy_data
    snapshot = {field.name: getattr(stat, field.name) for field in stat._meta.fields if field.name != "history"}
    snapshot = json.loads(json.dumps(snapshot, cls=DjangoJSONEncoder))
    legacy["contract_stats"] = [row for row in legacy.get("contract_stats", []) if row.get("id") != stat.pk] + [snapshot]
    history.legacy_data = legacy
    history.shared_purchase = any(row.get("shared_purchase") for row in legacy["contract_stats"]) or len({row.get("contract_reg_num") for row in legacy["contract_stats"]}) > 1
    if not history.tender_id and stat.is_ours:
        history.outcome_status = Tender.OUTCOME_WON
    history.updated_at = timezone.now()
    history.save(using=using)
    ContractStat.objects.using(using).filter(pk=stat.pk).update(history_id=history.pk)
    stat.history_id = history.pk


def sync_feedback(feedback, *, using="default"):
    sync_tenders([feedback.tender_id], using=using)
    history = TenderHistory.objects.using(using).get(tender_id=feedback.tender_id)
    if history.decision_at is not None and history.decision_at > feedback.created_at:
        return
    history.reason_code = feedback.reason
    history.decision_source = "manual"
    history.decision_at = feedback.created_at
    legacy = history.legacy_data
    snapshot = {"id": feedback.pk, "tender_id": feedback.tender_id, "reason": feedback.reason,
                "created_at": feedback.created_at.isoformat()}
    legacy["dismissal_feedback"] = [row for row in legacy.get("dismissal_feedback", []) if row.get("id") != feedback.pk] + [snapshot]
    history.legacy_data = legacy
    history.updated_at = timezone.now()
    history.save(using=using, update_fields=["reason_code", "decision_source", "decision_at", "legacy_data", "updated_at"])
