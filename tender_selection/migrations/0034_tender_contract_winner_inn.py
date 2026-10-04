from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tender_selection", "0033_archive_history_and_forecast_control")]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="contract_winner_inn",
            field=models.CharField(blank=True, max_length=32, verbose_name="ИНН победителя"),
        ),
    ]
