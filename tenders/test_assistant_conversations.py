import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AssistantConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-owner", "owner@example.test", "password")
        self.client.force_login(self.user)

    def _conversation(self):
        return self.client.post(reverse("assistant_conversation_new"), {"title": "Тест"}, secure=True).json()["conversation"]["id"]

    def _send(self, conversation_id, message):
        return self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": message}), content_type="application/json", secure=True)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_ordinary_conversation_uses_no_business_tool(self, request, _preflight):
        request.return_value = {"reply": "Привет! Чем могу помочь?", "status": "final", "basis": "user_supplied"}
        response = self._send(self._conversation(), "Привет")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][-1]["text"], "Привет! Чем могу помочь?")
        prompt = json.loads(request.call_args.args[0])
        self.assertNotIn("execution_plan", prompt)
        self.assertEqual([event["stage"] for event in response.json()["messages"][-1]["data"]["debug"]], ["decision.request", "decision.response"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_reads_last_cash_transaction_after_opening_contract(self, request, _preflight):
        from cash.models import CashTransaction

        CashTransaction.objects.create(created_by=self.user, operation_date=date(2026, 10, 1), account="cash", direction="income", amount="100", reason="Первая")
        latest = CashTransaction.objects.create(created_by=self.user, operation_date=date(2026, 10, 2), account="card", direction="expense", amount="250", reason="Последняя")
        request.side_effect = [
            {"reply": "Открываю описание операций кассы.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "assistant.navigate", "purpose": "Получить поля операции кассы", "arguments": {"path": ["read", "cash", "CashTransaction"]}}},
            {"reply": "Читаю последнюю операцию.", "status": "next", "basis": "verified_steps", "next_step": {"tool_id": "read", "purpose": "Получить последнюю операцию", "arguments": {"plan": {"entity": "cash.CashTransaction", "fields": ["operation_date", "account", "direction", "amount", "reason"], "filters": [], "order_by": "-created_at", "limit": 1}}}},
            {"reply": "Последняя операция: расход 250 — Последняя.", "status": "final", "basis": "verified_steps"},
        ]

        response = self._send(self._conversation(), "Покажи последнюю операцию по кассе")

        self.assertEqual(response.status_code, 200)
        message = response.json()["messages"][-1]
        self.assertIn("Последняя операция", message["text"])
        self.assertEqual([step["tool_id"] for step in message["data"]["steps"]], ["assistant.navigate", "read"])
        self.assertEqual(message["data"]["steps"][-1]["result"]["records"][0]["id"], latest.pk)
        prompt = json.loads(request.call_args_list[1].args[0])
        self.assertIn("cash.CashTransaction", {".".join(node["path"][1:]) for node in prompt["opened_capability_nodes"] if "path" in node})

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_rejects_read_before_contract_and_allows_generic_repair(self, request, _preflight):
        request.side_effect = [
            {"reply": "Читаю операции.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "read", "purpose": "Прочитать операции", "arguments": {"plan": {"entity": "cash.CashTransaction", "fields": ["amount"]}}}},
            {"reply": "Сначала открываю описание.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "assistant.navigate", "purpose": "Открыть операции кассы", "arguments": {"path": ["read", "cash", "CashTransaction"]}}},
            {"reply": "Готово.", "status": "final", "basis": "unavailable"},
        ]
        response = self._send(self._conversation(), "Покажи операции кассы")
        self.assertEqual(response.status_code, 200)
        correction = json.loads(request.call_args_list[1].args[0])
        self.assertIn("открыть описание", correction["rejected_step"]["error"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_needs_input_requires_an_opened_action_contract(self, request, _preflight):
        request.side_effect = [
            {"reply": "Открываю создание операции.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "assistant.navigate", "purpose": "Открыть создание операции", "arguments": {"path": ["action", "cash", "cash.transaction_create"]}}},
            {"reply": "Укажите сумму расхода.", "status": "needs_input", "basis": "unavailable", "missing_inputs": ["сумма"], "missing_for": "cash.transaction_create"},
        ]
        response = self._send(self._conversation(), "Добавь расход")
        self.assertEqual(response.status_code, 200)
        message = response.json()["messages"][-1]
        self.assertTrue(message["data"]["task_pending"])
        self.assertEqual(message["text"], "Укажите сумму расхода.")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_action_stops_for_confirmation_after_contract(self, request, _preflight):
        from cash.models import CashTransaction

        request.side_effect = [
            {"reply": "Открываю создание операции.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "assistant.navigate", "purpose": "Открыть создание операции", "arguments": {"path": ["action", "cash", "cash.transaction_create"]}}},
            {"reply": "Подготовил расход для подтверждения.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "cash.transaction_create", "purpose": "Создать расход", "arguments": {"operation_date": "2026-10-15", "account": "cash", "direction": "expense", "amount": "1", "reason": "тест"}}},
        ]
        response = self._send(self._conversation(), "Добавь расход 1 рубль")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][-1]["kind"], "confirmation_required")
        self.assertFalse(CashTransaction.objects.filter(reason="тест").exists())

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_continues_pending_task_without_losing_opened_contract(self, request, _preflight):
        request.side_effect = [
            {"reply": "Открываю создание операции.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "assistant.navigate", "purpose": "Открыть создание операции", "arguments": {"path": ["action", "cash", "cash.transaction_create"]}}},
            {"reply": "Укажите сумму.", "status": "needs_input", "basis": "unavailable", "missing_inputs": ["сумма"], "missing_for": "cash.transaction_create"},
            {"reply": "Подготовил операцию.", "status": "next", "basis": "user_supplied", "next_step": {"tool_id": "cash.transaction_create", "purpose": "Создать расход", "arguments": {"operation_date": "2026-10-15", "account": "cash", "direction": "expense", "amount": "100", "reason": "тест"}}},
        ]
        conversation_id = self._conversation()
        self.assertEqual(self._send(conversation_id, "Добавь расход").status_code, 200)
        second = self._send(conversation_id, "100 рублей")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["messages"][-1]["kind"], "confirmation_required")
        continuation = json.loads(request.call_args_list[2].args[0])
        self.assertTrue(continuation["opened_capability_nodes"])
        self.assertEqual(continuation["original_task"], "Добавь расход")
