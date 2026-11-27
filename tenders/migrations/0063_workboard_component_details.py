from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenders", "0062_workboard_ownership")]

    operations = [
        migrations.AddField(
            model_name="calculationcomponent",
            name="description",
            field=models.TextField(blank=True, verbose_name="Описание"),
        ),
        migrations.AlterField(
            model_name="calculationcomponent",
            name="quantity_per_parent",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                default=1,
                max_digits=14,
                null=True,
                verbose_name="Количество на единицу родителя",
            ),
        ),
    ]
