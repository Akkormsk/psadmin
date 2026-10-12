"""Business actions shared by finance UI and assistant adapters."""
from .forms import OrderRecordCreateForm
from .models import FinancialPeriod, OperatingExpense, PeriodExpense
from payroll.models import OrderRecord


class FinanceActionError(Exception):
    pass


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
