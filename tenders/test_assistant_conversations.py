import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .assistant_conversations import add_assistant_message, add_debug_event, create_conversation, respond


class AssistantConversationUiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-ui", "assistant-ui@example.test", "password")
        self.other = get_user_model().objects.create_superuser("assistant-other", "assistant-other@example.test", "password")
        self.conversation = create_conversation(self.user)
        add_assistant_message(self.conversation, "Покажи операции")
        add_debug_event(self.conversation, "function_call.request", event_input={"trace": "x" * 20_000})

    def test_conversation_detail_excludes_debug_journal(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("assistant_conversation_detail", args=[self.conversation.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("debug_journal", response.json())
        self.assertNotIn("trace", response.content.decode())

    def test_debug_journal_has_a_separate_owner_only_endpoint(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("assistant_conversation_debug", args=[self.conversation.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["debug_journal"][0]["input"]["trace"], "x" * 20_000)
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse("assistant_conversation_debug", args=[self.conversation.pk])).status_code,
            404,
        )


class AssistantFunctionCallingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("agent", "agent@example.test", "password")
        self.conversation = create_conversation(self.user)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_read_result_returns_through_matching_tool_call(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": "assistant_plan", "arguments": json.dumps({"plan": "1. Найду последнюю операцию. 2. Покажу результат."})}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "area", "type": "function", "function": {"name": "assistant_select_area", "arguments": json.dumps({"area": "cash"})}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "resource", "type": "function", "function": {"name": "assistant_select_capability", "arguments": json.dumps({"capability": "read:cash.transactions"})}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_read", "type": "function", "function": {"name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount"], "limit": 1}})}}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Последняя операция: 10. Сумма: 10."}),
            }}]},
        ]
        with patch("tenders.assistant_agent.execute_tool", side_effect=[
            {"entity": "cash.CashTransaction", "query": {}, "total_count": 1,
             "returned_count": 1, "aggregates": {}, "records": [{"id": 1, "amount": "10"}], "sources": []},
        ]) as execute:
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи последнюю операцию")
        self.assertEqual(response["text"], "Последняя операция: 10. Сумма: 10.")
        self.assertEqual(gateway.call_count, 5)
        initial_prompt = gateway.call_args_list[0].args[0][0]["content"]
        self.assertIn("Capability map:", initial_prompt)
        self.assertIn("read:cash.transactions", initial_prompt)
        self.assertNotIn('"properties"', initial_prompt)
        working_prompt = gateway.call_args_list[1].args[0][0]["content"]
        self.assertNotIn("Capability map:", working_prompt)
        self.assertIn("Current plan:", working_prompt)
        self.assertLess(len(working_prompt.encode()), len(initial_prompt.encode()) // 10)
        self.assertEqual(
            {item["function"]["name"] for item in gateway.call_args_list[0].args[1]},
            {"assistant_plan", "assistant_finish"},
        )
        self.assertEqual(execute.call_args.args[0], "read")
        messages = gateway.call_args_list[4].args[0]
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertEqual(messages[-2]["tool_call_id"], "call_read")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_cash_resource_executes_the_real_universal_read(self, gateway, _preflight):
        from cash.models import CashTransaction

        CashTransaction.objects.create(
            operation_date=date(2026, 10, 1), account="card", direction="income",
            amount="100", reason="Оплата", created_by=self.user,
        )
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": "assistant_plan", "arguments": '{"plan":"1. Найду операции. 2. Покажу результат."}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {"name": "assistant_select_area", "arguments": '{"area":"cash"}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "resource", "type": "function", "function": {"name": "assistant_select_capability", "arguments": '{"capability":"read:cash.transactions"}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {"name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount", "reason"], "order_by": "-operation_date", "limit": 3}})}}]},
            {"role": "assistant", "content": "Найдена операция: 100, Оплата."},
        ]

        messages = respond(self.conversation, self.user, "В приложении Касса покажи три последние операции")
        response = messages[-1]

        self.assertEqual(response["text"], "Найдена операция: 100, Оплата.")
        self.assertEqual(response["data"]["steps"][-1]["result"]["entity"], "cash.CashTransaction")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_empty_final_read_text_falls_back_to_backend_presentation(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": "assistant_plan", "arguments": '{"plan":"Найду операцию."}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {"name": "assistant_select_area", "arguments": '{"area":"cash"}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "resource", "type": "function", "function": {"name": "assistant_select_capability", "arguments": '{"capability":"read:cash.transactions"}'}}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {"name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount"], "limit": 1}})}}]},
            {"role": "assistant", "content": ""},
        ]
        result = {"entity": "cash.CashTransaction", "query": {}, "total_count": 1,
                  "returned_count": 1, "aggregates": {}, "records": [{"id": 1, "amount": "10"}], "sources": []}
        with patch("tenders.assistant_agent.execute_tool", return_value=result):
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи операцию")

        self.assertIn("10", response["text"])
        self.assertNotEqual(response["text"], "")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_final_text_without_read_is_unchanged(self, gateway, _preflight):
        gateway.return_value = {"role": "assistant", "content": "За какой период показать данные?"}

        from .assistant_agent import reply
        response = reply(self.conversation, self.user, "Покажи данные")

        self.assertEqual(response, {"text": "За какой период показать данные?", "kind": "tool_result", "data": {"steps": []}})

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_finish_returns_a_clarifying_question_without_another_gateway_call(self, gateway, _preflight):
        gateway.return_value = {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
            "name": "assistant_finish", "arguments": json.dumps({"answer": "За какой период показать операции?"}),
        }}]}

        from .assistant_agent import reply
        response = reply(self.conversation, self.user, "Покажи операции")

        self.assertEqual(response, {"text": "За какой период показать операции?", "kind": "tool_result", "data": {"steps": []}})
        self.assertEqual(gateway.call_count, 1)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_user_correction_continues_the_saved_plan_and_can_replace_it(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan-1", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": "Найду заказы Иванова за указанный период."}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "question", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "За какой период показать заказы?"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "plan-2", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": "Найду заказы Иванова за сентябрь."}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "За сентябрь заказы найдены."}),
            }}]},
        ]

        first = respond(self.conversation, self.user, "Покажи заказы Иванова")
        second = respond(self.conversation, self.user, "За сентябрь")

        self.assertEqual(first[-1]["text"], "За какой период показать заказы?")
        self.assertEqual(second[-1]["text"], "За сентябрь заказы найдены.")
        continuation_messages = gateway.call_args_list[2].args[0]
        self.assertIn("task_plan", continuation_messages[1]["content"])
        self.assertEqual([item for item in continuation_messages if item.get("role") == "user"][-1]["content"], "За сентябрь")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_read_result_can_trigger_a_saved_replan_before_finish(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan-1", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": "1. Найду операции. 2. Покажу результат."}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "read:cash.transactions"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount"], "limit": 1}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "plan-2", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": "Операций нет; сообщу результат без суммы."}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "За выбранный период операций нет."}),
            }}]},
        ]
        result = {"entity": "cash.CashTransaction", "query": {}, "total_count": 0,
                  "returned_count": 0, "aggregates": {}, "records": [], "sources": []}
        with patch("tenders.assistant_agent.execute_tool", return_value=result):
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи операции")

        self.assertEqual(response["text"], "За выбранный период операций нет.")
        self.assertEqual(gateway.call_count, 6)
        self.assertNotIn("read_cash_transactions", [
            item["function"]["name"] for item in gateway.call_args_list[4].args[1]
        ])
        plans = [item for item in self.conversation.feedback_events.filter(scope="assistant_debug") if item.payload.get("stage") == "agent.plan"]
        self.assertEqual([item.payload["output"]["plan"] for item in plans], [
            "1. Найду операции. 2. Покажу результат.", "Операций нет; сообщу результат без суммы.",
        ])
        self.assertIn("Capability map:", gateway.call_args_list[0].args[0][0]["content"])
        self.assertNotIn("Capability map:", gateway.call_args_list[1].args[0][0]["content"])
        self.assertIn("Capability map:", gateway.call_args_list[5].args[0][0]["content"])
        requests = [item for item in self.conversation.feedback_events.filter(scope="assistant_debug") if item.payload.get("stage") == "function_call.request"]
        self.assertTrue(all(item.payload["input"]["metrics"]["tool_schema_bytes"] > 0 for item in requests))

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_invalid_arguments_are_returned_to_model_as_tool_error(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "bad", "type": "function", "function": {"name": "assistant_select_area", "arguments": "[]"}}]},
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
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": "assistant_plan", "arguments": '{"plan":"Подготовлю операцию."}'}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "content": "Операция подготовлена.", "tool_calls": [{"id": "confirm", "type": "function", "function": {"name": "cash_transaction_create", "arguments": json.dumps({"operation_date": "2026-01-01", "account": "cash", "direction": "income", "amount": 1, "reason": "Тест"})}}]},
            {"role": "assistant", "content": "Операция выполнена."},
        ]
        from .assistant_agent import continue_after_confirmation, reply
        with patch("tenders.assistant_agent.execute_tool"):
            response = reply(self.conversation, self.user, "Добавь операцию с основанием Тест")
        self.assertEqual(response["kind"], "confirmation_required")
        self.assertEqual(response["data"]["action"]["name"], "Создать операцию кассы")
        self.assertNotIn("input_schema", response["data"]["action"])
        self.assertEqual(response["data"]["action"]["arguments"], [
            {"label": "Дата операции", "value": "01.01.2026"},
            {"label": "Счёт", "value": "Наличные"},
            {"label": "Вид движения", "value": "Поступление"},
            {"label": "Сумма операции", "value": "1"},
            {"label": "Основание операции или номер заказа из сообщения пользователя", "value": "Тест"},
        ])
        self.assertEqual(gateway.call_count, 4)
        response = continue_after_confirmation(self.conversation, self.user, "cash.transaction_create", {"id": 1})
        self.assertEqual(response["text"], "Операция выполнена.")
        self.assertEqual(gateway.call_args_list[4].args[0][-2]["tool_call_id"], "confirm")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_action_rejects_free_text_not_present_in_a_user_message(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": '{"plan":"Подготовлю операцию."}',
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "write", "type": "function", "function": {
                "name": "cash_transaction_create", "arguments": json.dumps({"operation_date": "2026-01-01", "account": "cash", "direction": "income", "amount": 1, "reason": "Придуманное основание"}),
            }}]},
            {"role": "assistant", "content": "Уточните основание операции."},
        ]
        with patch("tenders.assistant_agent.execute_tool"):
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Добавь операцию")

        self.assertEqual(response["kind"], "tool_result")
        self.assertIn("Уточните", response["text"])
        self.assertIn("сообщения пользователя", gateway.call_args_list[4].args[0][-2]["content"])

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
    def test_clarification_guidance_uses_business_language(self, gateway, _preflight):
        gateway.return_value = {"role": "assistant", "content": "За какой период показать операции?"}

        respond(self.conversation, self.user, "Покажи три последних поступления по кассе")

        prompt = gateway.call_args.args[0][0]["content"]
        self.assertIn("ordinary Russian", prompt)
        self.assertIn("never for technical fields", prompt)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_explicit_and_inferred_area_use_the_same_area_tool(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan-explicit", "type": "function", "function": {
                "name": "assistant_plan", "arguments": '{"plan":"Уточню задачу по кассе."}',
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "explicit", "type": "function", "function": {"name": "assistant_select_area", "arguments": '{"area":"cash"}'}}]},
            {"role": "assistant", "content": "Что именно сделать с кассой?"},
            {"role": "assistant", "tool_calls": [{"id": "plan-inferred", "type": "function", "function": {
                "name": "assistant_plan", "arguments": '{"plan":"Уточню поступления по кассе."}',
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "inferred", "type": "function", "function": {"name": "assistant_select_area", "arguments": '{"area":"cash"}'}}]},
            {"role": "assistant", "content": "За какой период показать поступления?"},
        ]

        explicit = respond(self.conversation, self.user, "В приложении Касса покажи операции")
        second = create_conversation(self.user)
        inferred = respond(second, self.user, "Покажи поступления по кассе")

        self.assertIn("касс", explicit[-1]["text"].lower())
        self.assertIn("поступления", inferred[-1]["text"].lower())
        self.assertIn("Capability map:", gateway.call_args_list[0].args[0][0]["content"])
        self.assertIn("Capability map:", gateway.call_args_list[3].args[0][0]["content"])
        explicit_area_tools = gateway.call_args_list[1].args[1]
        inferred_area_tools = gateway.call_args_list[4].args[1]
        self.assertEqual(explicit_area_tools, inferred_area_tools)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_completed_read_is_replaced_with_compact_memory_before_the_next_turn(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan-1", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": "Найду операцию и отвечу."}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "area-1", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "resource-1", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "read:cash.transactions"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read-1", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["reason"], "limit": 1}}),
            }}]},
            {"role": "assistant", "content": "Одна операция найдена."},
            {"role": "assistant", "tool_calls": [{"id": "resource-2", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "read:cash.transactions"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read-2", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["reason"], "limit": 1}}),
            }}]},
            {"role": "assistant", "content": "Это была расходная операция."},
        ]
        result = {"entity": "cash.CashTransaction", "query": {"filters": [], "view": None, "search": None, "order_by": ["-id"]}, "total_count": 1,
                  "returned_count": 1, "aggregates": {}, "records": [{"id": 1, "reason": "x"}],
                  "sources": [{"entity": "cash.CashTransaction", "id": 1}], "source_scope": "all matching records"}
        with patch("tenders.assistant_agent.execute_tool", side_effect=[result]) as execute:
            from .assistant_agent import reply
            reply(self.conversation, self.user, "Покажи операцию")
            self.assertNotIn("agent_run", self.conversation.context)
            self.assertEqual(self.conversation.context["agent_memory"]["last_read"]["records"], [{"id": 1, "reason": "x"}])
            self.assertLessEqual(len(json.dumps(self.conversation.context["agent_memory"])), 2_000)
            reply(self.conversation, self.user, "А какая это операция?")
            self.assertEqual(execute.call_count, 1)

        messages = gateway.call_args_list[7].args[0]
        tools = [item for item in messages if item.get("role") == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tools], ["resource-2", "read-2"])
