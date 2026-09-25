from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def link_legacy_orders(apps, schema_editor):
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    OrderEstimate = apps.get_model("tenders", "OrderEstimate")
    for order in OrderEstimate.objects.filter(legacy_tender_estimate__isnull=True).iterator():
        legacy = TenderEstimate.objects.filter(
            owner_id=order.owner_id,
            tender_number=order.order_number,
            name=order.name,
        ).filter(Q(tender__isnull=True) | Q(tender__source="manual")).order_by("pk").first()
        if legacy:
            order.legacy_tender_estimate_id = legacy.pk
            order.save(update_fields=["legacy_tender_estimate"])


class Migration(migrations.Migration):

    dependencies = [("tenders", "0036_orderestimate_orderline")]

    operations = [
        migrations.AddField(
            model_name="orderestimate",
            name="legacy_tender_estimate",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="migrated_order_estimate", to="tenders.tenderestimate", verbose_name="Исходный старый расчёт"),
        ),
        migrations.RunPython(link_legacy_orders, migrations.RunPython.noop),
    ]
