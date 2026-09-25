from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from decimal import Decimal


def copy_manual_tender_estimates_to_orders(apps, schema_editor):
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    TenderLine = apps.get_model("tenders", "TenderLine")
    OrderEstimate = apps.get_model("tenders", "OrderEstimate")
    OrderLine = apps.get_model("tenders", "OrderLine")

    from django.db.models import Q

    for estimate in TenderEstimate.objects.filter(
        Q(tender__isnull=True) | Q(tender__source="manual")
    ).iterator():
        order, created = OrderEstimate.objects.get_or_create(
            owner_id=estimate.owner_id,
            order_number=estimate.tender_number,
            name=estimate.name,
            defaults={
                "reduction_percent": estimate.reduction_percent,
                "russia_delivery": estimate.russia_delivery,
                "vat_rate_snapshot": estimate.vat_rate_snapshot,
                "summary_snapshot": estimate.summary_snapshot,
                "document_analysis": estimate.document_analysis,
                "notes": estimate.result_notes,
            },
        )
        if created:
            OrderEstimate.objects.filter(pk=order.pk).update(
                created_at=estimate.created_at,
                updated_at=estimate.updated_at,
            )
            OrderLine.objects.bulk_create([
                OrderLine(
                    estimate_id=order.pk, name=line.name, quantity=line.quantity,
                    nmck_unit=line.nmck_unit, material_unit=line.material_unit,
                    application_unit=line.application_unit, logistics_unit=line.logistics_unit,
                    product_url=line.product_url, comment=line.comment,
                    requirements=line.requirements, sort_order=line.sort_order,
                )
                for line in TenderLine.objects.filter(estimate_id=estimate.pk)
            ])


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0035_tendersettings_roi_good_percent_and_more"),
        ("tender_selection", "0025_tender_becomes_lifecycle_entity"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderEstimate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_number", models.CharField(max_length=100, verbose_name="Номер расчёта")),
                ("name", models.CharField(max_length=300, verbose_name="Название / комментарий")),
                ("reduction_percent", models.DecimalField(decimal_places=2, default=Decimal("30.00"), max_digits=5, verbose_name="Снижение цены, %")),
                ("russia_delivery", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Доставка по РФ")),
                ("vat_rate_snapshot", models.DecimalField(decimal_places=2, default=Decimal("5.00"), max_digits=5, verbose_name="НДС, %")),
                ("summary_snapshot", models.JSONField(blank=True, default=dict)),
                ("document_analysis", models.JSONField(blank=True, default=dict, verbose_name="Анализ документов")),
                ("notes", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="order_estimates", to=settings.AUTH_USER_MODEL, verbose_name="Ответственный")),
            ],
            options={"verbose_name": "Расчёт заказа", "verbose_name_plural": "Расчёты заказов", "ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="OrderLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=500, verbose_name="Наименование")),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Количество")),
                ("nmck_unit", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Цена за единицу")),
                ("material_unit", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Материал")),
                ("application_unit", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Нанесение")),
                ("logistics_unit", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Логистика")),
                ("product_url", models.URLField(blank=True, max_length=1000, verbose_name="Ссылка")),
                ("comment", models.CharField(blank=True, max_length=500, verbose_name="Комментарий")),
                ("requirements", models.JSONField(blank=True, default=dict, verbose_name="Требования")),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("estimate", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="tenders.orderestimate")),
            ],
            options={"verbose_name": "Позиция расчёта заказа", "verbose_name_plural": "Позиции расчётов заказов", "ordering": ["sort_order", "pk"]},
        ),
        migrations.RunPython(copy_manual_tender_estimates_to_orders, migrations.RunPython.noop),
    ]
