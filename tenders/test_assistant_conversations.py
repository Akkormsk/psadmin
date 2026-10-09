import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import OwnerFeedbackEvent, OwnerInteraction


class AssistantConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-owner", "owner@example.test", "password")
        self.client.force_login(self.user)

    def test_new_conversation_is_persistent_and_lists_real_capabilities(self):
        created = self.client.post(reverse("assistant_conversation_new"), {"title": "Создание контрагента — Пошив", "page": "/tenders/tender/42/", "label": "Расчёт №42", "tender_id": "42"}, secure=True)
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["conversation"]["id"]
        self.assertEqual(created.json()["conversation"]["business_context"]["label"], "Расчёт №42")

        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Что ты умеешь?"}),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(reply.status_code, 200)
        messages = reply.json()["messages"]
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["kind"], "capabilities")
        self.assertTrue(any(item["id"] == "provider.calculate" for item in messages[-1]["data"]["tools"]))

        interaction = OwnerInteraction.objects.get(pk=conversation_id)
        self.assertEqual(interaction.context["kind"], "assistant_conversation")
        self.assertEqual(OwnerFeedbackEvent.objects.filter(interaction=interaction).count(), 2)
        listed = self.client.get(reverse("assistant_conversations"), secure=True)
        self.assertEqual(listed.json()["conversations"][0]["id"], conversation_id)

    def test_unknown_request_is_not_presented_as_a_supported_action(self):
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Проверка"}, secure=True).json()["conversation"]["id"]
        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Отправь email подрядчику"}),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["messages"][-1]["kind"], "unsupported")

    def test_global_drawer_has_context_chips_and_real_suggestion_actions(self):
        response = self.client.get(reverse("tender_home"), secure=True)
        self.assertContains(response, 'assistant-context-chips')
        self.assertContains(response, 'data-assistant-suggestion=', count=2)
        self.assertContains(response, 'event.key === "Enter" && !event.shiftKey')
