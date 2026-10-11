from django.contrib.auth import get_user_model
from django.test import TestCase

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

        result = execute_tool("data.orders", self.user, {"query": "заказ"})

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["records"], [{"id": mine.pk, "number": "O-1", "name": "Печать", "status": "Черновик", "order_id": mine.order_id}])

    def test_data_agent_admin_can_see_all_calculations_and_sums_known_amounts(self):
        first = TenderEstimate.objects.create(owner=self.user, tender_number="T-1", name="Первый", summary_snapshot={"our_price": "120.50"})
        second = TenderEstimate.objects.create(owner=self.admin, tender_number="T-2", name="Второй", summary_snapshot={"our_price": "79.50"})

        result = execute_tool("data.tenders", self.admin, {})

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["totals"]["our_price"], "200.00")
        self.assertEqual({record["id"] for record in result["records"]}, {first.pk, second.pk})

    def test_data_agent_rejects_unknown_filters_instead_of_guessing(self):
        with self.assertRaisesRegex(Exception, "Некорректные параметры"):
            execute_tool("data.tenders", self.admin, {"sql": "SELECT *"})
