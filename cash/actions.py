"""Business actions shared by cash UI and assistant adapters."""
from __future__ import annotations

from .forms import CashReconciliationForm, CashTransactionForm
from .models import BankPayment, CashAuditLog, CashReconciliation, CashTransaction


class CashActionError(Exception):
    pass


def _snapshot(transaction):
    return {"date": transaction.operation_date.strftime("%d.%m.%Y"), "account": transaction.get_account_display(), "direction": transaction.get_direction_display(), "amount": f"{transaction.amount:.2f} ₽", "reason": transaction.reason}


def _transaction_text(snapshot):
    return " · ".join((snapshot["date"], snapshot["account"], snapshot["direction"].lower() + " " + snapshot["amount"], snapshot["reason"]))


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


def update_transaction(actor, transaction_id, values):
    transaction = CashTransaction.objects.filter(pk=transaction_id).first()
    if transaction is None:
        raise CashActionError("Операция кассы не найдена")
    before = _snapshot(transaction)
    form = CashTransactionForm(values, instance=transaction)
    if not form.is_valid():
        raise CashActionError(form.errors.get_json_data())
    transaction = form.save()
    after = _snapshot(transaction)
    labels = (("date", "дата"), ("account", "счёт"), ("direction", "тип"), ("amount", "сумма"), ("reason", "основание"))
    changed = [f"{label}: {before[key]} → {after[key]}" for key, label in labels if before[key] != after[key]]
    if changed:
        _write_audit(actor, CashAuditLog.ACTION_UPDATED, f"Изменил операцию #{transaction.pk}: " + "; ".join(changed), transaction)
    return {"transaction_id": transaction.pk, "transaction": after, "changed": changed}


def delete_transaction(actor, transaction_id):
    transaction = CashTransaction.objects.filter(pk=transaction_id).first()
    if transaction is None:
        raise CashActionError("Операция кассы не найдена")
    snapshot = _snapshot(transaction)
    transaction_id = transaction.pk
    _write_audit(actor, CashAuditLog.ACTION_DELETED, f"Удалил операцию: {_transaction_text(snapshot)}")
    transaction.delete()
    return {"transaction_id": transaction_id, "deleted": True}


def save_reconciliation(actor, values):
    form = CashReconciliationForm(values)
    if not form.is_valid():
        raise CashActionError(form.errors.get_json_data())
    reconciliation, created = CashReconciliation.objects.update_or_create(
        effective_date=form.cleaned_data["effective_date"],
        defaults={
            "cash_balance": form.cleaned_data["cash_balance"],
            "card_balance": form.cleaned_data["card_balance"],
            "note": form.cleaned_data["note"],
            "created_by": actor,
        },
    )
    action = "Создал" if created else "Обновил"
    _write_audit(actor, CashAuditLog.ACTION_RECONCILED, f"{action} сверку на {reconciliation.effective_date:%d.%m.%Y}: наличные {reconciliation.cash_balance:.2f} ₽, карта {reconciliation.card_balance:.2f} ₽.")
    return {"reconciliation_id": reconciliation.pk, "created": created}


def toggle_bank_payment_visibility(actor, payment_id):
    payment = BankPayment.objects.filter(pk=payment_id).first()
    if payment is None:
        raise CashActionError("Банковский платёж не найден")
    payment.hidden_from_managers = not payment.hidden_from_managers
    payment.save(update_fields=["hidden_from_managers"])
    verb = "Скрыл от менеджеров" if payment.hidden_from_managers else "Показал менеджерам"
    _write_audit(actor, CashAuditLog.ACTION_UPDATED, f"{verb} платёж из банка на {payment.amount:.2f} ₽ от «{payment.counterparty_name}» ({payment.operation_date:%d.%m.%Y})")
    return {"bank_payment_id": payment.pk, "hidden_from_managers": payment.hidden_from_managers}
