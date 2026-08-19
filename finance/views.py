from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import get_user_model
from django.shortcuts import redirect, render
from django.utils import timezone
from calendar import monthrange

from .models import KpiTier, ManagerKpiRate, ManagerSettings
from .models import FinancialPeriod, OperatingExpense, PayrollLine, PeriodExpense
from payroll.models import OrderRecord
from django.db.models import Sum
from django.db.models import Case, IntegerField, Value, When
from decimal import Decimal


DEFAULT_THRESHOLDS = (0, 100000, 200000, 300000)
PRINTER_FIXED_SALARY = Decimal("90000")
PRINTER_ADVANCE = Decimal("45000")
PRINTER_LEAVE_DAY_RATE = Decimal("1000")


def _manager_data():
    tiers = []
    for threshold in DEFAULT_THRESHOLDS:
        tier, _ = KpiTier.objects.get_or_create(threshold=threshold)
        tiers.append(tier)

    managers = get_user_model().objects.filter(is_superuser=False).order_by("last_name", "first_name", "username")
    result = []
    for manager in managers:
        settings, _ = ManagerSettings.objects.get_or_create(user=manager)
        rates = {}
        for tier in tiers:
            rate, _ = ManagerKpiRate.objects.get_or_create(manager=manager, tier=tier)
            rates[tier.pk] = rate
        result.append((manager, settings, [rates[tier.pk] for tier in tiers]))
    return tiers, result


@login_required
@user_passes_test(lambda user: user.is_superuser)
def dashboard(request):
    current_period = timezone.localdate().strftime("%Y-%m")
    return redirect(f"/finance/calculation/?period={current_period}")


def _refresh_period(period):
    for line in period.payroll_lines.filter(kind=PayrollLine.MANAGER).select_related("manager"):
        profit = OrderRecord.objects.filter(manager=line.manager, accounting_period=period.code).aggregate(total=Sum("gross_profit"))["total"] or Decimal("0")
        rate = ManagerKpiRate.objects.filter(manager=line.manager, tier__threshold__lte=profit).order_by("-tier__threshold").first()
        line.order_profit = profit
        line.kpi_percent = rate.percent if rate else Decimal("0")
        line.kpi_bonus = profit * line.kpi_percent / Decimal("100")
        line.save(update_fields=("order_profit", "kpi_percent", "kpi_bonus"))


def _sync_open_manager_lines(period, managers):
    """Open periods use current manager rates; closed periods remain snapshots."""
    if period.is_closed:
        return
    for manager, settings, _ in managers:
        line, _ = PayrollLine.objects.get_or_create(
            period=period,
            manager=manager,
            defaults={
                "kind": PayrollLine.MANAGER,
                "name": manager.get_full_name() or manager.username,
                "shift_rate": settings.shift_rate,
                "leave_shift_rate": settings.leave_shift_rate,
            },
        )
        line.name = manager.get_full_name() or manager.username
        line.shift_rate = settings.shift_rate
        line.leave_shift_rate = settings.leave_shift_rate
        line.save(update_fields=("name", "shift_rate", "leave_shift_rate"))


def _sync_open_printer_line(period):
    """Printer: 90k monthly fixed salary, 45k advance and 1k per leave day."""
    if period.is_closed:
        return
    line, created = PayrollLine.objects.get_or_create(
        period=period,
        manager=None,
        kind=PayrollLine.PRINTER,
        defaults={
            "name": "Печатник",
            "fixed_salary": PRINTER_FIXED_SALARY,
            "leave_shift_rate": PRINTER_LEAVE_DAY_RATE,
            "advance": PRINTER_ADVANCE,
        },
    )
    line.fixed_salary = PRINTER_FIXED_SALARY
    line.shift_rate = Decimal("0")
    line.leave_shift_rate = PRINTER_LEAVE_DAY_RATE
    if created:
        line.advance = PRINTER_ADVANCE
    line.save(update_fields=("fixed_salary", "shift_rate", "leave_shift_rate", "advance"))


def _base_salary(line, period):
    if line.kind == PayrollLine.PRINTER:
        year, month = map(int, period.code.split("-"))
        days_in_month = Decimal(monthrange(year, month)[1])
        leave_days = Decimal(min(line.leave_shifts, int(days_in_month)))
        worked_part = line.fixed_salary / days_in_month * (days_in_month - leave_days)
        return worked_part + leave_days * line.leave_shift_rate
    return line.work_shifts * line.shift_rate + line.leave_shifts * line.leave_shift_rate


def _sync_open_period_expenses(period, refresh_values=False):
    if period.is_closed:
        return
    if refresh_values:
        PeriodExpense.objects.filter(period=period, template__is_active=False).delete()
    for template in OperatingExpense.objects.filter(is_active=True):
        expense, created = PeriodExpense.objects.get_or_create(
            period=period,
            template=template,
            defaults={"name": template.name, "amount": template.amount},
        )
        if refresh_values and not created:
            expense.name = template.name
            expense.amount = template.amount
            expense.save(update_fields=("name", "amount"))


@login_required
@user_passes_test(lambda user: user.is_superuser)
def calculation(request):
    code = request.GET.get("period") or request.POST.get("period") or timezone.localdate().strftime("%Y-%m")
    period, _ = FinancialPeriod.objects.get_or_create(code=code)
    tiers, managers = _manager_data()
    _sync_open_manager_lines(period, managers)
    if not period.is_closed:
        _sync_open_printer_line(period)
        PayrollLine.objects.get_or_create(period=period, manager=None, kind=PayrollLine.DESIGNER, defaults={"name": "Дизайнер"})
    _sync_open_period_expenses(period)
    if request.method == "POST" and request.POST.get("action") == "reopen":
        period.is_closed = False
        period.save(update_fields=("is_closed",))
        return redirect(f"{request.path}?period={code}")
    if request.method == "POST" and not period.is_closed:
        for line in period.payroll_lines.all():
            for field in ("work_shifts", "leave_shifts", "fixed_salary", "design_amount", "design_percent", "deductions", "advance"):
                value = request.POST.get(f"{field}_{line.pk}")
                if value is not None:
                    setattr(line, field, value or 0)
            line.save()
        _refresh_period(period)
        if request.POST.get("action") == "close":
            period.is_closed = True
            period.save(update_fields=("is_closed",))
        return redirect(f"{request.path}?period={code}")
    _refresh_period(period) if not period.is_closed else None
    lines = period.payroll_lines.order_by(
        Case(
            When(kind=PayrollLine.MANAGER, then=Value(0)),
            When(kind=PayrollLine.DESIGNER, then=Value(1)),
            When(kind=PayrollLine.PRINTER, then=Value(2)),
            output_field=IntegerField(),
        ),
        "name",
    )
    manager_payroll = Decimal("0")
    for line in lines:
        base = _base_salary(line, period)
        fixed_salary = Decimal("0") if line.kind == PayrollLine.PRINTER else line.fixed_salary
        line.total = base + fixed_salary + line.kpi_bonus + line.design_amount * line.design_percent / Decimal("100") - line.deductions
        line.balance = line.total - line.advance
        if line.kind == PayrollLine.MANAGER:
            manager_payroll += base + line.kpi_bonus - line.deductions
    order_total = OrderRecord.objects.filter(accounting_period=code).aggregate(total=Sum("gross_profit"))["total"] or Decimal("0")
    expense_total = period.expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    current_period = timezone.localdate().strftime("%Y-%m")
    period_codes = sorted(
        set(OrderRecord.objects.values_list("accounting_period", flat=True))
        | set(FinancialPeriod.objects.values_list("code", flat=True))
        | {code, current_period},
        reverse=True,
    )
    month_names = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
    period_options = [(value, f"{month_names[int(value[5:]) - 1]} {value[:4]}") for value in period_codes]
    for line in lines:
        line.base_salary = _base_salary(line, period)
    return render(request, "finance/calculation.html", {"period": period, "lines": lines, "period_options": period_options, "order_total": order_total, "manager_payroll": manager_payroll, "expense_total": expense_total, "company_profit": order_total - manager_payroll - expense_total})


@login_required
@user_passes_test(lambda user: user.is_superuser)
def expenses(request):
    period_code = request.GET.get("period") or request.POST.get("period") or timezone.localdate().strftime("%Y-%m")
    period, _ = FinancialPeriod.objects.get_or_create(code=period_code)
    if request.method == "POST":
        delete_id = request.POST.get("delete_expense")
        if delete_id:
            expense = OperatingExpense.objects.filter(pk=delete_id).first()
            if expense:
                PeriodExpense.objects.filter(template=expense, period__is_closed=False).delete()
                expense.delete()
            return redirect(f"{request.path}?period={period_code}")
        for expense in OperatingExpense.objects.all():
            expense.name = request.POST.get(f"name_{expense.pk}", expense.name).strip() or expense.name
            expense.amount = request.POST.get(f"amount_{expense.pk}") or 0
            expense.is_active = True
            expense.save()
        new_name = request.POST.get("new_name", "").strip()
        new_amount = request.POST.get("new_amount") or 0
        if new_name:
            OperatingExpense.objects.update_or_create(name=new_name, defaults={"amount": new_amount, "is_active": True})
        for open_period in FinancialPeriod.objects.filter(is_closed=False):
            _sync_open_period_expenses(open_period, refresh_values=True)
        return redirect(f"{request.path}?period={period_code}")
    _sync_open_period_expenses(period)
    return render(request, "finance/expenses.html", {"period": period, "expenses": OperatingExpense.objects.all()})


@login_required
@user_passes_test(lambda user: user.is_superuser)
def manager_settings(request):
    tiers, managers = _manager_data()
    if request.method == "POST":
        for manager, settings, rates in managers:
            settings.shift_rate = request.POST.get(f"shift_rate_{manager.pk}") or 0
            settings.leave_shift_rate = request.POST.get(f"leave_rate_{manager.pk}") or 0
            settings.save()
            for tier, rate in zip(tiers, rates):
                rate.percent = request.POST.get(f"kpi_{manager.pk}_{tier.pk}") or 0
                rate.save()
        for period in FinancialPeriod.objects.filter(is_closed=False):
            _sync_open_manager_lines(period, managers)
        return redirect("finance:manager_settings")
    return render(request, "finance/manager_settings.html", {"tiers": tiers, "managers": managers})
