"""Business actions shared by finance UI and assistant adapters."""
from .forms import OrderRecordCreateForm
from payroll.models import OrderRecord


class FinanceActionError(Exception):
    pass


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
