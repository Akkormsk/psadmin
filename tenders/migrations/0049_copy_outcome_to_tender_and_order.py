from django.db import migrations


def copy_forward(apps, schema_editor):
    TenderEstimate = apps.get_model("tenders", "TenderEstimate")
    OrderEstimate = apps.get_model("tenders", "OrderEstimate")
    Order = apps.get_model("tenders", "Order")

    # Тендерная сторона: исход торгов переезжает с расчёта на сам тендер —
    # расчёт может исчезнуть/пересчитаться, а факт того, что случилось с
    # тендером, должен остаться (и пережить архивацию).
    for estimate in TenderEstimate.objects.exclude(tender__isnull=True).select_related("tender"):
        tender = estimate.tender
        tender.outcome_status = estimate.status
        tender.contract_price = estimate.actual_price
        tender.contract_reduction_percent = estimate.actual_reduction_percent
        tender.outcome_checked_at = estimate.outcome_checked_at
        tender.outcome_source = estimate.outcome_source
        tender.bid_number = estimate.bid_number
        tender.bid_price = estimate.bid_price
        tender.protocol = estimate.protocol
        tender.protocol_checked_at = estimate.protocol_checked_at
        tender.save()

    # Заказная сторона: у каждого OrderEstimate раньше был свой статус —
    # заводим Order на каждый и переносим статус туда.
    for estimate in OrderEstimate.objects.filter(order__isnull=True):
        order = Order.objects.create(name=estimate.name, status=estimate.status)
        estimate.order = order
        estimate.save(update_fields=["order"])


def copy_backward(apps, schema_editor):
    # Поля возвращаются следующей миграцией назад автоматически (RemoveField
    # откатывается в AddField без данных) — переносить данные обратно некуда,
    # это разовый перенос вперёд.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0048_order_and_order_fk'),
        ('tender_selection', '0031_tender_bid_number_tender_bid_price_and_more'),
    ]

    operations = [
        migrations.RunPython(copy_forward, copy_backward),
    ]
