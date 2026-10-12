import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import modulbank
from .forms import CashReconciliationForm, CashTransactionForm
from .models import BankPayment, BankSyncState, CashAuditLog, CashReconciliation, CashTransaction
from .services import balance_for_date
from .actions import CashActionError, create_transaction, delete_transaction, save_reconciliation, sync_bank, toggle_bank_payment_visibility, update_transaction

BANK_WINDOW_DAYS = modulbank.WINDOW_DAYS


def _parse_date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _bank_context(request):
    """Панель «Банк — входящие платежи» под кассой: окно в последний месяц + фильтры."""
    today = timezone.localdate()
    window_start = today - timedelta(days=BANK_WINDOW_DAYS)
    is_admin = request.user.is_superuser

    date_from = max(_parse_date(request.GET.get("bank_from")) or window_start, window_start)
    date_to = min(_parse_date(request.GET.get("bank_to")) or today, today)
    query = (request.GET.get("bank_q") or "").strip()
    show_internal = is_admin and request.GET.get("bank_internal") == "1"

    base = BankPayment.objects.filter(operation_date__gte=date_from, operation_date__lte=date_to)
    if not is_admin:
        base = base.filter(hidden_from_managers=False)

    payments = base if show_internal else base.filter(is_internal=False)
    if query:
        payments = payments.filter(
            Q(counterparty_name__icontains=query)
            | Q(counterparty_inn__icontains=query)
            | Q(payment_purpose__icontains=query)
            | Q(doc_number__icontains=query)
        )
    payments = list(payments)
    internal_count = base.filter(is_internal=True).count()

    toggle_params = {"date": request.GET.get("date") or ""}
    if date_from != window_start:
        toggle_params["bank_from"] = date_from.isoformat()
    if date_to != today:
        toggle_params["bank_to"] = date_to.isoformat()
    if query:
        toggle_params["bank_q"] = query
    if not show_internal:
        toggle_params["bank_internal"] = "1"
    bank_toggle_url = "?" + urlencode({k: v for k, v in toggle_params.items() if v})

    return {
        "bank_payments": payments,
        "bank_total": sum((item.amount for item in payments), Decimal("0")),
        "bank_count": len(payments),
        "bank_filter": {"from": date_from, "to": date_to, "q": query},
        "bank_window_start": window_start,
        "bank_today": today,
        "bank_is_admin": is_admin,
        "bank_show_internal": show_internal,
        "bank_internal_count": internal_count,
        "bank_toggle_url": bank_toggle_url,
        "bank_has_filter": bool(query or date_from != window_start or date_to != today),
        "bank_sync_state": BankSyncState.load(),
        "bank_configured": bool(settings.MODULBANK_TOKEN and settings.MODULBANK_ACCOUNT_ID),
    }


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
    context.update(_bank_context(request))
    return render(request, "cash/home.html", context)


@login_required
def transaction_create(request):
    selected_date = _selected_date(request)
    account = request.GET.get("account", CashTransaction.ACCOUNT_CASH)
    prefix = f"create-{account}" if request.method == "POST" and f"create-{account}-operation_date" in request.POST else None
    form = CashTransactionForm(request.POST or None, initial={"operation_date": selected_date, "account": account}, prefix=prefix)
    if request.method == "POST" and form.is_valid():
        values = {name: form.cleaned_data[name] for name in CashTransactionForm.Meta.fields}
        try:
            transaction_id = create_transaction(request.user, values)["transaction_id"]
        except CashActionError:
            return render(request, "cash/transaction_form.html", {"form": form, "selected_date": selected_date, "title": "Новая операция"})
        transaction = CashTransaction.objects.get(pk=transaction_id)
        messages.success(request, "Операция добавлена.")
        return redirect(_cash_url(transaction.operation_date))
    return render(request, "cash/transaction_form.html", {"form": form, "selected_date": selected_date, "title": "Новая операция"})


@login_required
def transaction_update(request, pk):
    transaction = get_object_or_404(CashTransaction, pk=pk)
    before = _snapshot(transaction)
    form = CashTransactionForm(request.POST or None, instance=transaction, prefix=f"edit-{pk}")
    if request.method == "POST" and form.is_valid():
        values = {name: form.cleaned_data[name] for name in CashTransactionForm.Meta.fields}
        try:
            update_transaction(request.user, transaction.pk, values)
        except CashActionError:
            return render(request, "cash/transaction_form.html", {"form": form, "selected_date": transaction.operation_date, "title": "Изменить операцию", "transaction": transaction})
        messages.success(request, "Операция сохранена.")
        return redirect(_cash_url(transaction.operation_date))
    return render(request, "cash/transaction_form.html", {"form": form, "selected_date": transaction.operation_date, "title": "Изменить операцию", "transaction": transaction})


@login_required
def transaction_delete(request, pk):
    transaction = get_object_or_404(CashTransaction, pk=pk)
    if request.method == "POST":
        selected_date = transaction.operation_date
        delete_transaction(request.user, transaction.pk)
        messages.success(request, "Операция удалена.")
        return redirect(_cash_url(selected_date))
    return redirect(_cash_url(transaction.operation_date))


@login_required
@user_passes_test(lambda user: user.is_superuser)
def reconcile(request):
    form = CashReconciliationForm(request.POST or None, initial={"effective_date": _selected_date(request)})
    if request.method == "POST" and form.is_valid():
        reconciliation_data = save_reconciliation(request.user, form.cleaned_data)
        reconciliation = CashReconciliation.objects.get(pk=reconciliation_data["reconciliation_id"])
        messages.success(request, "Сверка сохранена. Все последующие остатки теперь считаются от неё.")
        return redirect(_cash_url(reconciliation.effective_date))
    return render(request, "cash/reconcile_form.html", {"form": form, "selected_date": _selected_date(request)})


@login_required
def audit_log(request):
    return render(request, "cash/audit_log.html", {"events": CashAuditLog.objects.select_related("actor", "actor__profile").defer("actor__profile__avatar_data")[:200]})


@login_required
@user_passes_test(lambda user: user.is_superuser)
@require_POST
def bank_sync_now(request):
    try:
        touched = sync_bank(request.user)["touched"]
    except CashActionError as error:
        messages.error(request, f"Синхронизация с банком не удалась: {error}")
    else:
        messages.success(request, f"Синхронизация с банком выполнена. Обработано платежей: {touched}.")
    return redirect("cash_home")


@login_required
@user_passes_test(lambda user: user.is_superuser)
@require_POST
def bank_payment_toggle(request, pk):
    payment = get_object_or_404(BankPayment, pk=pk)
    toggle_bank_payment_visibility(request.user, payment.pk)
    messages.success(request, "Видимость платежа обновлена.")
    next_url = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "cash_home"
    return redirect(next_url)


@csrf_exempt
@require_POST
def bank_webhook(request):
    """Приёмник веб-хуков Модульбанка о новых транзакциях."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("bad json")
    operation = payload.get("operation") or {}
    if not modulbank.verify_webhook(operation.get("id", ""), payload.get("SHA1Hash", "")):
        return HttpResponseForbidden("bad signature")
    modulbank.upsert_operation(operation)
    return HttpResponse("ok")
