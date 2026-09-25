from django.db import migrations, models


def copy_status_and_remove_legacy_estimates(apps, schema_editor):
    OrderEstimate = apps.get_model("tenders", "OrderEstimate")
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    Tender = apps.get_model("tender_selection", "Tender")

    legacy_ids = list(OrderEstimate.objects.exclude(legacy_calculation_id=None).values_list("legacy_calculation_id", flat=True))
    estimates = TenderEstimate.objects.filter(pk__in=legacy_ids)
    for estimate in estimates.iterator():
        OrderEstimate.objects.filter(legacy_calculation_id=estimate.pk).update(status=estimate.status)

    manual_tender_ids = list(estimates.filter(tender__source="manual").values_list("tender_id", flat=True))
    estimates.delete()
    Tender.objects.filter(pk__in=manual_tender_ids, source="manual", estimates__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [("tenders", "0039_remove_orderestimate_legacy_tender_estimate")]

    operations = [
        migrations.AddField(
            model_name="orderestimate",
            name="status",
            field=models.CharField(choices=[("draft", "Черновик"), ("pending", "В ожидании"), ("not_participated", "Не участвовали"), ("lost", "Проигран"), ("won", "Выигран")], default="draft", max_length=16, verbose_name="Статус"),
        ),
        migrations.RunPython(copy_status_and_remove_legacy_estimates, migrations.RunPython.noop),
    ]
