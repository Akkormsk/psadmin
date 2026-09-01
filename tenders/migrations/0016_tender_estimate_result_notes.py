from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0015_tender_estimate_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenderestimate",
            name="result_notes",
            field=models.TextField(blank=True, verbose_name="Результат торгов"),
        ),
        migrations.AlterField(
            model_name="tenderestimate",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Черновик"),
                    ("pending", "В ожидании"),
                    ("not_participated", "Не участвовали"),
                    ("lost", "Проигран"),
                    ("won", "Выигран"),
                ],
                default="draft",
                max_length=16,
                verbose_name="Статус",
            ),
        ),
    ]
