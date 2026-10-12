from django.contrib.auth import get_user_model
from django.test import TestCase
from datetime import date

from .assistant_tools import AssistantToolConfirmationRequired, available_tools, execute_tool
from .models import ProcessDefinition


class AssistantToolRegistryTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("assistant-admin", "assistant@example.test", "password")
        self.user = get_user_model().objects.create_user("assistant-user")
        self.stage, _ = ProcessDefinition.objects.get_or_create(name="Швейное производство под ключ", role="production")

    def test_inventory_describes_real_registered_tools_and_permissions(self):
        admin_tools = {item["id"]: item for item in available_tools(self.admin)}
        user_tools = {item["id"]: item for item in available_tools(self.user)}

        self.assertIn("provider.calculate", admin_tools)
        self.assertTrue(admin_tools["provider.create_draft"]["requires_confirmation"])
        self.assertFalse(user_tools["provider.create_draft"]["available"])

    def test_mutating_provider_draft_requires_confirmation(self):
        payload = {"name": "Черновой пошив", "stage_ids": [self.stage.pk], "source_text": "Прайс получен"}

        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.create_draft", self.admin, payload)
        result = execute_tool("provider.create_draft", self.admin, payload, confirmed=True)

        self.assertEqual(result["status"], "draft")
        self.assertEqual(execute_tool("provider.find", self.admin, {"query": "Черновой"})["counterparties"][0]["name"], "Черновой пошив")

    def test_read_uses_real_cash_records_and_aggregates_before_limit(self):
        from cash.models import CashTransaction

        for amount in ("10.00", "20.00", "30.00"):
            CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount=amount, reason="Бумага", created_by=self.admin)
        result = execute_tool("read", self.admin, {"plan": {"entity": "cash.CashTransaction", "fields": ["id", "amount"], "limit": 1, "aggregates": [{"op": "sum", "field": "amount"}]}})
        self.assertEqual(result["total_count"], 3)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(str(result["aggregates"]["sum_amount"]), "60")

    def test_read_rejects_sensitive_bank_account_and_non_owner(self):
        from cash.models import BankPayment

        BankPayment.objects.create(external_id="test-payment", amount="10", operation_date=date(2026, 9, 1), account_number="secret")
        with self.assertRaisesRegex(Exception, "недоступные поля"):
            execute_tool("read", self.admin, {"plan": {"entity": "cash.BankPayment", "fields": ["account_number"]}})
        with self.assertRaisesRegex(Exception, "Недостаточно прав"):
            execute_tool("read", self.user, {"plan": {"entity": "cash.BankPayment", "fields": ["id"]}})

    def test_cash_action_uses_existing_form_and_audit(self):
        from cash.models import CashAuditLog, CashTransaction

        payload = {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Доставка"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("cash.transaction_create", self.admin, payload)
        result = execute_tool("cash.transaction_create", self.admin, payload, confirmed=True)
        self.assertEqual(CashTransaction.objects.get(pk=result["transaction_id"]).reason, "Доставка")
        self.assertEqual(CashAuditLog.objects.filter(transaction_id=result["transaction_id"], action=CashAuditLog.ACTION_CREATED).count(), 1)

    def test_finance_action_uses_existing_order_form(self):
        from payroll.models import OrderRecord

        manager = get_user_model().objects.create_user("order-manager")
        payload = {"record_type": "order", "order_number": "123", "gross_profit": "85000", "accounting_period": "2026-10", "manager": str(manager.pk)}
        result = execute_tool("finance.order_record_create", self.admin, payload, confirmed=True)
        self.assertEqual(OrderRecord.objects.get(pk=result["order_record_id"]).manager, manager)

    def test_catalog_is_generated_from_django_models_and_real_actions(self):
        result = execute_tool("assistant.catalog", self.admin, {})
        self.assertTrue(any(item["model"] == "cash.CashTransaction" for item in result["models"]))
        actions = {item["id"]: item for item in result["actions"]}
        self.assertIn("cash.transaction_update", actions)
        self.assertIn("tender.filter_words_update", actions)
        self.assertTrue(actions["cash.transaction_update"]["source"])

    def test_cash_update_delete_and_bank_visibility_reuse_real_actions(self):
        from cash.models import BankPayment, CashTransaction

        transaction = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount="10", reason="Старое", created_by=self.admin)
        updated = execute_tool("cash.transaction_update", self.admin, {"transaction_id": transaction.pk, "operation_date": "2026-09-02", "account": "card", "direction": "income", "amount": "11", "reason": "Новое"}, confirmed=True)
        self.assertEqual(updated["transaction"]["reason"], "Новое")
        self.assertTrue(execute_tool("cash.transaction_delete", self.admin, {"transaction_id": transaction.pk}, confirmed=True)["deleted"])
        payment = BankPayment.objects.create(external_id="visibility", amount="10", operation_date=date(2026, 9, 1))
        self.assertTrue(execute_tool("cash.bank_payment_visibility_toggle", self.admin, {"bank_payment_id": payment.pk}, confirmed=True)["hidden_from_managers"])

    def test_general_calculator_uses_existing_calculation_service(self):
        from calculator.models import PriceItem

        item = PriceItem.objects.create(category="paper", name="Бумага", unit_name="лист", unit_price="2")
        result = execute_tool("calculator.sheet_calculate", self.admin, {"product_quantity": 100, "work_hours": "1", "lines": [{"item_id": item.pk, "category": "paper", "quantity": "100"}]})
        self.assertIn("standard", result["summary"])
