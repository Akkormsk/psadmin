from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("finance", "0003_payrollline_deductions_and_printer_rules")]

    operations = [
        migrations.AlterModelOptions(
            name="payrollline",
            options={"verbose_name": "Строка зарплаты", "verbose_name_plural": "Строки зарплаты"},
        ),
    ]
