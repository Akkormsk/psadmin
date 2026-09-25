from django.db import migrations, models


def copy_found_tenders_to_tenders(apps, schema_editor):
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
                law=found.law,
                purchase_number=found.purchase_number,
            )
            found.tender_id = tender.pk
            found.save(update_fields=["tender"])
        for field in copied_fields:
            setattr(tender, field, getattr(found, field))
        tender.source = "manual" if (found.raw or {}).get("manual_entry") else "eis"
        tender.save(update_fields=["source", *copied_fields])
        if found.pushed_estimate_id:
            TenderEstimate.objects.filter(pk=found.pushed_estimate_id, tender__isnull=True).update(tender_id=tender.pk)

    # Standalone historical calculations are intentionally left without a
    # Tender. The next migration moves them to OrderEstimate instead of
    # fabricating a manual pipeline card.


class Migration(migrations.Migration):

    dependencies = [
        ("tender_selection", "0024_filtersettings_risk_deadlines"),
    ]

    operations = [
        migrations.AddField(model_name="tender", name="archived_at", field=models.DateTimeField(blank=True, null=True, verbose_name="В архиве с")),
        migrations.AddField(model_name="tender", name="clarifications_raw", field=models.JSONField(blank=True, default=list, verbose_name="Разъяснения (сырые)")),
        migrations.AddField(model_name="tender", name="collecting_finished_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Окончание подачи заявок")),
        migrations.AddField(model_name="tender", name="complaints_raw", field=models.JSONField(blank=True, default=list, verbose_name="Жалобы (сырые)")),
        migrations.AddField(model_name="tender", name="currency_code", field=models.CharField(blank=True, max_length=8, verbose_name="Валюта")),
        migrations.AddField(model_name="tender", name="customer_inn", field=models.CharField(blank=True, max_length=32, verbose_name="ИНН заказчика")),
        migrations.AddField(model_name="tender", name="eis_url", field=models.URLField(blank=True, max_length=500, verbose_name="Ссылка на ЕИС")),
        migrations.AddField(model_name="tender", name="extras_checked_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Разъяснения/жалобы загружены")),
        migrations.AddField(model_name="tender", name="first_seen_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Впервые найден")),
        migrations.AddField(model_name="tender", name="last_pulled_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Последняя выгрузка")),
        migrations.AddField(model_name="tender", name="max_price", field=models.DecimalField(blank=True, decimal_places=2, max_digits=16, null=True, verbose_name="НМЦК")),
        migrations.AddField(model_name="tender", name="notification_checked_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Извещение загружено")),
        migrations.AddField(model_name="tender", name="notification_raw", field=models.JSONField(blank=True, default=dict, verbose_name="Извещение (сырое)")),
        migrations.AddField(model_name="tender", name="object_info", field=models.TextField(blank=True, verbose_name="Наименование объекта закупки (как в ЕИС)")),
        migrations.AddField(model_name="tender", name="okpd2", field=models.JSONField(blank=True, default=list, verbose_name="ОКПД2")),
        migrations.AddField(model_name="tender", name="opened_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Открыт пользователем (впервые)")),
        migrations.AddField(model_name="tender", name="published_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Опубликовано")),
        migrations.AddField(model_name="tender", name="purchase_type", field=models.CharField(blank=True, max_length=64, verbose_name="Тип закупки")),
        migrations.AddField(model_name="tender", name="raw", field=models.JSONField(blank=True, default=dict, verbose_name="Ответ API")),
        migrations.AddField(model_name="tender", name="region", field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Код региона")),
        migrations.AddField(model_name="tender", name="review", field=models.CharField(choices=[("unreviewed", "Не проверен"), ("interesting", "В работе")], default="unreviewed", max_length=16, verbose_name="Проверка")),
        migrations.AddField(model_name="tender", name="risk_assessment", field=models.JSONField(blank=True, default=dict, verbose_name="Оценка рисков")),
        migrations.AddField(model_name="tender", name="risk_assessment_docs", field=models.JSONField(blank=True, default=list, verbose_name="Документы, использованные при оценке")),
        migrations.AddField(model_name="tender", name="risk_checked_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Риски оценены")),
        migrations.AddField(model_name="tender", name="risk_error", field=models.CharField(blank=True, max_length=400, verbose_name="Ошибка оценки рисков")),
        migrations.AddField(model_name="tender", name="source", field=models.CharField(choices=[("eis", "ЕИС"), ("manual", "Вручную")], default="eis", max_length=12, verbose_name="Источник")),
        migrations.AddField(model_name="tender", name="stage", field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="Этап")),
        migrations.AddField(model_name="tender", name="status", field=models.CharField(choices=[("new", "Новый"), ("dismissed", "Скрыт"), ("pushed", "В работе")], default="new", max_length=16, verbose_name="Статус")),
        migrations.AddField(model_name="tender", name="title", field=models.TextField(blank=True, verbose_name="Название")),
        migrations.RunPython(copy_found_tenders_to_tenders, migrations.RunPython.noop),
    ]
