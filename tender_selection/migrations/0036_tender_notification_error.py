from django.db import migrations, models


def mark_unknown_as_retryable(apps, schema_editor):
    tender = apps.get_model("tender_selection", "Tender")
    tender.objects.filter(notification_raw={}, notification_checked_at__isnull=False).update(
        notification_error="Требуется повторная загрузка",
    )


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0035_profile_triage_and_dismissal_feedback")]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="notification_error",
            field=models.CharField(blank=True, max_length=300, verbose_name="Ошибка загрузки извещения"),
        ),
        migrations.RunPython(mark_unknown_as_retryable, migrations.RunPython.noop),
    ]
