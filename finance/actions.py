"""Business actions shared by finance UI and assistant adapters."""
from .forms import OrderRecordCreateForm
from .models import FinancialPeriod, KpiTier, ManagerKpiRate, ManagerSettings, OperatingExpense, PayrollLine, PeriodExpense
from django.contrib.auth import get_user_model
from django.db.models import Sum
from payroll.models import OrderRecord


class FinanceActionError(Exception):
    pass


DEFAULT_THRESHOLDS = (0, 100000, 200000, 300000)


def manager_data():
    tiers = [KpiTier.objects.get_or_create(threshold=threshold)[0] for threshold in DEFAULT_THRESHOLDS]
    users = get_user_model().objects.filter(is_superuser=False, is_active=True)
    for user in users:
        ManagerSettings.objects.get_or_create(user=user)
    result = []
    for settings in ManagerSettings.objects.filter(user__is_superuser=False, user__is_active=True).select_related("user").order_by("sort_order", "user__first_name", "user__last_name", "user_id"):
        rates = {tier.pk: ManagerKpiRate.objects.get_or_create(manager=settings.user, tier=tier)[0] for tier in tiers}
        result.append((settings.user, settings, [rates[tier.pk] for tier in tiers]))
    return tiers, result


def _sync_open_manager_lines(period, managers):
    if period.is_closed:
        return
    for manager, settings, _rates in managers:
        line, _ = PayrollLine.objects.get_or_create(period=period, manager=manager, defaults={"kind": PayrollLine.MANAGER, "name": manager.get_full_name().strip() or "Имя не указано", "shift_rate": settings.shift_rate, "leave_shift_rate": settings.leave_shift_rate, "design_percent": settings.design_percent})
        line.name = manager.get_full_name().strip() or "Имя не указано"
        line.shift_rate, line.leave_shift_rate, line.design_percent = settings.shift_rate, settings.leave_shift_rate, settings.design_percent
        line.save(update_fields=("name", "shift_rate", "leave_shift_rate", "design_percent"))


def refresh_period(period):
    for line in period.payroll_lines.filter(kind=PayrollLine.MANAGER).select_related("manager"):
        records = OrderRecord.objects.filter(manager=line.manager, accounting_period=period.code)
        profit = records.filter(record_type=OrderRecord.RECORD_ORDER).aggregate(total=Sum("gross_profit"))["total"] or 0
        design_total = records.filter(record_type=OrderRecord.RECORD_DESIGN).aggregate(total=Sum("gross_profit"))["total"] or 0
        rate = ManagerKpiRate.objects.filter(manager=line.manager, tier__threshold__lte=profit).order_by("-tier__threshold").first()
        line.order_profit = profit
        line.kpi_percent = rate.percent if rate else 0
        line.kpi_bonus = profit * line.kpi_percent / 100
        line.design_amount = design_total
        line.save(update_fields=("order_profit", "kpi_percent", "kpi_bonus", "design_amount"))


def save_payroll_lines(actor, code, updates):
    if not isinstance(code, str):
        raise FinanceActionError("Неверный расчётный период")
    if not isinstance(updates, list):
        raise FinanceActionError("Нужен список изменений зарплаты")
    period, _ = FinancialPeriod.objects.get_or_create(code=code)
    if period.is_closed:
        raise FinanceActionError("Закрытый период нельзя изменять")
    editable = period.payroll_lines.all() if actor.is_superuser else period.payroll_lines.filter(manager=actor)
    allowed = {line.pk: line for line in editable}
    editable_fields = (
        {"work_shifts", "leave_shifts", "fixed_salary", "deductions", "advance"}
        if actor.is_superuser else {"work_shifts", "leave_shifts"}
    )
    changed = []
    for item in updates:
        if not isinstance(item, dict) or not isinstance(item.get("line_id"), int):
            raise FinanceActionError("Некорректная строка зарплаты")
        line = allowed.get(item["line_id"])
        if line is None:
            raise FinanceActionError("Строка зарплаты недоступна")
        fields = [name for name in editable_fields if name in item]
        if not fields:
            continue
        for name in fields:
            setattr(line, name, item[name] or 0)
        line.save(update_fields=fields)
        changed.append(line.pk)
    refresh_period(period)
    return {"period": period.code, "updated_line_ids": changed}


def save_manager_settings(actor, values):
    if not actor.is_superuser or not isinstance(values, dict) or not isinstance(values.get("managers"), list):
        raise FinanceActionError("Некорректные параметры настроек менеджеров")
    order = {manager_id: position for position, manager_id in enumerate(values.get("employee_order", [])) if isinstance(manager_id, int)}
    tiers, managers = manager_data()
    by_id = {manager.pk: (settings, rates) for manager, settings, rates in managers}
    for item in values["managers"]:
        if not isinstance(item, dict) or item.get("manager_id") not in by_id:
            raise FinanceActionError("Менеджер не найден")
        settings, rates = by_id[item["manager_id"]]
        settings.shift_rate = item.get("shift_rate") or 0
        settings.leave_shift_rate = item.get("leave_shift_rate") or 0
        settings.design_percent = item.get("design_percent") or 90
        if item["manager_id"] in order:
            settings.sort_order = order[item["manager_id"]]
        settings.save()
        kpi = item.get("kpi", {})
        if not isinstance(kpi, dict):
            raise FinanceActionError("Некорректные ставки KPI")
        for tier, rate in zip(tiers, rates):
            rate.percent = kpi.get(str(tier.pk), kpi.get(tier.pk, rate.percent)) or 0
            rate.save()
    _tiers, refreshed = manager_data()
    for period in FinancialPeriod.objects.filter(is_closed=False):
        _sync_open_manager_lines(period, refreshed)
    return {"updated_manager_ids": [item["manager_id"] for item in values["managers"]]}


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


def save_operating_expenses(actor, values):
    if not actor.is_superuser:
        raise FinanceActionError("Недостаточно прав для изменения постоянных расходов")
    if not isinstance(values, dict):
        raise FinanceActionError("Некорректные параметры расходов")
    updated = 0
    for item in values.get("updates", []):
        if not isinstance(item, dict):
            raise FinanceActionError("Некорректные параметры расходов")
        expense = OperatingExpense.objects.filter(pk=item.get("id")).first()
        if expense is None:
            raise FinanceActionError("Постоянный расход не найден")
        expense.name = str(item.get("name", expense.name)).strip() or expense.name
        expense.amount = item.get("amount") or 0
        expense.is_active = True
        expense.save()
        updated += 1
    created_id = None
    new = values.get("new") or {}
    if not isinstance(new, dict):
        raise FinanceActionError("Некорректные параметры нового расхода")
    new_name = str(new.get("name", "")).strip()
    if new_name:
        expense, _created = OperatingExpense.objects.update_or_create(
            name=new_name,
            defaults={"amount": new.get("amount") or 0, "is_active": True},
        )
        created_id = expense.pk
    for period in FinancialPeriod.objects.filter(is_closed=False):
        _sync_open_period_expenses(period, refresh_values=True)
    return {"updated": updated, "created_id": created_id}


def delete_operating_expense(actor, expense_id):
    if not actor.is_superuser:
        raise FinanceActionError("Недостаточно прав для изменения постоянных расходов")
    expense = OperatingExpense.objects.filter(pk=expense_id).first()
    if expense is None:
        raise FinanceActionError("Постоянный расход не найден")
    PeriodExpense.objects.filter(template=expense, period__is_closed=False).delete()
    expense.delete()
    return {"expense_id": expense_id, "deleted": True}


def create_order_record(actor, values):
    form = OrderRecordCreateForm(values, user=actor)
    if not form.is_valid():
        raise FinanceActionError(form.errors.get_json_data())
    record = form.save(commit=False)
    record.manager = form.cleaned_data.get("manager") if actor.is_superuser else actor
    record.created_by = actor
    record.source = OrderRecord.SOURCE_MANUAL
    record.save()
    return {"order_record_id": record.pk, "order_number": record.order_number, "gross_profit": str(record.gross_profit), "accounting_period": record.accounting_period}


def update_order_record(actor, record_id, values):
    records = OrderRecord.objects.filter(pk=record_id)
    if not actor.is_superuser:
        records = records.filter(manager=actor)
    record = records.first()
    if record is None:
        raise FinanceActionError("Закрытый заказ не найден или недоступен")
    form = OrderRecordCreateForm(values, instance=record, user=actor)
    if not form.is_valid():
        raise FinanceActionError(form.errors.get_json_data())
    record = form.save(commit=False)
    if actor.is_superuser:
        record.manager = form.cleaned_data["manager"]
    record.save()
    return {"order_record_id": record.pk, "order_number": record.order_number, "gross_profit": str(record.gross_profit), "accounting_period": record.accounting_period}


def delete_order_record(actor, record_id):
    records = OrderRecord.objects.filter(pk=record_id)
    if not actor.is_superuser:
        records = records.filter(manager=actor)
    record = records.first()
    if record is None:
        raise FinanceActionError("Закрытый заказ не найден или недоступен")
    record.delete()
    return {"order_record_id": record_id, "deleted": True}

def set_financial_period_state(user, code, *, closed):
    """Shared period lock state for finance UI and assistant."""
    if not user.is_superuser:
        raise FinanceActionError("Недостаточно прав для закрытия периода")
    if not isinstance(code, str) or len(code) != 7 or code[4] != "-" or not code[:4].isdigit() or not code[5:].isdigit() or not (1 <= int(code[5:]) <= 12):
        raise FinanceActionError("Неверный расчётный период, ожидается ГГГГ-ММ")
    period, _ = FinancialPeriod.objects.get_or_create(code=code)
    period.is_closed = bool(closed)
    period.save(update_fields=("is_closed",))
    return {"period": period.code, "is_closed": period.is_closed}
