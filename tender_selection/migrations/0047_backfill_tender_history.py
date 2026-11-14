"""Preserve existing evidence before making source identity unique.

Uses historical models only. No network, AI, change of decisions or source-card deletion.
"""
import json
import re

from django.core.serializers.json import DjangoJSONEncoder
from django.db import migrations, models
from django.db.models import Count


TRACE_FIELDS = ("id", "law", "purchase_number", "title", "okpd2", "max_price",
                "plus_hits", "filtered_out", "was_opened", "purged_at")
CARD_FIELDS = (
    "id", "source", "law", "purchase_number", "title", "object_info", "okpd2", "max_price",
    "customer_inn", "currency_code", "region", "stage", "purchase_type", "eis_url", "published_at",
    "collecting_finished_at", "first_seen_at", "last_pulled_at", "status", "review", "archived_at",
    "archived_from_stage", "opened_at", "outcome_status", "bid_number", "bid_price", "contract_price",
    "contract_reduction_percent", "contract_reg_num", "contract_winner_inn", "outcome_source",
    "outcome_checked_at", "market_forecast_percent", "market_forecast_sample_count", "market_forecast_at",
    "protocol__participants", "protocol__applications_total",
)
RENAMED = {"eis_url": "source_url", "last_pulled_at": "last_seen_at", "contract_price": "final_price",
           "contract_reduction_percent": "discount_pct", "contract_winner_inn": "winner_inn",
           "market_forecast_percent": "forecast_percent", "market_forecast_sample_count": "forecast_sample_count",
           "market_forecast_at": "forecast_at"}
STAT_FIELDS = ("id", "law", "purchase_number", "contract_reg_num", "shared_purchase", "category", "okpd2",
               "ktru", "region", "subject", "customer_inn", "nmck", "final_price", "discount_pct",
               "participants_count", "winner_inn", "is_ours", "own_funnel", "forecast_included", "contract_date",
               "nmck_checked", "collected_at")


def packed(row):
    return json.loads(json.dumps(row, cls=DjangoJSONEncoder))


def forward(apps, schema_editor):
    db = schema_editor.connection.alias
    History = apps.get_model("tender_selection", "TenderHistory")
    Tender = apps.get_model("tender_selection", "Tender")
    Stat = apps.get_model("tender_selection", "ContractStat")
    Feedback = apps.get_model("tender_selection", "TenderDismissalFeedback")
    histories = History.objects.using(db)
    cards = Tender.objects.using(db)

    # Old traces have no source. Assign one only when the surviving working card proves it.
    for old in histories.values(*TRACE_FIELDS).iterator(chunk_size=500):
        source = cards.filter(law=old["law"], purchase_number=old["purchase_number"]).values_list("source", flat=True).first()
        histories.filter(pk=old["id"]).update(
            source=source or "unknown", legacy_data={"incoming_traces": [packed(old)]},
        )
    duplicates = list(histories.values("source", "law", "purchase_number").annotate(n=Count("pk")).filter(n__gt=1))
    for identity in duplicates:
        identity.pop("n")
        rows = list(histories.filter(**identity).order_by("purged_at", "pk"))
        kept = rows[0]
        originals = [original for row in rows for original in row.legacy_data["incoming_traces"]]
        latest = rows[-1]
        for key in ("title", "okpd2", "max_price", "filtered_out"):
            setattr(kept, key, getattr(latest, key))
        kept.was_opened = any(row.was_opened for row in rows)
        kept.plus_hits = list(dict.fromkeys(word for row in rows for word in row.plus_hits))
        kept.legacy_data = {"incoming_traces": originals}
        kept.save(using=db)
        # Every merged row is preserved in the canonical row; no working object is removed.
        histories.filter(pk__in=[row.pk for row in rows[1:]]).delete()

    for row in cards.values(*CARD_FIELDS).iterator(chunk_size=500):
        identity = {key: row[key] for key in ("source", "law", "purchase_number")}
        history, _ = histories.get_or_create(**identity)
        values = {RENAMED.get(key, key): value for key, value in row.items()
                  if key not in {"id", "title", "object_info", "opened_at", "protocol__participants", "protocol__applications_total"}}
        values.update(tender_id=row["id"], title=row["title"] or row["object_info"])
        for key, value in values.items():
            if value is None and key != "archived_at":
                continue
            if value in ("", []) and key in {"title", "okpd2", "currency_code", "customer_inn", "source_url", "purchase_type", "bid_number", "contract_reg_num", "winner_inn", "outcome_source"}:
                continue
            setattr(history, key, value)
        history.was_opened = history.was_opened or row["opened_at"] is not None
        participants = row["protocol__participants"]
        bid = re.sub(r"\s+", "", row["bid_number"] or "").upper()
        if bid and isinstance(participants, list):
            ours = next((p for p in participants if isinstance(p, dict) and re.sub(r"\s+", "", str(p.get("id") or "")).upper() == bid), None)
            rank = ours.get("rank") if ours else None
            if isinstance(rank, str) and rank.strip().isdigit():
                rank = int(rank.strip())
            if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0:
                history.our_place = rank
        total = row["protocol__applications_total"]
        if isinstance(total, str) and total.strip().isdigit():
            total = int(total.strip())
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            history.participants_count = total
        history.save(using=db)

    for stat in Stat.objects.using(db).filter(own_funnel=True).values(*STAT_FIELDS).order_by("collected_at", "pk").iterator(chunk_size=500):
        history = histories.filter(tender__law=stat["law"], tender__purchase_number=stat["purchase_number"]).first()
        if history is None:
            history, _ = histories.get_or_create(source="unknown", law=stat["law"], purchase_number=stat["purchase_number"], defaults={"review": ""})
        original = history.legacy_data
        original.setdefault("contract_stats", []).append(packed(stat))
        history.legacy_data = original
        mapping = {"subject": "title", "nmck": "max_price", "final_price": "final_price", "discount_pct": "discount_pct",
                   "okpd2": "okpd2", "region": "region", "customer_inn": "customer_inn", "winner_inn": "winner_inn",
                   "contract_reg_num": "contract_reg_num", "contract_date": "contract_date",
                   "participants_count": "participants_count", "category": "category"}
        for key, destination in mapping.items():
            value = stat[key]
            if value not in (None, "", []) and (not history.tender_id or getattr(history, destination) in (None, "", [])):
                setattr(history, destination, value)
        history.forecast_included = stat["forecast_included"]
        history.shared_purchase = any(row.get("shared_purchase") for row in original["contract_stats"]) or len({row.get("contract_reg_num") for row in original["contract_stats"]}) > 1
        if not history.tender_id and stat["is_ours"]:
            history.outcome_status = "won"
        history.save(using=db)
        Stat.objects.using(db).filter(pk=stat["id"]).update(history_id=history.pk)

    for feedback in Feedback.objects.using(db).order_by("created_at", "pk").values("id", "tender_id", "reason", "created_at").iterator(chunk_size=500):
        history = histories.get(tender_id=feedback["tender_id"])
        original = history.legacy_data
        original.setdefault("dismissal_feedback", []).append(packed(feedback))
        history.legacy_data = original
        history.reason_code = feedback["reason"]
        history.decision_source = "manual"
        history.decision_at = feedback["created_at"]
        history.save(using=db)


def reverse(apps, schema_editor):
    """Restore original trace rows, including their IDs, before dropping added columns."""
    History = apps.get_model("tender_selection", "TenderHistory")
    histories = History.objects.using(schema_editor.connection.alias)
    for history in histories.all().iterator(chunk_size=500):
        for original in history.legacy_data.get("incoming_traces", []):
            pk = original["id"]
            row = histories.filter(pk=pk).first()
            if row is not None and row.pk != history.pk:
                raise RuntimeError("Cannot restore an occupied legacy history ID; restore the verified backup instead")
            values = {key: value for key, value in original.items() if key != "id"}
            if row is None:
                row = histories.create(pk=pk, **{key: value for key, value in values.items() if key != "purged_at"})
            histories.filter(pk=row.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0046_alter_tenderhistory_options_contractstat_history_and_more")]

    operations = [
        migrations.RunPython(forward, reverse),
        migrations.AddConstraint(
            model_name="tenderhistory",
            constraint=models.UniqueConstraint(fields=["source", "law", "purchase_number"], name="uniq_tender_history_identity"),
        ),
    ]
