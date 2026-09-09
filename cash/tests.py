import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import modulbank
from .forms import CashReconciliationForm
from .models import BankPayment, CashAuditLog, CashReconciliation, CashTransaction
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


@override_settings(MODULBANK_TOKEN="NDIWJFNASDJKFHNASDJFASDJKFHASDJKFHASDJFHASDK", MODULBANK_ACCOUNT_ID="acc-1")
class BankPaymentTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.admin = get_user_model().objects.create_superuser(username="bank-admin", password="pw")
        self.manager = get_user_model().objects.create_user(username="bank-manager", password="pw")
        self.visible = BankPayment.objects.create(
            external_id="op-visible", status="Received", direction="Debet", amount=Decimal("184000.00"),
            counterparty_name="ООО Ромашка", counterparty_inn="7701234567", payment_purpose="Оплата по счёту 312",
            operation_date=self.today - timedelta(days=2),
        )
        self.hidden = BankPayment.objects.create(
            external_id="op-hidden", status="Received", direction="Debet", amount=Decimal("9800.00"),
            counterparty_name="АО ТрейдСервис", payment_purpose="Аванс", operation_date=self.today - timedelta(days=5),
            hidden_from_managers=True,
        )
        self.old = BankPayment.objects.create(
            external_id="op-old", status="Received", direction="Debet", amount=Decimal("1000.00"),
            counterparty_name="Старый платёж", operation_date=self.today - timedelta(days=90),
        )

    def test_manager_sees_only_visible_recent_payments(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("cash_home"))
        self.assertContains(response, "ООО Ромашка")
        self.assertNotContains(response, "АО ТрейдСервис")
        self.assertNotContains(response, "Старый платёж")
        self.assertNotContains(response, "Синхронизировать сейчас")

    def test_admin_sees_hidden_payment_and_controls(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cash_home"))
        self.assertContains(response, "АО ТрейдСервис")
        self.assertContains(response, "Синхронизировать сейчас")

    def test_query_filter_matches_name_and_inn(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cash_home"), {"bank_q": "7701234567"})
        self.assertContains(response, "ООО Ромашка")
        self.assertNotContains(response, "АО ТрейдСервис")

    def test_admin_toggles_visibility_and_it_is_audited(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("bank_payment_toggle", args=[self.visible.pk]))
        self.assertEqual(response.status_code, 302)
        self.visible.refresh_from_db()
        self.assertTrue(self.visible.hidden_from_managers)
        self.assertEqual(CashAuditLog.objects.filter(action=CashAuditLog.ACTION_UPDATED).count(), 1)

    def test_manager_cannot_toggle_visibility(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse("bank_payment_toggle", args=[self.visible.pk]))
        self.assertEqual(response.status_code, 302)
        self.visible.refresh_from_db()
        self.assertFalse(self.visible.hidden_from_managers)

    def test_webhook_stores_payment_with_valid_signature(self):
        operation = {
            "id": "webhook-op-1", "companyId": "c1", "status": "Received", "category": "Debet",
            "contragentName": "ООО Вебхук", "contragentInn": "5024090909", "currency": "RUR",
            "amount": 55000.0, "bankAccountNumber": "40802810070000000001",
            "paymentPurpose": "Оплата счёта 900", "executed": "2026-09-08T10:00:00", "created": "2026-09-08T10:00:00",
        }
        token = "NDIWJFNASDJKFHNASDJFASDJKFHASDJKFHASDJFHASDK"
        signature = hashlib.sha1(f"{token[:10]}&{operation['id']}".encode("utf-8")).hexdigest()
        response = self.client.post(
            reverse("bank_webhook"),
            data=json.dumps({"companyInn": "5024090909", "operation": operation, "SHA1Hash": signature}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payment = BankPayment.objects.get(external_id="webhook-op-1")
        self.assertEqual(payment.amount, Decimal("55000.00"))
        self.assertEqual(payment.counterparty_name, "ООО Вебхук")

    def test_webhook_rejects_bad_signature(self):
        response = self.client.post(
            reverse("bank_webhook"),
            data=json.dumps({"operation": {"id": "x"}, "SHA1Hash": "deadbeef"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BankPayment.objects.filter(external_id="x").exists())

    def test_upsert_keeps_manual_visibility_flag(self):
        modulbank.upsert_operation({"id": "op-visible", "category": "Debet", "amount": 5, "status": "Received"})
        modulbank.upsert_operation({"id": "op-hidden", "category": "Debet", "amount": 5, "status": "Received"})
        self.assertFalse(BankPayment.objects.get(external_id="op-visible").hidden_from_managers)
        self.assertTrue(BankPayment.objects.get(external_id="op-hidden").hidden_from_managers)

    def test_internal_transfers_are_flagged_and_hidden_from_the_list(self):
        own = {"7712345678", "40802810170010029231"}
        modulbank.upsert_operation({"id": "op-c", "category": "Debet", "amount": 1000, "status": "Received", "contragentName": "ООО Клиент", "contragentInn": "5024090909"}, own)
        modulbank.upsert_operation({"id": "op-s", "category": "Debet", "amount": 50000, "status": "Received", "contragentName": "ИП Я Сам", "contragentInn": "7712345678"}, own)
        self.assertFalse(BankPayment.objects.get(external_id="op-c").is_internal)
        self.assertTrue(BankPayment.objects.get(external_id="op-s").is_internal)

        self.client.force_login(self.admin)
        default_view = self.client.get(reverse("cash_home"))
        self.assertContains(default_view, "ООО Клиент")
        self.assertNotContains(default_view, "ИП Я Сам")
        with_internal = self.client.get(reverse("cash_home"), {"bank_internal": "1"})
        self.assertContains(with_internal, "ИП Я Сам")
