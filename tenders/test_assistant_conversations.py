import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import OwnerFeedbackEvent, OwnerInteraction, ProcessDefinition


class AssistantConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-owner", "owner@example.test", "password")
        self.client.force_login(self.user)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_new_conversation_is_persistent_and_lists_real_capabilities(self, request, _preflight):
        request.side_effect = [
            {"reply": "Покажу доступные возможности.", "action": {"id": "assistant.capabilities", "arguments": {}}},
            {"reply": "Вот доступные вам функции."},
        ]
        created = self.client.post(reverse("assistant_conversation_new"), {"title": "Создание контрагента — Пошив", "page": "/tenders/tender/42/", "label": "Расчёт №42", "tender_id": "42"}, secure=True)
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["conversation"]["id"]
        self.assertEqual(created.json()["conversation"]["business_context"]["label"], "Расчёт №42")

        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Что ты умеешь?", "context": {"page": "/tenders/tender/42/", "label": "Позиция 6", "tender_id": "42", "line_id": "6"}}),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(reply.status_code, 200)
        messages = reply.json()["messages"]
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["kind"], "tool_result")
        self.assertTrue(any(item["id"] == "provider.calculate" for item in messages[-1]["data"]["result"]["tools"]))

        interaction = OwnerInteraction.objects.get(pk=conversation_id)
        self.assertEqual(interaction.context["kind"], "assistant_conversation")
        self.assertEqual(OwnerFeedbackEvent.objects.filter(interaction=interaction).count(), 2)
        self.assertEqual(interaction.feedback_events.order_by("created_at", "pk").first().payload["context"]["line_id"], "6")
        listed = self.client.get(reverse("assistant_conversations"), secure=True)
        self.assertEqual(listed.json()["conversations"][0]["id"], conversation_id)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request", return_value={"reply": "Отправка email пока не подключена.", "action": None})
    def test_unknown_request_is_not_presented_as_a_supported_action(self, _request, _preflight):
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Проверка"}, secure=True).json()["conversation"]["id"]
        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Отправь email подрядчику"}),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["messages"][-1]["kind"], "text")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_model_can_run_stage_registry_without_keyword_routing(self, request, _preflight):
        stage = ProcessDefinition.objects.create(name="Швейный этап", role=ProcessDefinition.ROLE_PRODUCTION)
        request.side_effect = [
            {"reply": "Сейчас проверю реестр.", "action": {"id": "process.list", "arguments": {}}},
            {"reply": "Доступен этап: Швейный этап."},
        ]
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Этапы"}, secure=True).json()["conversation"]["id"]

        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Какие у тебя есть этапы?"}),
            content_type="application/json",
            secure=True,
        )

        message = reply.json()["messages"][-1]
        self.assertEqual(message["text"], "Доступен этап: Швейный этап.")
        self.assertIn({"id": stage.pk, "name": stage.name}, message["data"]["result"]["stages"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_model_opens_registered_provider_form_without_creating_a_draft(self, _request, _preflight):
        stage = ProcessDefinition.objects.create(name="Швейный этап", role=ProcessDefinition.ROLE_PRODUCTION)
        _request.return_value = {"reply": "Проверьте данные и прикрепите XLS.", "action": {"id": "provider.create_draft", "arguments": {"name": "Атекс", "stage_id": stage.pk}}}
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Контрагент"}, secure=True).json()["conversation"]["id"]

        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Создай контрагента по пошиву"}),
            content_type="application/json",
            secure=True,
        )

        message = reply.json()["messages"][-1]
        self.assertEqual(message["kind"], "provider_upload")
        self.assertIn({"id": stage.pk, "name": stage.name}, message["data"]["stages"])
        self.assertEqual(message["data"]["name"], "Атекс")
        self.assertEqual(message["data"]["stage_id"], stage.pk)

    def test_global_drawer_has_context_chips_and_real_suggestion_actions(self):
        response = self.client.get(reverse("tender_home"), secure=True)
        self.assertContains(response, 'assistant-context-summary')
        self.assertContains(response, 'data-assistant-suggestion=', count=2)
        self.assertContains(response, 'event.key === "Enter" && !event.shiftKey')
        self.assertContains(response, 'assistant-tool-result')
        self.assertContains(response, 'assistant-message--pending')

    def test_legacy_assistant_url_returns_to_global_drawer_host(self):
        response = self.client.get(reverse("assistant_console"), secure=True)
        self.assertRedirects(response, reverse("tender_home"))
