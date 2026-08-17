from decimal import Decimal

from django.db import migrations, models


def apply_open_printer_rules(apps, schema_editor):
    FinancialPeriod = apps.get_model("finance", "FinancialPeriod")
    PayrollLine = apps.get_model("finance", "PayrollLine")
    for period in FinancialPeriod.objects.filter(is_closed=False):
        printer = PayrollLine.objects.filter(period=period, kind="printer").first()
        if printer:
            printer.fixed_salary = Decimal("90000")
            printer.shift_rate = Decimal("0")
            printer.leave_shift_rate = Decimal("1000")
            if not printer.advance:
                printer.advance = Decimal("45000")
            printer.save(update_fields=("fixed_salary", "shift_rate", "leave_shift_rate", "advance"))


class Migration(migrations.Migration):
    dependencies = [("finance", "0002_payroll_snapshots")]

    operations = [
        migrations.AddField(
            model_name="payrollline",
            name="deductions",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12, verbose_name="Вычеты"),
        ),
        migrations.RunPython(apply_open_printer_rules, migrations.RunPython.noop),
    ]
