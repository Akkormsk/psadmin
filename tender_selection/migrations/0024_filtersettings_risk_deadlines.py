from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0023_backfill_missing_tender_anchors")]

    operations = [
        migrations.AddField(
            model_name="filtersettings",
            name="risk_warning_days",
            field=models.PositiveSmallIntegerField(default=14, verbose_name="Риск: короткий срок, дней"),
        ),
        migrations.AddField(
            model_name="filtersettings",
            name="risk_critical_days",
            field=models.PositiveSmallIntegerField(default=7, verbose_name="Риск: критический срок, дней"),
        ),
    ]
