from django.db import migrations, models
import django.db.models.deletion


def create_default_expenses(apps, schema_editor):
    OperatingExpense = apps.get_model("finance", "OperatingExpense")
    for name, amount in (("Аренда", 60000), ("ЖКХ", 8000), ("Интернет", 3500)):
        OperatingExpense.objects.get_or_create(name=name, defaults={"amount": amount, "is_active": True})


class Migration(migrations.Migration):
    dependencies = [("finance", "0004_alter_payrollline_options")]

    operations = [
        migrations.CreateModel(
            name="OperatingExpense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True, verbose_name="Название расхода")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Сумма")),
                ("is_active", models.BooleanField(default=True, verbose_name="Учитывать")),
            ],
            options={"ordering": ("name",), "verbose_name": "Постоянный расход", "verbose_name_plural": "Постоянные расходы"},
        ),
        migrations.CreateModel(
            name="PeriodExpense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Название расхода")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="Сумма")),
                ("period", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="expenses", to="finance.financialperiod")),
                ("template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="finance.operatingexpense")),
            ],
            options={"verbose_name": "Расход периода", "verbose_name_plural": "Расходы периода"},
        ),
        migrations.AddConstraint(model_name="periodexpense", constraint=models.UniqueConstraint(fields=("period", "template"), name="unique_period_expense_template")),
        migrations.RunPython(create_default_expenses, migrations.RunPython.noop),
    ]
