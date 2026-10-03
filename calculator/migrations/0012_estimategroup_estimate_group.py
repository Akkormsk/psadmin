from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("calculator", "0011_reset_calculatorsettings_sequence")]

    operations = [
        migrations.CreateModel(
            name="EstimateGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("calculator_type", models.CharField(choices=[("sheet", "Листовая печать"), ("wide", "Плоттер Canon")], default="sheet", max_length=20)),
                ("name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="calculator_estimate_groups", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name", "pk"], "verbose_name": "Группа расчётов", "verbose_name_plural": "Группы расчётов"},
        ),
        migrations.AddField(
            model_name="estimate",
            name="group",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="estimates", to="calculator.estimategroup"),
        ),
    ]
