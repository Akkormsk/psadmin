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
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {"name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать последнюю операцию", "steps": [{"id": "s1", "capability_id": "read:cash.transactions", "objective": "Найти операцию"}]}})}}]},
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
            {"assistant_plan"},
        )
        self.assertEqual(execute.call_args.args[0], "read")
        messages = gateway.call_args_list[4].args[0]
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertEqual(messages[-2]["tool_call_id"], "call_read")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_m91_simple_read_uses_plan_read_finish_and_display_values(self, gateway, _preflight):
        from cash.models import CashTransaction
        CashTransaction.objects.create(
            operation_date=date(2026, 10, 20), account="cash", direction="expense",
            amount="1250", reason="Покупка мыла", created_by=self.user,
        )
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать три операции", "steps": [{
                    "id": "s1", "capability_id": "read:cash.transactions", "objective": "Получить операции",
                    "expected_result": "Три последние операции с датой, суммой, счётом и основанием",
                }]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {
                    "fields": ["operation_date", "amount", "account", "reason"], "order_by": ["-operation_date"], "limit": 3,
                }}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Последняя операция: Наличные, 1250 рублей."}),
            }}]},
        ]

        response = respond(self.conversation, self.user, "Покажи три последние операции по кассе: дату, сумму, счёт и основание")[-1]

        self.assertEqual(gateway.call_count, 3)
        self.assertIn("Наличные", response["text"])
        read_step = response["data"]["steps"][-1]
        self.assertEqual(read_step["result"]["display"]["records"][0]["account"], "Наличные")
        for call in gateway.call_args_list:
            self.assertNotIn("assistant_select_area", [item["function"]["name"] for item in call.args[1]])
            self.assertNotIn("assistant_select_capability", [item["function"]["name"] for item in call.args[1]])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_m91_expenses_and_sum_are_one_read_step_without_calculator(self, gateway, _preflight):
        from cash.models import CashTransaction
        for direction, amount in (("expense", "100"), ("expense", "250"), ("income", "900")):
            CashTransaction.objects.create(
                operation_date=date(2026, 9, 10), account="cash", direction=direction,
                amount=amount, reason="Тест", created_by=self.user,
            )
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать сентябрьские расходы и сумму", "steps": [{
                    "id": "s1", "capability_id": "read:cash.transactions", "objective": "Получить расходы и сумму",
                    "expected_result": "Список расходов за сентябрь и общая сумма",
                }]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {
                    "fields": ["operation_date", "amount", "account", "reason"],
                    "filters": [
                        {"field": "direction", "op": "eq", "value": "expense"},
                        {"field": "operation_date", "op": "gte", "value": "2026-09-01"},
                        {"field": "operation_date", "op": "lt", "value": "2026-10-01"},
                    ], "aggregates": [{"op": "sum", "field": "amount"}], "order_by": ["-operation_date"], "limit": 20,
                }}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "За сентябрь два расхода на общую сумму 350 рублей."}),
            }}]},
        ]

        response = respond(self.conversation, self.user, "Покажи расходы по кассе за сентябрь, а затем посчитай их общую сумму")[-1]

        self.assertEqual(gateway.call_count, 3)
        read = response["data"]["steps"][-1]["result"]
        self.assertEqual(read["aggregates"]["sum_amount"], "350")
        self.assertEqual(read["query"]["filters"][0], {"field": "direction", "op": "eq", "value": "expense"})

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_m92_three_months_use_one_grouped_read_and_backend_comparison(self, gateway, _preflight):
        from cash.models import CashTransaction
        for operation_date, amount in ((date(2026, 8, 5), "100"), (date(2026, 9, 5), "350")):
            CashTransaction.objects.create(
                operation_date=operation_date, account="cash", direction="expense", amount=amount,
                reason="Тест", created_by=self.user,
            )
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Сравнить три месяца", "steps": [{
                    "id": "s1", "capability_id": "read:cash.transactions", "objective": "Получить суммы по месяцам",
                    "expected_result": "Месячные суммы и их разность",
                }]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {
                    "filters": [
                        {"field": "direction", "op": "eq", "value": "expense"},
                        {"field": "operation_date", "op": "gte", "value": "2026-07-01"},
                        {"field": "operation_date", "op": "lt", "value": "2026-10-01"},
                    ],
                    "group_by": [{"field": "operation_date", "period": "month"}],
                    "aggregates": [{"op": "sum", "field": "amount"}],
                }}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Больше всего расходов в сентябре: 350 рублей, на 250 рублей больше августа."}),
            }}]},
        ]

        response = respond(self.conversation, self.user, "Посмотри расходы по кассе за последние три полных месяца. В каком месяце их больше всего и насколько?")[-1]

        self.assertEqual(gateway.call_count, 3)
        read = response["data"]["steps"][-1]["result"]
        self.assertEqual(read["records"][0]["sum_amount"], 0)
        self.assertEqual(read["comparisons"]["sum_amount"]["maximum"]["difference_to_next"], "250")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_direct_read_then_action_keeps_the_next_capability_until_confirmation(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Проверить и добавить операцию", "steps": [
                    {"id": "s1", "capability_id": "read:cash.transactions", "objective": "Проверить операции", "expected_result": "Список операций"},
                    {"id": "s2", "capability_id": "cash.transaction_create", "objective": "Подготовить операцию", "expected_result": "Карточка подтверждения"},
                ]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount"], "limit": 1}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "create", "type": "function", "function": {
                "name": "cash_transaction_create", "arguments": json.dumps({
                    "operation_date": "2026-10-20", "account": "cash", "direction": "expense", "amount": 10, "reason": "Тест",
                }),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Операция создана."}),
            }}]},
        ]
        read_result = {"entity": "cash.CashTransaction", "query": {}, "total_count": 0,
                       "returned_count": 0, "aggregates": {}, "records": [], "sources": []}
        with patch("tenders.assistant_agent.execute_tool", return_value=read_result):
            from .assistant_agent import continue_after_confirmation, reply
            prepared = reply(self.conversation, self.user, "Проверь операции и добавь расход 10 рублей за Тест")
            finished = continue_after_confirmation(self.conversation, self.user, "cash.transaction_create", {"id": 2})

        self.assertEqual(prepared["kind"], "confirmation_required")
        self.assertEqual(finished["text"], "Операция создана.")
        self.assertEqual(gateway.call_count, 4)

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
    def test_clarifying_text_keeps_the_active_plan_without_another_gateway_call(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать операции", "steps": [{"id": "s1", "capability_id": "read:cash.transactions", "objective": "Найти операции"}]}}),
            }}]},
            {"role": "assistant", "content": "За какой период показать операции?"},
        ]

        from .assistant_agent import reply
        response = reply(self.conversation, self.user, "Покажи операции")

        self.assertEqual(response["text"], "За какой период показать операции?")
        self.assertEqual(gateway.call_count, 2)
        self.assertIn("agent_run", self.conversation.context)

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
        self.assertIn("active_task", continuation_messages[1]["content"])
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
        self.assertEqual([item.payload["output"]["plan"]["goal"] for item in plans], [
            "1. Найду операции. 2. Покажу результат.", "Операций нет; сообщу результат без суммы.",
        ])
        self.assertIn("Capability map:", gateway.call_args_list[0].args[0][0]["content"])
        self.assertNotIn("Capability map:", gateway.call_args_list[1].args[0][0]["content"])
        self.assertIn("Capability map:", gateway.call_args_list[5].args[0][0]["content"])
        requests = [item for item in self.conversation.feedback_events.filter(scope="assistant_debug") if item.payload.get("stage") == "function_call.request"]
        self.assertTrue(all(item.payload["input"]["metrics"]["tool_schema_bytes"] > 0 for item in requests))

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_finish_is_rejected_until_the_structured_plan_has_real_evidence(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать операции", "steps": [{"id": "s1", "capability_id": "read:cash.transactions", "objective": "Найти операции"}]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "early", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Готово."}),
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
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Операция найдена."}),
            }}]},
        ]
        result = {"entity": "cash.CashTransaction", "query": {}, "total_count": 0,
                  "returned_count": 0, "aggregates": {}, "records": [], "sources": []}
        with patch("tenders.assistant_agent.execute_tool", return_value=result):
            from .assistant_agent import reply
            response = reply(self.conversation, self.user, "Покажи операции")

        self.assertEqual(response["text"], "Операция найдена.")
        self.assertIn("не подтверждены шаги s1", gateway.call_args_list[2].args[0][-2]["content"])
        self.assertNotIn("agent_run", self.conversation.context)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_text_clarification_resumes_the_same_structured_plan(self, gateway, _preflight):
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Показать операции за период", "steps": [{"id": "s1", "capability_id": "read:cash.transactions", "objective": "Найти операции"}]}}),
            }}]},
            {"role": "assistant", "content": "За какой период показать операции?"},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "read:cash.transactions"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_cash_transactions", "arguments": json.dumps({"plan": {"fields": ["amount"], "limit": 1}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "За сентябрь операций нет."}),
            }}]},
        ]
        result = {"entity": "cash.CashTransaction", "query": {}, "total_count": 0,
                  "returned_count": 0, "aggregates": {}, "records": [], "sources": []}
        with patch("tenders.assistant_agent.execute_tool", return_value=result):
            first = respond(self.conversation, self.user, "Покажи операции")[-1]
            second = respond(self.conversation, self.user, "За сентябрь")[-1]

        self.assertIn("За какой период", first["text"])
        self.assertEqual(second["text"], "За сентябрь операций нет.")
        self.assertEqual(self.conversation.feedback_events.filter(
            scope="assistant_debug", payload__stage="agent.plan",
        ).count(), 1)

    def test_structured_plan_rejects_an_unpublished_capability(self):
        from .assistant_agent import _normalise_plan
        from .assistant_tools import AssistantToolError

        with self.assertRaises(AssistantToolError):
            _normalise_plan({"goal": "Тест", "steps": [{
                "id": "s1", "capability_id": "not.published", "objective": "Тест",
            }]}, self.user)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_legacy_saved_plan_cannot_finish_without_a_structured_replan(self, gateway, _preflight):
        self.conversation.context["agent_run"] = {
            "messages": [{"role": "system", "content": "old"}], "steps": [], "plan": "Старый план",
        }
        self.conversation.save(update_fields=["context"])
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Готово."}),
            }}]},
            {"role": "assistant", "content": "Уточните следующий шаг."},
        ]

        from .assistant_agent import reply
        response = reply(self.conversation, self.user, "Продолжай")

        self.assertEqual(response["text"], "Уточните следующий шаг.")
        self.assertIn("обновлённый план", gateway.call_args_list[1].args[0][-2]["content"])
        self.conversation.refresh_from_db()
        self.assertTrue(self.conversation.context["agent_run"]["plan"]["legacy"])

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
        self.assertIn("опираться на слова пользователя", gateway.call_args_list[4].args[0][-2]["content"])

    @patch("tenders.assistant_agent.timezone.localdate", return_value=date(2026, 10, 19))
    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_normalised_action_text_reaches_one_confirmation_card(self, gateway, _preflight, _today):
        from cash.models import CashTransaction
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Создать расход по кассе", "steps": [{"id": "s1", "capability_id": "cash.transaction_create", "objective": "Подготовить расход"}]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "content": "Проверьте операцию.", "tool_calls": [{"id": "create", "type": "function", "function": {
                "name": "cash_transaction_create", "arguments": json.dumps({
                    "operation_date": "2026-10-19", "account": "cash", "direction": "expense", "amount": 1250,
                    "reason": "Покупка коробок",
                }),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "finish", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Операция проведена."}),
            }}]},
        ]

        messages = respond(self.conversation, self.user, "Запиши расход наличными сегодня 1250 рублей за пакупку каробок")
        response = messages[-1]

        self.assertEqual(response["kind"], "confirmation_required")
        self.assertEqual(response["data"]["arguments"]["reason"], "Покупка коробок")
        self.assertEqual(response["data"]["arguments"]["operation_date"], "2026-10-19")
        self.assertEqual(gateway.call_count, 4)
        self.assertEqual(len(response["data"]["action"]["arguments"]), 5)
        self.assertFalse(CashTransaction.objects.exists())

        self.client.force_login(self.user)
        confirmed = self.client.post(reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, response["id"]]), {"accept": "yes"})

        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(CashTransaction.objects.count(), 1)
        self.assertEqual(CashTransaction.objects.get().reason, "Покупка коробок")
        self.conversation.refresh_from_db()
        self.assertNotIn("agent_run", self.conversation.context)
        assistant_texts = [
            event.raw_text for event in self.conversation.feedback_events.filter(scope="assistant_conversation")
            if (event.payload or {}).get("role") == "assistant"
        ]
        self.assertNotIn("Действие выполнено.", assistant_texts)
        self.assertEqual(assistant_texts[-1], "Операция проведена.")
        repeated = self.client.post(reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, response["id"]]), {"accept": "yes"})
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(CashTransaction.objects.count(), 1)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_cancelled_confirmation_keeps_the_plan_open_without_a_write(self, gateway, _preflight):
        from cash.models import CashTransaction
        gateway.side_effect = [
            {"role": "assistant", "tool_calls": [{"id": "plan", "type": "function", "function": {
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Создать операцию", "steps": [{"id": "s1", "capability_id": "cash.transaction_create", "objective": "Подготовить операцию"}]}}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "area", "type": "function", "function": {
                "name": "assistant_select_area", "arguments": json.dumps({"area": "cash"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "capability", "type": "function", "function": {
                "name": "assistant_select_capability", "arguments": json.dumps({"capability": "cash.transaction_create"}),
            }}]},
            {"role": "assistant", "tool_calls": [{"id": "create", "type": "function", "function": {
                "name": "cash_transaction_create", "arguments": json.dumps({
                    "operation_date": "2026-10-20", "account": "cash", "direction": "expense", "amount": 10, "reason": "Тест",
                }),
            }}]},
            {"role": "assistant", "content": "Действие отменено. Что изменить?"},
        ]

        response = respond(self.conversation, self.user, "Запиши расход наличными 10 рублей за Тест")[-1]
        self.client.force_login(self.user)
        cancelled = self.client.post(
            reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, response["id"]]), {"accept": "no"},
        )

        self.assertEqual(cancelled.status_code, 200)
        self.assertFalse(CashTransaction.objects.exists())
        self.conversation.refresh_from_db()
        run = self.conversation.context["agent_run"]
        self.assertTrue(run["waiting_user"])
        self.assertEqual(run["steps"][-1]["status"], "cancelled")

    def test_action_text_normalisation_accepts_typos_case_spacing_and_punctuation(self):
        from .assistant_agent import _validate_action_text_sources
        from .assistant_tools import available_tools
        tool = next(item for item in available_tools(self.user) if item["id"] == "cash.transaction_create")

        for source, normalised in (
            ("  пакупку   каробок!!!  ", "Покупка коробок"),
            ("пакупка каробок", "Покупка коробок"),
            ("распечатку 1 иста А4 4+4", "Распечатка 1 листа А4 4+4"),
            ("пакупка бумаги", "Покупка бумаги"),
        ):
            with self.subTest(source=source):
                _validate_action_text_sources(tool, {"reason": normalised}, {
                    "messages": [{"role": "user", "content": source}],
                })

    def test_action_text_normalisation_is_generic_for_another_registered_action(self):
        from .assistant_agent import _validate_action_text_sources
        from .assistant_tools import available_tools
        tool = next(item for item in available_tools(self.user) if item["id"] == "provider.create")

        _validate_action_text_sources(tool, {"name": "Атекс"}, {
            "messages": [{"role": "user", "content": "Создай контрагента Атексс."}],
        })

    def test_action_text_normalisation_rejects_new_business_facts_and_ambiguous_short_words(self):
        from .assistant_agent import _validate_action_text_sources
        from .assistant_tools import AssistantToolError, available_tools
        tool = next(item for item in available_tools(self.user) if item["id"] == "cash.transaction_create")
        state = {"messages": [{"role": "user", "content": "Покупка бумаги за 10 рублей"}]}

        with self.assertRaises(AssistantToolError):
            _validate_action_text_sources(tool, {"reason": "Покупка бумаги для ООО Ромашка"}, state)
        with self.assertRaises(AssistantToolError):
            _validate_action_text_sources(tool, {"reason": "Покупка не бумаги"}, state)
        with self.assertRaises(AssistantToolError):
            _validate_action_text_sources(tool, {"reason": "Покупка 1500 коробок"}, {
                "messages": [{"role": "user", "content": "Покупка 1250 коробок"}],
            })

    def test_typed_action_arguments_do_not_require_serialized_literals_in_user_text(self):
        from .assistant_agent import _validate_action_text_sources
        from .assistant_tools import available_tools
        tool = next(item for item in available_tools(self.user) if item["id"] == "cash.transaction_create")

        _validate_action_text_sources(tool, {
            "operation_date": "2026-10-19", "account": "card", "direction": "income", "amount": 10,
            "reason": "Распечатка",
        }, {"messages": [{"role": "user", "content": "Сегодня внеси приход на карту десять рублей за распечатку"}]})

    @patch("tenders.assistant_agent.timezone.localdate", return_value=date(2026, 10, 19))
    def test_system_prompt_exposes_application_date_for_relative_dates(self, _today):
        from .assistant_agent import _system_prompt

        self.assertIn("2026-10-19", _system_prompt(self.user, {"capability_map_pending": False}))

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_finished_text_turn_does_not_keep_visible_history(self, gateway, _preflight):
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
                "name": "assistant_plan", "arguments": json.dumps({"plan": {"goal": "Найти операцию", "steps": [{"id": "s1", "capability_id": "read:cash.transactions", "objective": "Найти операцию"}]}}),
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
            {"role": "assistant", "tool_calls": [{"id": "finish-1", "type": "function", "function": {
                "name": "assistant_finish", "arguments": json.dumps({"answer": "Одна операция найдена."}),
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
            selection = self.conversation.context["agent_memory"]["recent_selections"][-1]
            self.assertEqual(selection["records"], [{"id": 1, "reason": "x"}])
            self.assertEqual(selection["record_ids"], [1])
            self.assertLessEqual(len(json.dumps(self.conversation.context["agent_memory"])), 8 * 1024)
            reply(self.conversation, self.user, "А какая это операция?")
            self.assertEqual(execute.call_count, 1)

        messages = gateway.call_args_list[5].args[0]
        self.assertIn("Compact conversation memory", messages[1]["content"])
        self.assertEqual([item["role"] for item in messages], ["system", "system", "user"])
        self.assertNotIn("Одна операция найдена.", json.dumps(messages, ensure_ascii=False))


class AssistantMemoryTests(TestCase):
    """M7 persistence tests use the real conversation JSON field and DB."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser("memory", "memory@example.test", "password")
        self.other = get_user_model().objects.create_superuser("memory-other", "memory-other@example.test", "password")
        self.conversation = create_conversation(self.user)

    @staticmethod
    def read(entity, identifiers, *, total=None, records=None, source_scope="all matching records"):
        records = records if records is not None else [{"id": value, "name": f"Объект {value}"} for value in identifiers]
        return {
            "entity": entity, "query": {"entity": entity, "filters": [], "order_by": ["-id"], "limit": len(identifiers)},
            "total_count": len(identifiers) if total is None else total, "returned_count": len(records),
            "aggregates": {}, "records": records,
            "sources": [{"entity": entity, "id": value} for value in identifiers], "source_scope": source_scope,
        }

    def finish(self, steps, plan="Проверю данные."):
        from .assistant_agent import _finish_turn
        _finish_turn(self.conversation, {"steps": steps, "plan": plan})
        self.conversation.refresh_from_db()
        return self.conversation.context["agent_memory"]

    def test_read_survives_a_text_turn_and_keeps_real_internal_id(self):
        memory = self.finish([{"tool_id": "read:tenders.incoming", "result": self.read(
            "tender_selection.Tender", [17], records=[{"id": 17, "purchase_number": "0123456789", "name": "Тендер"}],
        )}])

        self.assertEqual(memory["active_focus"]["id"], 17)
        self.assertEqual(memory["active_focus"]["external_ids"], [{"purchase_number": "0123456789"}])
        self.finish([], "Отвечу о документах.")
        self.assertEqual(self.conversation.context["agent_memory"]["active_focus"]["id"], 17)

    def test_multiple_read_selections_are_preserved_in_order(self):
        self.finish([{"tool_id": "read:cash.transactions", "result": self.read("cash.CashTransaction", [1, 2])}])
        memory = self.finish([{"tool_id": "read:tenders.incoming", "result": self.read("tender_selection.Tender", [3])}])

        self.assertEqual([item["entity"] for item in memory["recent_selections"]], ["cash.CashTransaction", "tender_selection.Tender"])
        self.assertEqual(memory["recent_selections"][0]["record_ids"], [1, 2])

    def test_partial_selection_never_becomes_complete_or_a_reusable_global_scope(self):
        memory = self.finish([{"tool_id": "read:cash.transactions", "result": self.read(
            "cash.CashTransaction", list(range(1, 11)), total=39,
        )}])
        selection = memory["recent_selections"][-1]
        self.assertFalse(selection["complete"])
        self.assertEqual(selection["record_ids"], list(range(1, 11)))

    def test_new_state_keeps_prior_plan_as_context_not_an_active_plan(self):
        self.finish([], "Старый план по кассе.")
        from .assistant_agent import _new_state
        state = _new_state(self.conversation, self.user, "Теперь найди тендер", [])

        self.assertNotIn("plan", state)
        self.assertIn("active_task", state["messages"][1]["content"])
        self.assertTrue(state["capability_map_pending"])

    def test_new_state_uses_compact_memory_not_completed_visible_history(self):
        add_assistant_message(self.conversation, "Старый длинный ответ " + "x" * 4000)
        from .assistant_agent import _new_state

        state = _new_state(self.conversation, self.user, "Новая задача", [])

        self.assertEqual([item["role"] for item in state["messages"]], ["system", "user"])
        self.assertEqual(state["messages"][-1]["content"], "Новая задача")

    def test_action_result_is_retained_only_after_actual_executor_result(self):
        memory = self.finish([{"tool_id": "cash.transaction_create", "result": {"id": 44, "created": True}}])

        self.assertEqual(memory["recent_actions"][-1]["object_ids"], {"id": 44})
        self.assertEqual(memory["recent_actions"][-1]["tool_id"], "cash.transaction_create")

    def test_old_memory_format_is_promoted_without_exception(self):
        self.conversation.context["agent_memory"] = {"task_plan": "Старый план", "last_read": {
            "owner_id": self.user.pk, "entity": "cash.CashTransaction", "query": {}, "total_count": 1,
            "returned_count": 1, "records": [{"id": 7, "reason": "x"}],
            "sources": [{"entity": "cash.CashTransaction", "id": 7}], "source_scope": "all matching records",
        }}
        self.conversation.save(update_fields=["context"])
        from .assistant_agent import _new_state
        state = _new_state(self.conversation, self.user, "Продолжим", [])

        self.assertEqual(state["memory"]["recent_selections"][0]["record_ids"], [7])
        self.assertNotIn("plan", state)

    def test_memory_is_bounded_without_cutting_saved_ids(self):
        for number in range(7):
            self.finish([{"tool_id": "read:cash.transactions", "result": self.read(
                "cash.CashTransaction", list(range(number * 50, number * 50 + 50)),
                records=[{"id": value, "reason": "x" * 200} for value in range(number * 50, number * 50 + 50)],
            )}], f"План {number}")
        memory = self.conversation.context["agent_memory"]

        self.assertLessEqual(len(json.dumps(memory, ensure_ascii=False).encode()), 8 * 1024)
        self.assertLessEqual(len(memory["recent_selections"]), 5)
        self.assertTrue(all(len(item["record_ids"]) == 50 for item in memory["recent_selections"]), memory["recent_selections"])

    def test_memory_isolated_between_conversations_and_users(self):
        self.finish([{"tool_id": "read:cash.transactions", "result": self.read("cash.CashTransaction", [1])}])
        other_conversation = create_conversation(self.other)
        from .assistant_agent import _new_state
        state = _new_state(other_conversation, self.other, "Покажи данные", [])

        self.assertNotIn("recent_selections", " ".join(item["content"] for item in state["messages"]))
