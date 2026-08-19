from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CashReconciliationForm, CashTransactionForm
from .models import CashAuditLog, CashReconciliation, CashTransaction
from .services import balance_for_date


def _selected_date(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    try:
        return date.fromisoformat(raw_date) if raw_date else timezone.localdate()
    except ValueError:
        return timezone.localdate()


def _cash_url(operation_date):
    return f"/cash/?date={operation_date.isoformat()}"


def _snapshot(transaction):
    return {"date": transaction.operation_date.strftime("%d.%m.%Y"), "account": transaction.get_account_display(), "direction": transaction.get_direction_display(), "amount": f"{transaction.amount:.2f} ₽", "reason": transaction.reason}


def _transaction_text(snapshot):
    return f"{snapshot['date']} · {snapshot['account']} · {snapshot['direction'].lower()} {snapshot['amount']} · {snapshot['reason']}"


def _write_audit(actor, action, message, transaction=None):
    CashAuditLog.objects.create(actor=actor, action=action, message=message, transaction=transaction)


@login_required
def home(request):
    selected_date = _selected_date(request)
    cash_transactions = list(CashTransaction.objects.filter(operation_date=selected_date, account=CashTransaction.ACCOUNT_CASH).select_related("created_by", "created_by__profile").defer("created_by__profile__avatar_data"))
    card_transactions = list(CashTransaction.objects.filter(operation_date=selected_date, account=CashTransaction.ACCOUNT_CARD).select_related("created_by", "created_by__profile").defer("created_by__profile__avatar_data"))
    for transaction in cash_transactions + card_transactions:
        transaction.edit_form = CashTransactionForm(instance=transaction, prefix=f"edit-{transaction.pk}")
    context = {
        "selected_date": selected_date,
        "cash_transactions": cash_transactions,
        "card_transactions": card_transactions,
        "cash_income_transactions": [item for item in cash_transactions if item.direction == CashTransaction.DIRECTION_INCOME],
        "cash_expense_transactions": [item for item in cash_transactions if item.direction == CashTransaction.DIRECTION_EXPENSE],
        "card_income_transactions": [item for item in card_transactions if item.direction == CashTransaction.DIRECTION_INCOME],
        "card_expense_transactions": [item for item in card_transactions if item.direction == CashTransaction.DIRECTION_EXPENSE],
        "cash_balance": balance_for_date(selected_date, CashTransaction.ACCOUNT_CASH),
        "card_balance": balance_for_date(selected_date, CashTransaction.ACCOUNT_CARD),
        "today": timezone.localdate(),
        "cash_create_form": CashTransactionForm(initial={"operation_date": selected_date, "account": CashTransaction.ACCOUNT_CASH}, prefix="create-cash"),
        "card_create_form": CashTransactionForm(initial={"operation_date": selected_date, "account": CashTransaction.ACCOUNT_CARD}, prefix="create-card"),
    }
    return render(request, "cash/home.html", context)


@login_required
def transaction_create(request):
    selected_date = _selected_date(request)
    account = request.GET.get("account", CashTransaction.ACCOUNT_CASH)
    prefix = f"create-{account}" if request.method == "POST" and f"create-{account}-operation_date" in request.POST else None
    form = CashTransactionForm(request.POST or None, initial={"operation_date": selected_date, "account": account}, prefix=prefix)
    if request.method == "POST" and form.is_valid():
        transaction = form.save(commit=False)
        transaction.created_by = request.user
        transaction.save()
        _write_audit(request.user, CashAuditLog.ACTION_CREATED, f"Создал операцию: {_transaction_text(_snapshot(transaction))}", transaction)
        messages.success(request, "Операция добавлена.")
        return redirect(_cash_url(transaction.operation_date))
    return render(request, "cash/transaction_form.html", {"form": form, "selected_date": selected_date, "title": "Новая операция"})


@login_required
def transaction_update(request, pk):
    transaction = get_object_or_404(CashTransaction, pk=pk)
    before = _snapshot(transaction)
    form = CashTransactionForm(request.POST or None, instance=transaction, prefix=f"edit-{pk}")
    if request.method == "POST" and form.is_valid():
        transaction = form.save()
        after = _snapshot(transaction)
        fields = (("date", "дата"), ("account", "счёт"), ("direction", "тип"), ("amount", "сумма"), ("reason", "основание"))
        changed = [f"{label}: {before[key]} → {after[key]}" for key, label in fields if before[key] != after[key]]
        if changed:
            _write_audit(request.user, CashAuditLog.ACTION_UPDATED, f"Изменил операцию #{transaction.pk}: " + "; ".join(changed), transaction)
        messages.success(request, "Операция сохранена.")
        return redirect(_cash_url(transaction.operation_date))
    return render(request, "cash/transaction_form.html", {"form": form, "selected_date": transaction.operation_date, "title": "Изменить операцию", "transaction": transaction})


@login_required
def transaction_delete(request, pk):
    transaction = get_object_or_404(CashTransaction, pk=pk)
    if request.method == "POST":
        selected_date = transaction.operation_date
        _write_audit(request.user, CashAuditLog.ACTION_DELETED, f"Удалил операцию: {_transaction_text(_snapshot(transaction))}")
        transaction.delete()
        messages.success(request, "Операция удалена.")
        return redirect(_cash_url(selected_date))
    return redirect(_cash_url(transaction.operation_date))


@login_required
@user_passes_test(lambda user: user.is_superuser)
def reconcile(request):
    form = CashReconciliationForm(request.POST or None, initial={"effective_date": _selected_date(request)})
    if request.method == "POST" and form.is_valid():
        reconciliation, created = CashReconciliation.objects.update_or_create(
            effective_date=form.cleaned_data["effective_date"],
            defaults={
                "cash_balance": form.cleaned_data["cash_balance"],
                "card_balance": form.cleaned_data["card_balance"],
                "note": form.cleaned_data["note"],
                "created_by": request.user,
            },
        )
        note = f" {reconciliation.note}" if reconciliation.note else ""
        action = "Создал" if created else "Обновил"
        _write_audit(request.user, CashAuditLog.ACTION_RECONCILED, f"{action} сверку на {reconciliation.effective_date:%d.%m.%Y}: наличные {reconciliation.cash_balance:.2f} ₽, карта {reconciliation.card_balance:.2f} ₽.{note}")
        messages.success(request, "Сверка сохранена. Все последующие остатки теперь считаются от неё.")
        return redirect(_cash_url(reconciliation.effective_date))
    return render(request, "cash/reconcile_form.html", {"form": form, "selected_date": _selected_date(request)})


@login_required
def audit_log(request):
    return render(request, "cash/audit_log.html", {"events": CashAuditLog.objects.select_related("actor", "actor__profile").defer("actor__profile__avatar_data")[:200]})
