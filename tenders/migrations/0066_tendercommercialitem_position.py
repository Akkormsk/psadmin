from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenders", "0065_task_routes")]
    operations = [
        migrations.AddField(
            model_name="tendercommercialitem",
            name="position",
            field=models.PositiveIntegerField(default=0, verbose_name="Порядок"),
        ),
    ]
