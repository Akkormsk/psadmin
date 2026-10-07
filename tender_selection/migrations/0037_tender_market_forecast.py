from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tender_selection", "0036_tender_notification_error"),
    ]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="market_forecast_percent",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name="Прогноз рынка по снижению, %"),
        ),
        migrations.AddField(
            model_name="tender",
            name="market_forecast_sample_count",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Закупок в прогнозе рынка"),
        ),
        migrations.AddField(
            model_name="tender",
            name="market_forecast_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Прогноз рынка рассчитан"),
        ),
    ]
