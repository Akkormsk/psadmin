from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import CashReconciliationForm
from .models import CashAuditLog, CashReconciliation, CashTransaction
from .services import balance_for_date


class CashBalanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cash-test", password="test-password")
        CashReconciliation.objects.all().delete()
        CashReconciliation.objects.create(
            effective_date=date(2026, 8, 17),
            cash_balance=Decimal("403.00"),
            card_balance=Decimal("84993.00"),
        )

    def make_transaction(self, operation_date, account, direction, amount):
        return CashTransaction.objects.create(
            operation_date=operation_date,
            account=account,
            direction=direction,
            amount=Decimal(amount),
            reason="Тест",
            created_by=self.user,
        )

    def test_balance_chain_carries_forward(self):
        self.make_transaction(date(2026, 8, 17), "cash", "income", "100.00")
        self.make_transaction(date(2026, 8, 18), "cash", "expense", "50.00")

        balance = balance_for_date(date(2026, 8, 18), "cash")

        self.assertEqual(balance["opening"], Decimal("503.00"))
        self.assertEqual(balance["expense"], Decimal("50.00"))
        self.assertEqual(balance["closing"], Decimal("453.00"))

    def test_later_reconciliation_becomes_new_anchor(self):
        self.make_transaction(date(2026, 8, 17), "card", "expense", "1000.00")
        CashReconciliation.objects.create(
            effective_date=date(2026, 8, 20),
            cash_balance=Decimal("900.00"),
            card_balance=Decimal("50000.00"),
        )
        self.make_transaction(date(2026, 8, 20), "card", "income", "300.00")

        balance = balance_for_date(date(2026, 8, 21), "card")

        self.assertEqual(balance["opening"], Decimal("50300.00"))

    def test_reconciliation_form_allows_replacing_same_date(self):
        form = CashReconciliationForm(
            data={"effective_date": "2026-08-17", "cash_balance": "500", "card_balance": "80000", "note": "Повторная сверка"}
        )
        self.assertTrue(form.is_valid())

    def test_any_logged_in_user_can_create_transaction_and_it_is_audited(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("transaction_create") + "?date=2026-08-17&account=cash",
            data={"operation_date": "2026-08-17", "account": "cash", "direction": "income", "amount": "200", "reason": "Заказ №1"},
        )

        self.assertRedirects(response, "/cash/?date=2026-08-17")
        self.assertEqual(CashTransaction.objects.count(), 1)
        self.assertEqual(CashAuditLog.objects.filter(action=CashAuditLog.ACTION_CREATED).count(), 1)

    def test_modal_create_form_uses_prefixed_fields(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("transaction_create") + "?date=2026-08-17&account=card",
            data={"create-card-operation_date": "2026-08-17", "create-card-account": "card", "create-card-direction": "income", "create-card-amount": "500", "create-card-reason": "Оплата"},
        )

        self.assertRedirects(response, "/cash/?date=2026-08-17")
        self.assertEqual(CashTransaction.objects.get().account, CashTransaction.ACCOUNT_CARD)

    def test_cash_home_renders_both_accounts_for_logged_in_user(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("cash_home") + "?date=2026-08-17")

        self.assertContains(response, "Наличные")
        self.assertContains(response, "Карта")
        self.assertContains(response, "403.00")

    def test_any_logged_in_user_can_view_history_but_not_reconcile(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(reverse("audit_log")).status_code, 200)
        self.assertEqual(self.client.get(reverse("reconcile")).status_code, 302)

    def test_admin_can_replace_reconciliation_for_same_date(self):
        admin = get_user_model().objects.create_superuser(username="cash-admin", password="test-password")
        self.client.force_login(admin)

        response = self.client.post(
            reverse("reconcile"),
            data={"effective_date": "2026-08-17", "cash_balance": "700", "card_balance": "80000", "note": "Повторная сверка"},
        )

        self.assertRedirects(response, "/cash/?date=2026-08-17")
        self.assertEqual(CashReconciliation.objects.get(effective_date=date(2026, 8, 17)).cash_balance, Decimal("700.00"))
