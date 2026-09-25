from django.db import migrations


def migrate_and_verify_legacy_rows(apps, schema_editor):
    Tender = apps.get_model("tender_selection", "Tender")
    FoundTender = apps.get_model("tender_selection", "FoundTender")
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    copied_fields = (
        "object_info", "title", "max_price", "currency_code", "customer_inn",
        "region", "stage", "purchase_type", "okpd2", "published_at",
        "collecting_finished_at", "eis_url", "raw", "notification_raw",
        "notification_checked_at", "clarifications_raw", "complaints_raw",
        "extras_checked_at", "risk_assessment", "risk_assessment_docs",
        "risk_checked_at", "risk_error", "status", "review", "opened_at",
        "first_seen_at", "last_pulled_at", "archived_at",
    )
    for found in FoundTender.objects.select_related("tender", "pushed_estimate").iterator():
        tender = found.tender
        if tender is None:
            tender, _ = Tender.objects.get_or_create(
                law=found.law, purchase_number=found.purchase_number,
            )
            found.tender_id = tender.pk
        for field in copied_fields:
            setattr(tender, field, getattr(found, field))
        tender.source = "manual" if (found.raw or {}).get("manual_entry") else "eis"
        tender.save(update_fields=["source", *copied_fields])
        if found.pushed_estimate_id:
            TenderEstimate.objects.filter(pk=found.pushed_estimate_id, tender__isnull=True).update(tender_id=tender.pk)
        found.save(update_fields=["tender"])

    unmigrated = list(FoundTender.objects.filter(tender__isnull=True).values_list("pk", flat=True)[:20])
    if unmigrated:
        raise RuntimeError(
            "Нельзя удалить FoundTender: не перенесены записи "
            + ", ".join(map(str, unmigrated))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("tender_selection", "0025_tender_becomes_lifecycle_entity"),
        ("tenders", "0039_remove_orderestimate_legacy_tender_estimate"),
    ]

    operations = [
        migrations.RunPython(migrate_and_verify_legacy_rows, migrations.RunPython.noop),
    ]
