from django.db import migrations


def backfill_missing_tender_anchors(apps, schema_editor):
    Tender = apps.get_model("tender_selection", "Tender")
    FoundTender = apps.get_model("tender_selection", "FoundTender")

    for found in FoundTender.objects.filter(tender__isnull=True).iterator():
        tender, _ = Tender.objects.get_or_create(
            law=found.law,
            purchase_number=found.purchase_number,
        )
        found.tender_id = tender.id
        found.save(update_fields=["tender"])


class Migration(migrations.Migration):

    dependencies = [
        ("tender_selection", "0022_backfill_manual_estimate_cards"),
    ]

    operations = [
        migrations.RunPython(backfill_missing_tender_anchors, migrations.RunPython.noop),
    ]
