"""Business actions shared by cash UI and assistant adapters."""
from __future__ import annotations

from .forms import CashTransactionForm
from .models import CashAuditLog, CashTransaction


class CashActionError(Exception):
    pass


def _snapshot(transaction):
    return {"date": transaction.operation_date.strftime("%d.%m.%Y"), "account": transaction.get_account_display(), "direction": transaction.get_direction_display(), "amount": f"{transaction.amount:.2f} ₽", "reason": transaction.reason}


def _write_audit(actor, action, message, transaction=None):
    CashAuditLog.objects.create(actor=actor, action=action, message=message, transaction=transaction)


def create_transaction(actor, values):
    form = CashTransactionForm(values)
    if not form.is_valid():
        raise CashActionError(form.errors.get_json_data())
    transaction = form.save(commit=False)
    transaction.created_by = actor
    transaction.save()
    snapshot = _snapshot(transaction)
    _write_audit(actor, CashAuditLog.ACTION_CREATED, f"Создал операцию: {snapshot['date']} · {snapshot['account']} · {snapshot['direction'].lower()} {snapshot['amount']} · {snapshot['reason']}", transaction)
    return {"transaction_id": transaction.pk, "transaction": snapshot}
