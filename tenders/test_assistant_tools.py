from django.contrib.auth import get_user_model
from django.test import TestCase

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
