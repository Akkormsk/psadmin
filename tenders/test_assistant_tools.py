from django.contrib.auth import get_user_model
from django.test import TestCase

from .capability_map import MAP_PATH, serialized_map

from .assistant_tools import AssistantToolConfirmationRequired, available_tools, execute_tool
from .models import Order, OrderEstimate, ProcessDefinition, TenderEstimate


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

    def test_data_agent_lists_only_users_own_calculations_and_returns_evidence(self):
        mine = OrderEstimate.objects.create(owner=self.user, order=Order.objects.create(name="Мой заказ"), order_number="O-1", name="Печать")
        other = get_user_model().objects.create_user("other-user")
        OrderEstimate.objects.create(owner=other, order=Order.objects.create(name="Чужой заказ"), order_number="O-2", name="Чужой")

        result = execute_tool("data.query", self.user, {"plan": {"entity": "order_estimate", "search": "Печать", "fields": ["id", "number", "name"]}})

        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["records"], [{"id": mine.pk, "number": "O-1", "name": "Печать"}])

    def test_data_agent_admin_can_see_all_calculations_and_sums_known_amounts(self):
        first = TenderEstimate.objects.create(owner=self.user, tender_number="T-1", name="Первый", summary_snapshot={"our_price": "120.50"})
        second = TenderEstimate.objects.create(owner=self.admin, tender_number="T-2", name="Второй", summary_snapshot={"our_price": "79.50"})

        result = execute_tool("data.query", self.admin, {"plan": {"entity": "tender_estimate", "fields": ["id", "number", "name"]}})

        self.assertEqual(result["total_count"], 2)
        self.assertEqual({record["id"] for record in result["records"]}, {first.pk, second.pk})

    def test_data_agent_rejects_unknown_filters_instead_of_guessing(self):
        with self.assertRaisesRegex(Exception, "Некорректный план"):
            execute_tool("data.query", self.admin, {"plan": {"entity": "tender_estimate", "sql": "SELECT *"}})

    def test_universal_query_aggregates_all_visible_rows_not_only_page(self):
        from cash.models import CashTransaction
        from datetime import date

        for amount in ("10.00", "20.00", "30.00"):
            CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount=amount, reason="Бумага", created_by=self.user)

        result = execute_tool("data.query", self.user, {"plan": {"entity": "cash_transaction", "fields": ["id", "amount"], "page_size": 1, "aggregates": [{"op": "sum", "field": "amount"}]}})

        self.assertEqual(result["total_count"], 3)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(str(result["aggregates"]["sum_amount"]), "60")
        self.assertEqual(len(result["evidence"]), 1)

    def test_universal_query_cannot_follow_other_users_order(self):
        mine = Order.objects.create(name="Мой")
        OrderEstimate.objects.create(owner=self.user, order=mine, order_number="mine", name="Мой")
        other = get_user_model().objects.create_user("agent-other")
        hidden = Order.objects.create(name="Чужой")
        OrderEstimate.objects.create(owner=other, order=hidden, order_number="hidden", name="Чужой")

        result = execute_tool("data.query", self.user, {"plan": {"entity": "order", "fields": ["id", "name"]}})

        self.assertEqual(result["records"], [{"id": mine.pk, "name": "Мой"}])

    def test_general_calculator_uses_price_catalog_not_model_supplied_prices(self):
        from calculator.models import CalculatorSettings, PriceItem

        CalculatorSettings.objects.create()
        paper = PriceItem.objects.create(category=PriceItem.CATEGORY_PAPER, name="Бумага", unit_price="2.00")
        result = execute_tool("calculator.quote", self.user, {"quantity": 10, "work_hours": 0, "items": [{"item_id": paper.pk, "quantity": 10}]})

        self.assertEqual(result["result"]["material_cost"], "20.0000")
        self.assertEqual(result["evidence"], [{"entity": "calculator_price_item", "id": paper.pk}])

    def test_committed_capability_map_matches_live_models_and_operations(self):
        self.assertEqual(MAP_PATH.read_text(encoding="utf-8"), serialized_map())
