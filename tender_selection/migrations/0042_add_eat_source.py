from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tender_selection", "0041_tender_profile_assessment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tender",
            name="source",
            field=models.CharField(
                "Источник", max_length=12, default="eis",
                choices=[("eis", "ЕИС"), ("eat", "ЕАТ «Берёзка»"), ("manual", "Вручную")],
            ),
        ),
    ]
