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
        self.assertTrue(any(item["id"] == "cash.transaction_create" for item in messages[-1]["data"]["result"]["tools"]))

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
    def test_fake_ai_receives_read_metadata_and_executes_a_generic_plan(self, request, _preflight):
        from tender_selection.models import Tender

        tender = Tender.objects.create(purchase_number="CHAT-READ", title="Каталог", collecting_finished_at="2026-09-30T10:00:00Z", outcome_status=Tender.OUTCOME_LOST)

        def fake_gateway(prompt):
            payload = json.loads(prompt)
            if "tool_result" in payload:
                self.assertEqual(payload["tool_result"]["records"][0]["id"], tender.pk)
                self.assertEqual(payload["tool_result"]["records"][0]["collecting_finished_at"], "2026-09-30T10:00:00+00:00")
                return {"reply": "Найден тендер CHAT-READ."}
            self.assertIn("cash", {goal["id"] for goal in payload["workflows"]})
            schema = next(item for item in payload["read_catalog"] if item["id"] == "tender_selection.Tender")
            self.assertIn("outcome_status", {field["name"] for field in schema["fields"]})
            return {"reply": "Проверяю тендер.", "action": {"id": "read", "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "outcome_status", "collecting_finished_at"], "filters": [{"field": "id", "op": "eq", "value": tender.pk}]}}}}

        request.side_effect = fake_gateway
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "READ"}, secure=True).json()["conversation"]["id"]
        reply = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Покажи тендер"}), content_type="application/json", secure=True)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["messages"][-1]["kind"], "tool_result")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_confirmed_action_executes_once_from_conversation_event(self, request, _preflight):
        request.return_value = {"reply": "Подтвердите расход.", "action": {"id": "cash.transaction_create", "arguments": {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Доставка"}}}
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Расход"}, secure=True).json()["conversation"]["id"]
        message = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.assertEqual(message["kind"], "confirmation_required")
        first = self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, message["id"]]), {"accept": "yes"}, secure=True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, message["id"]]), {"accept": "yes"}, secure=True).status_code, 409)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_old_confirmation_is_invalid_after_newer_conversation_message(self, request, _preflight):
        from cash.models import CashTransaction

        request.side_effect = [
            {"reply": "Подтвердите расход.", "action": {"id": "cash.transaction_create", "arguments": {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Устаревший расход"}}},
            {"reply": "Проверим заново.", "action": None},
        ]
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Устаревшее подтверждение"}, secure=True).json()["conversation"]["id"]
        endpoint = reverse("assistant_conversation_message", args=[conversation_id])
        first = self.client.post(endpoint, data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.assertEqual(first["kind"], "confirmation_required")
        self.client.post(endpoint, data=json.dumps({"message": "Погоди, я хочу изменить условия"}), content_type="application/json", secure=True)
        confirmation = self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, first["id"]]), {"accept": "yes"}, secure=True)
        self.assertEqual(confirmation.status_code, 409)
        self.assertFalse(CashTransaction.objects.filter(reason="Устаревший расход").exists())

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_cancelled_action_is_consumed_without_writing_data(self, request, _preflight):
        from cash.models import CashTransaction

        request.return_value = {"reply": "Подтвердите расход.", "action": {"id": "cash.transaction_create", "arguments": {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Отмена"}}}
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Расход"}, secure=True).json()["conversation"]["id"]
        message = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True).json()["messages"][-1]
        cancelled = self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, message["id"]]), {"accept": "no"}, secure=True)
        self.assertEqual(cancelled.status_code, 200)
        self.assertFalse(CashTransaction.objects.filter(reason="Отмена").exists())
        self.assertEqual(self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, message["id"]]), {"accept": "yes"}, secure=True).status_code, 409)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_missing_parameters_are_kept_in_same_conversation(self, request, _preflight):
        request.side_effect = [
            {"reply": "Какова сумма расхода?", "action": {"id": "cash.transaction_create", "arguments": {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "reason": "Доставка"}}},
            {"reply": "Подтвердите расход.", "action": {"id": "cash.transaction_create", "arguments": {"amount": "15000"}}},
        ]
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Расход"}, secure=True).json()["conversation"]["id"]
        first = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Добавь расход на доставку"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.assertEqual(first["kind"], "text")
        self.assertIn("amount", first["data"]["missing"])
        second = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "15 000"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.assertEqual(second["kind"], "confirmation_required")
        self.assertEqual(second["data"]["arguments"]["reason"], "Доставка")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_pending_macro_task_can_be_explicitly_cancelled(self, request, _preflight):
        request.side_effect = [
            {"reply": "Укажите сумму.", "action": {"id": "cash.transaction_create", "arguments": {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "reason": "Доставка"}}},
            {"reply": "Задача отменена.", "action": None, "task_status": "cancel"},
        ]
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Отмена задачи"}, secure=True).json()["conversation"]["id"]
        endpoint = reverse("assistant_conversation_message", args=[conversation_id])
        self.client.post(endpoint, data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True)
        response = self.client.post(endpoint, data=json.dumps({"message": "Отмена, не добавляй расход"}), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pending_action", OwnerInteraction.objects.get(pk=conversation_id).context)

    def test_global_drawer_has_context_chips_and_real_suggestion_actions(self):
        response = self.client.get(reverse("tender_home"), secure=True)
        self.assertContains(response, 'assistant-context-summary')
        self.assertContains(response, 'data-assistant-suggestion=', count=2)
        self.assertContains(response, 'event.key === "Enter" && !event.shiftKey')
        self.assertContains(response, 'assistant-tool-result')
        self.assertContains(response, 'assistant-message--pending')
        self.assertNotContains(response, 'data-sewing-upload')
        self.assertNotContains(response, 'data-sewing-confirm')

    def test_legacy_assistant_url_returns_to_global_drawer_host(self):
        response = self.client.get(reverse("assistant_console"), secure=True)
        self.assertRedirects(response, reverse("tender_home"), fetch_redirect_response=False)

    def test_clear_history_archives_conversations_but_keeps_audit_events(self):
        conversation_id = self.client.post(reverse("assistant_conversation_new"), {"title": "Тестовая беседа"}, secure=True).json()["conversation"]["id"]
        OwnerFeedbackEvent.objects.create(interaction_id=conversation_id, actor=self.user, raw_text="Сообщение", payload={"role": "user"})

        cleared = self.client.post(reverse("assistant_conversations_clear"), secure=True)

        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["archived"], 1)
        interaction = OwnerInteraction.objects.get(pk=conversation_id)
        self.assertEqual(interaction.status, "archived")
        self.assertEqual(interaction.feedback_events.count(), 1)
        self.assertEqual(self.client.get(reverse("assistant_conversations"), secure=True).json()["conversations"], [])
