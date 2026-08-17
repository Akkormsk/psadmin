from decimal import Decimal

from django.db.models import Sum

from .models import CashReconciliation, CashTransaction


ZERO = Decimal("0")


def balance_for_date(operation_date, account):
    """Return opening, income, expense and closing balance for one account/day."""
    reconciliation = (
        CashReconciliation.objects.filter(effective_date__lte=operation_date)
        .order_by("-effective_date")
        .first()
    )
    if reconciliation is None:
        opening = ZERO
        start_date = None
    else:
        opening = reconciliation.cash_balance if account == CashTransaction.ACCOUNT_CASH else reconciliation.card_balance
        start_date = reconciliation.effective_date

    before_day = CashTransaction.objects.filter(account=account, operation_date__lt=operation_date)
    today = CashTransaction.objects.filter(account=account, operation_date=operation_date)
    if start_date:
        before_day = before_day.filter(operation_date__gte=start_date)
        today = today.filter(operation_date__gte=start_date)

    opening += before_day.filter(direction=CashTransaction.DIRECTION_INCOME).aggregate(total=Sum("amount"))["total"] or ZERO
    opening -= before_day.filter(direction=CashTransaction.DIRECTION_EXPENSE).aggregate(total=Sum("amount"))["total"] or ZERO
    income = today.filter(direction=CashTransaction.DIRECTION_INCOME).aggregate(total=Sum("amount"))["total"] or ZERO
    expense = today.filter(direction=CashTransaction.DIRECTION_EXPENSE).aggregate(total=Sum("amount"))["total"] or ZERO
    return {"opening": opening, "income": income, "expense": expense, "closing": opening + income - expense}
