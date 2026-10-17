import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .assistant_conversations import create_conversation


class AssistantFunctionCallingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("agent", "agent@example.test", "password")
        self.conversation = create_conversation(self.user)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_read_result_returns_through_matching_tool_call(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_read", "type": "function", "function": {"name": "read", "arguments": json.dumps({"plan": {"entity": "cash.CashTransaction", "fields": ["amount"], "limit": 1}})}}]},
            {"role": "assistant", "content": "Последняя операция найдена."},
        ]
        with patch("tenders.assistant_agent.execute_tool", return_value={"rows": [{"id": 1, "amount": "10"}]}) as execute:
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи последнюю операцию")
        self.assertEqual(response["text"], "Последняя операция найдена.")
        self.assertEqual(execute.call_args.args[0], "read")
        messages = gateway.call_args_list[1].args[0]
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertEqual(messages[-2]["tool_call_id"], "call_read")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_invalid_arguments_are_returned_to_model_as_tool_error(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "bad", "type": "function", "function": {"name": "read", "arguments": "[]"}}]},
            {"role": "assistant", "content": "Уточните, пожалуйста, нужный показатель."},
        ]
        from .assistant_agent import reply
        response = reply(self.conversation, self.user, "Покажи данные")
        self.assertIn("Уточните", response["text"])
        self.assertIn('"ok": false', gateway.call_args_list[1].args[0][-2]["content"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_action_waits_for_confirmation_and_continues_once(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "content": "Операция подготовлена.", "tool_calls": [{"id": "confirm", "type": "function", "function": {"name": "cash_transaction_create", "arguments": json.dumps({"operation_date": "2026-01-01", "account": "cash", "direction": "income", "amount": 1, "reason": "Тест"})}}]},
            {"role": "assistant", "content": "Операция выполнена."},
        ]
        from .assistant_agent import continue_after_confirmation, reply
        response = reply(self.conversation, self.user, "Добавь операцию")
        self.assertEqual(response["kind"], "confirmation_required")
        self.assertEqual(gateway.call_count, 1)
        response = continue_after_confirmation(self.conversation, self.user, "cash.transaction_create", {"id": 1})
        self.assertEqual(response["text"], "Операция выполнена.")
        self.assertEqual(gateway.call_args_list[1].args[0][-2]["tool_call_id"], "confirm")
