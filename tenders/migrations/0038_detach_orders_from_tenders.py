from django.db import migrations, models
import django.db.models.deletion


def detach_orders_from_tenders(apps, schema_editor):
    OrderEstimate = apps.get_model("tenders", "OrderEstimate")
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    Tender = apps.get_model("tender_selection", "Tender")

    linked_orders = list(
        OrderEstimate.objects.exclude(legacy_tender_estimate_id=None).values_list(
            "pk", "legacy_tender_estimate_id"
        )
    )
    for order_id, legacy_id in linked_orders:
        OrderEstimate.objects.filter(pk=order_id).update(legacy_calculation_id=legacy_id)

    order_legacy_ids = set(
        OrderEstimate.objects.exclude(legacy_calculation_id=None).values_list(
            "legacy_calculation_id", flat=True)
    )
    old_estimates = TenderEstimate.objects.filter(pk__in=order_legacy_ids)
    invalid_ids = list(
        old_estimates.exclude(tender__isnull=True).exclude(tender__source="manual").values_list("pk", flat=True)
    )
    if invalid_ids:
        raise RuntimeError(
            "Найдены расчёты заказов, привязанные к тендерам пайплайна: "
            + ", ".join(map(str, invalid_ids))
        )

    unmigrated_ids = list(
        TenderEstimate.objects.filter(tender__isnull=True).exclude(pk__in=order_legacy_ids).values_list("pk", flat=True)
    )
    if unmigrated_ids:
        raise RuntimeError(
            "Не все самостоятельные расчёты перенесены в OrderEstimate: "
            + ", ".join(map(str, unmigrated_ids))
        )

class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0037_orderestimate_legacy_tender_estimate"),
        ("tender_selection", "0025_tender_becomes_lifecycle_entity"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderestimate",
            name="legacy_calculation_id",
            field=models.PositiveBigIntegerField(blank=True, null=True, unique=True, verbose_name="Старый ID расчёта"),
        ),
        migrations.RunPython(detach_orders_from_tenders, migrations.RunPython.noop),
    ]
