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
        records = execute_tool("data.query", self.admin, {"plan": {"entity": "tenders.Counterparty", "search": "Черновой", "fields": ["id", "name"]}})["records"]
        self.assertEqual(records[0]["name"], "Черновой пошив")

    def test_data_agent_lists_only_users_own_calculations_and_returns_evidence(self):
        mine = OrderEstimate.objects.create(owner=self.user, order=Order.objects.create(name="Мой заказ"), order_number="O-1", name="Печать")
        other = get_user_model().objects.create_user("other-user")
        OrderEstimate.objects.create(owner=other, order=Order.objects.create(name="Чужой заказ"), order_number="O-2", name="Чужой")

        with self.assertRaisesRegex(Exception, "права администратора"):
            execute_tool("data.query", self.user, {"plan": {"entity": "tenders.OrderEstimate", "search": "Печать", "fields": ["id", "order_number", "name"]}})

    def test_data_agent_admin_can_see_all_calculations_and_sums_known_amounts(self):
        first = TenderEstimate.objects.create(owner=self.user, tender_number="T-1", name="Первый", summary_snapshot={"our_price": "120.50"})
        second = TenderEstimate.objects.create(owner=self.admin, tender_number="T-2", name="Второй", summary_snapshot={"our_price": "79.50"})

        result = execute_tool("data.query", self.admin, {"plan": {"entity": "tenders.TenderEstimate", "fields": ["id", "tender_number", "name"]}})

        self.assertEqual(result["total_count"], 2)
        self.assertEqual({record["id"] for record in result["records"]}, {first.pk, second.pk})

    def test_data_agent_rejects_unknown_filters_instead_of_guessing(self):
        with self.assertRaisesRegex(Exception, "Некорректный план"):
            execute_tool("data.query", self.admin, {"plan": {"entity": "tenders.TenderEstimate", "sql": "SELECT *"}})

    def test_universal_query_aggregates_all_visible_rows_not_only_page(self):
        from cash.models import CashTransaction
        from datetime import date

        for amount in ("10.00", "20.00", "30.00"):
            CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount=amount, reason="Бумага", created_by=self.user)

        result = execute_tool("data.query", self.admin, {"plan": {"entity": "cash.CashTransaction", "fields": ["id", "amount"], "page_size": 1, "aggregates": [{"op": "sum", "field": "amount"}]}})

        self.assertEqual(result["total_count"], 3)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(str(result["aggregates"]["sum_amount"]), "60")
        self.assertEqual(len(result["evidence"]), 1)

    def test_universal_query_groups_cash_operations_by_direction(self):
        from cash.models import CashTransaction
        from datetime import date

        CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="income", amount="100", reason="Оплата", created_by=self.user)
        CashTransaction.objects.create(operation_date=date(2026, 9, 2), account="cash", direction="expense", amount="30", reason="Бумага", created_by=self.user)
        result = execute_tool("data.query", self.admin, {"plan": {"entity": "cash.CashTransaction", "group_by": ["direction"], "aggregates": [{"op": "sum", "field": "amount"}]}})
        self.assertEqual(result["records"], [{"direction": "expense", "sum_amount": "30"}, {"direction": "income", "sum_amount": "100"}])

    def test_universal_query_cannot_follow_other_users_order(self):
        mine = Order.objects.create(name="Мой")
        OrderEstimate.objects.create(owner=self.user, order=mine, order_number="mine", name="Мой")
        other = get_user_model().objects.create_user("agent-other")
        hidden = Order.objects.create(name="Чужой")
        OrderEstimate.objects.create(owner=other, order=hidden, order_number="hidden", name="Чужой")

        with self.assertRaisesRegex(Exception, "права администратора"):
            execute_tool("data.query", self.user, {"plan": {"entity": "tenders.Order", "fields": ["id", "name"]}})

    def test_general_calculator_uses_price_catalog_not_model_supplied_prices(self):
        from calculator.models import CalculatorSettings, PriceItem

        CalculatorSettings.objects.create()
        paper = PriceItem.objects.create(category=PriceItem.CATEGORY_PAPER, name="Бумага", unit_price="2.00")
        result = execute_tool("calculator.quote", self.user, {"quantity": 10, "work_hours": 0, "items": [{"item_id": paper.pk, "quantity": 10}]})

        self.assertEqual(result["result"]["material_cost"], "20.0000")
        self.assertEqual(result["evidence"], [{"entity": "calculator.PriceItem", "id": paper.pk}])

    def test_committed_capability_map_matches_live_models_and_operations(self):
        self.assertEqual(MAP_PATH.read_text(encoding="utf-8"), serialized_map())

    def test_generic_crud_preview_requires_confirmation_and_is_idempotent(self):
        from .agent_crud import confirm, preview
        from .assistant_conversations import create_conversation

        conversation = create_conversation(self.admin, "CRUD")
        item = preview(conversation, self.admin, {"action": "create", "model": "tenders.ProcessDefinition", "object_id": None, "values": {"name": "Новый этап", "role": ProcessDefinition.ROLE_PRODUCTION}})
        self.assertFalse(ProcessDefinition.objects.filter(name="Новый этап").exists())
        result = confirm(conversation, self.admin, item["operation_id"], item["token"], True)
        self.assertEqual(result["status"], "executed")
        with self.assertRaisesRegex(Exception, "уже использовано"):
            confirm(conversation, self.admin, item["operation_id"], item["token"], True)

    def test_gap_export_uses_database_as_source_of_truth(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from .agent_gaps import export_gaps, record_gap

        record_gap("Нет маршрута доставки", "missing_service", "В реестре нет сервиса логистики", "logistics.quote", "Зарегистрировать сервис логистики")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent_gaps.jsonl"
            export_gaps(path)
            self.assertIn('"missing_capability": "logistics.quote"', path.read_text(encoding="utf-8"))
