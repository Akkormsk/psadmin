import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .assistant_conversations import create_conversation, respond


class AssistantFunctionCallingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("agent", "agent@example.test", "password")
        self.conversation = create_conversation(self.user)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_read_result_returns_through_matching_tool_call(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "describe", "type": "function", "function": {"name": "schema_describe", "arguments": json.dumps({"entity": "cash.CashTransaction"})}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_read", "type": "function", "function": {"name": "read", "arguments": json.dumps({"plan": {"entity": "cash.CashTransaction", "fields": ["amount"], "limit": 1}})}}]},
            {"role": "assistant", "content": "Последняя операция найдена."},
        ]
        with patch("tenders.assistant_agent.execute_tool", side_effect=[
            {"entity": "cash.CashTransaction", "fields": []},
            {"entity": "cash.CashTransaction", "query": {}, "total_count": 1,
             "returned_count": 1, "aggregates": {}, "records": [{"id": 1, "amount": "10"}], "sources": []},
        ]) as execute:
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи последнюю операцию")
        self.assertIn("10", response["text"])
        self.assertEqual(execute.call_args_list[1].args[0], "read")
        messages = gateway.call_args_list[2].args[0]
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
            {"role": "assistant", "content": None, "tool_calls": [{"id": "action-schema", "type": "function", "function": {
                "name": "action_describe", "arguments": json.dumps({"action": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "content": "Операция подготовлена.", "tool_calls": [{"id": "confirm", "type": "function", "function": {"name": "cash_transaction_create", "arguments": json.dumps({"operation_date": "2026-01-01", "account": "cash", "direction": "income", "amount": 1, "reason": "Тест"})}}]},
            {"role": "assistant", "content": "Операция выполнена."},
        ]
        from .assistant_agent import continue_after_confirmation, reply
        with patch("tenders.assistant_agent.execute_tool", side_effect=[{"id": "cash.transaction_create", "input_schema": {} }]):
            response = reply(self.conversation, self.user, "Добавь операцию с основанием Тест")
        self.assertEqual(response["kind"], "confirmation_required")
        self.assertEqual(gateway.call_count, 2)
        response = continue_after_confirmation(self.conversation, self.user, "cash.transaction_create", {"id": 1})
        self.assertEqual(response["text"], "Операция выполнена.")
        self.assertEqual(gateway.call_args_list[2].args[0][-2]["tool_call_id"], "confirm")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_action_rejects_free_text_not_present_in_a_user_message(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "action-schema", "type": "function", "function": {
                "name": "action_describe", "arguments": json.dumps({"action": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "write", "type": "function", "function": {
                "name": "cash_transaction_create", "arguments": json.dumps({"operation_date": "2026-01-01", "account": "cash", "direction": "income", "amount": 1, "reason": "Придуманное основание"}),
            }}]},
            {"role": "assistant", "content": "Уточните основание операции."},
        ]
        with patch("tenders.assistant_agent.execute_tool", return_value={"id": "cash.transaction_create", "input_schema": {}}):
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Добавь операцию")

        self.assertEqual(response["kind"], "tool_result")
        self.assertIn("Уточните", response["text"])
        self.assertIn("сообщения пользователя", gateway.call_args_list[2].args[0][-2]["content"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_follow_up_uses_the_existing_conversation_state_once(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "content": "За какой период?"},
            {"role": "assistant", "content": "За сентябрь найдено 3 заказа."},
        ]
        first = respond(self.conversation, self.user, "Покажи заказы Иванова")
        second = respond(self.conversation, self.user, "За сентябрь")

        self.assertEqual(first[-1]["text"], "За какой период?")
        self.assertEqual(second[-1]["text"], "За сентябрь найдено 3 заказа.")
        messages = gateway.call_args_list[1].args[0]
        self.assertEqual([item for item in messages if item.get("role") == "user"], [
            {"role": "user", "content": "Покажи заказы Иванова"},
            {"role": "user", "content": "За сентябрь"},
        ])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_persisted_user_message_is_not_added_to_a_new_state_twice(self, gateway, _preflight):
        gateway.return_value = {"role": "assistant", "content": "Здравствуйте."}

        respond(self.conversation, self.user, "Привет")

        messages = gateway.call_args.args[0]
        self.assertEqual([item for item in messages if item.get("role") == "user"], [
            {"role": "user", "content": "Привет"},
        ])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_completed_read_is_replaced_with_compact_memory_before_the_next_turn(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "schema-1", "type": "function", "function": {
                "name": "schema_describe", "arguments": json.dumps({"entity": "cash.CashTransaction"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read-1", "type": "function", "function": {
                "name": "read", "arguments": json.dumps({"plan": {"entity": "cash.CashTransaction", "fields": ["reason"], "limit": 1}}),
            }}]},
            {"role": "assistant", "content": "Одна операция найдена."},
            {"role": "assistant", "tool_calls": [{"id": "read-2", "type": "function", "function": {
                "name": "read", "arguments": json.dumps({"plan": {"entity": "cash.CashTransaction", "fields": ["reason"], "limit": 1}}),
            }}]},
            {"role": "assistant", "content": "Это была расходная операция."},
        ]
        result = {"entity": "cash.CashTransaction", "query": {"filters": [], "view": None, "search": None, "order_by": ["-id"]}, "total_count": 1,
                  "returned_count": 1, "aggregates": {}, "records": [{"id": 1, "reason": "x"}],
                  "sources": [{"entity": "cash.CashTransaction", "id": 1}], "source_scope": "all matching records"}
        with patch("tenders.assistant_agent.execute_tool", side_effect=[{"entity": "cash.CashTransaction", "fields": []}, result]) as execute:
            from .assistant_agent import reply
            reply(self.conversation, self.user, "Покажи операцию")
            self.assertNotIn("agent_run", self.conversation.context)
            self.assertEqual(self.conversation.context["agent_memory"]["last_read"]["records"], [{"id": 1, "reason": "x"}])
            self.assertLessEqual(len(json.dumps(self.conversation.context["agent_memory"])), 2_000)
            reply(self.conversation, self.user, "А какая это операция?")
            self.assertEqual(execute.call_count, 2)

        messages = gateway.call_args_list[3].args[0]
        tools = [item for item in messages if item.get("role") == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tools], ["read-2"])
