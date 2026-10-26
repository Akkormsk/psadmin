import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from cash.models import CashTransaction
from .assistant_conversations import create_conversation, respond
from .test_assistant_conversations import function_call, planned_call


class AssistantFinalizationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("finalization", "final@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.client.force_login(self.user)
        patcher = patch("tenders.assistant_agent._gateway")
        self.gateway = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("tenders.assistant_agent.preflight")
        patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, continuation):
        self.gateway.side_effect = [planned_call("cash.transaction_create"), function_call("cash_transaction_create", {
            "operation_date": "2026-10-25", "account": "cash", "direction": "expense", "amount": 1,
            "reason": "Финальная проверка",}, "write"), continuation]
        card = respond(self.conversation, self.user, "Подготовь расход наличными 1 рубль за 25 октября 2026. Основание: Финальная проверка. Только подготовь, ничего пока не выполняй")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertFalse(CashTransaction.objects.exists())
        return reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, card["id"]])

    def receipt(self):
        messages = self.gateway.call_args_list[-1].args[0]
        return json.loads(next(m["content"] for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "write"))

    def test_button_authorization_and_actual_result_reach_final_after_prepare_only(self):
        url = self.prepare(function_call("assistant_finish", {"answer": "Расход создан.</answer>"}))
        response = self.client.post(url, {"accept": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CashTransaction.objects.count(), 1)
        receipt = self.receipt()
        self.assertIs(receipt["user_confirmed"], True)
        self.assertEqual(receipt["execution_status"], "completed")
        final = response.json()["messages"][-1]
        self.assertEqual(final["text"], "Расход создан.")
        execution = next(s for s in final["data"]["steps"] if s["tool_id"] == "cash.transaction_create")
        self.assertEqual(receipt["result"], execution["result"])
        self.assertEqual(self.client.post(url, {"accept": "yes"}).status_code, 409)
        self.assertEqual(CashTransaction.objects.count(), 1)

    def test_cancellation_is_not_reported_as_authorization_or_execution(self):
        url = self.prepare(function_call("assistant_finish", {"answer": "Must not run"}))
        calls = self.gateway.call_count
        response = self.client.post(url, {"accept": "no"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.gateway.call_count, calls)
        final = response.json()["messages"][-1]
        execution = next(s for s in final["data"]["steps"] if s["tool_id"] == "cash.transaction_create")
        self.assertEqual(execution["status"], "cancelled")
        self.assertIsNone(execution["result"])
        self.assertNotIn("user_confirmed", final["data"])
        self.conversation.refresh_from_db()
        self.assertNotIn("agent_run", self.conversation.context)
        self.assertEqual(self.conversation.context["agent_memory"]["active_task"]["status"], "cancelled")
        self.assertFalse(CashTransaction.objects.exists())

    def test_only_unmatched_trailing_service_marker_is_removed(self):
        from .assistant_agent import _final_answer_text
        state = {"messages": [{"role": "user", "content": "Ответь кратко"}]}
        cases = [
            ("Готово. </answer>\n", "Готово."),
            ("<div>Готово</div>\n</answer>", "<div>Готово</div>"),
            ("<answer>Пользовательская разметка</answer>", "<answer>Пользовательская разметка</answer>"),
            ("<div>Пользовательская разметка</div>", "<div>Пользовательская разметка</div>"),
            ("Тег </answer> внутри текста", "Тег </answer> внутри текста"),
            ("```html\n</answer>\n```", "```html\n</answer>\n```"),
            ("</answer>", ""),
        ]
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(_final_answer_text(original, state), expected)
        literal = {"messages": [{"role": "user", "content": "Выведи буквально: пример</answer>"}]}
        self.assertEqual(_final_answer_text("пример</answer>", literal), "пример</answer>")
        source = {"messages": [{"role": "tool", "content": '{"html": "пример</answer>"}'}]}
        self.assertEqual(_final_answer_text("пример</answer>", source), "пример</answer>")

    def test_existing_result_is_not_rewritten_as_newly_created(self):
        from tender_selection.models import Tender
        from .assistant_tools import execute_tool
        from .models import TenderEstimate
        from .test_assistant_conversations import remember_reference
        tender = Tender.objects.create(purchase_number="FINAL-EXISTING", title="Существующий расчёт")
        initial = execute_tool("tender.send_to_calculation", self.user, {"tender_id": tender.pk}, confirmed=True)
        remember_reference(self.conversation, self.user, "tender_selection.Tender", tender.pk)
        self.gateway.side_effect = [planned_call("tender.send_to_calculation", sources={"tender_id": "verified"}),
            function_call("tender_send_to_calculation", {"tender_id": tender.pk}, "write"),
            function_call("assistant_finish", {"answer": "Возвращён существующий расчёт"})]
        card = respond(self.conversation, self.user, "Подготовь передачу выбранного объекта, пока не выполняй")[-1]
        url = reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, card["id"]])
        self.assertEqual(self.client.post(url, {"accept": "yes"}).status_code, 200)
        receipt = self.receipt()
        self.assertIs(receipt["user_confirmed"], True)
        self.assertIs(receipt["result"]["created"], False)
        self.assertEqual(receipt["result"]["estimate_id"], initial["estimate_id"])
        self.assertEqual(TenderEstimate.objects.count(), 1)

    def test_marker_only_finish_recovers_without_executing_again(self):
        url = self.prepare(function_call("assistant_finish", {"answer": "unused"}))
        self.gateway.side_effect = [function_call("assistant_finish", {"answer": "</answer>"}),
                                   function_call("assistant_finish", {"answer": "Расход создан"})]
        response = self.client.post(url, {"accept": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][-1]["text"], "Расход создан")
        self.assertEqual(CashTransaction.objects.count(), 1)


class AssistantGatewayEnvelopeTests(SimpleTestCase):
    def test_gateway_allows_plan_output_headroom_without_changing_tools(self):
        from .assistant_agent import _gateway

        payload = {"choices": [{"message": {"role": "assistant", "tool_calls": []}}]}
        tools = [{"type": "function", "function": {"name": "assistant_plan"}}]
        with patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": "local-test"}), patch("tenders.assistant_agent.urlopen") as transport:
            transport.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            _gateway([{"role": "user", "content": "Составь план"}], tools)
        body = json.loads(transport.call_args.args[0].data)
        self.assertEqual(body["max_tokens"], 2400)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "required")
        self.assertFalse(body["parallel_tool_calls"])
