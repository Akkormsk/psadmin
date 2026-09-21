from django.db import migrations


def backfill(apps, schema_editor):
    Tender = apps.get_model('tender_selection', 'Tender')
    FoundTender = apps.get_model('tender_selection', 'FoundTender')
    TenderEstimate = apps.get_model('tenders', 'TenderEstimate')

    for found in FoundTender.objects.all().iterator():
        tender, _ = Tender.objects.get_or_create(law=found.law, purchase_number=found.purchase_number)
        found.tender = tender

        legacy_id = found._legacy_pushed_estimate_id
        if legacy_id:
            estimate = TenderEstimate.objects.filter(pk=legacy_id).first()
            if estimate is not None:
                found.pushed_estimate = estimate
                if estimate.tender_id != tender.id:
                    estimate.tender = tender
                    estimate.save(update_fields=['tender'])
        found.save(update_fields=['tender', 'pushed_estimate'])


class Migration(migrations.Migration):

    dependencies = [
        ('tender_selection', '0018_tender_and_pushed_estimate_link'),
        ('tenders', '0033_tenderestimate_tender'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
