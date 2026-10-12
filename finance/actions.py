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
