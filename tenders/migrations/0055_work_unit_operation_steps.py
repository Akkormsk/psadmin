from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenders", "0054_commercial_items_components")]

    operations = [
        migrations.AddField(
            model_name="tendercomputeworkunit",
            name="operation_steps",
            field=models.ManyToManyField(
                blank=True,
                related_name="preparation_work_units",
                to="tenders.componentoperationstep",
            ),
        ),
    ]
