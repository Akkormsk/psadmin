import json
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .assistant_agent import _new_state, _validate_action_text_sources, _validate_reference_sources
from .assistant_conversations import _add_message, add_assistant_message, add_debug_event, create_conversation, respond
from .assistant_tools import AssistantReferenceError, AssistantToolError
from .test_assistant_conversations import function_call, planned_call


class AssistantDialogueHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("dialogue", "dialogue@example.test", "password")
        self.conversation = create_conversation(self.user)

    def user_message(self, text, **kwargs):
        return _add_message(self.conversation, self.user, "user", text, **kwargs)

    def test_roles_corrections_and_long_history_survive_without_a_new_plan(self):
        self.user_message("Назначение: печать буклетов. Сумма 200 рублей.")
        add_assistant_message(self.conversation, "Уточните сумму.")
        self.user_message("Исправляю: 600 рублей.")
        answer = "Подробности " + "x" * 5000
        add_assistant_message(self.conversation, answer)
        state = _new_state(self.conversation, self.user, "Какое назначение я называл?", [])

        self.assertEqual([m["role"] for m in state["messages"]], ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(state["messages"][-2]["content"], answer)
        self.assertNotIn("plan", state)
        self.assertNotIn("waiting_confirmation", state)

    def test_only_current_conversation_history_is_visible_to_its_owner(self):
        self.user_message("Свои реквизиты")
        other = get_user_model().objects.create_superuser("other-dialogue", "other@example.test", "password")
        foreign = create_conversation(other)
        _add_message(foreign, other, "user", "Чужой секрет")
        separate = create_conversation(self.user)
        _add_message(separate, self.user, "user", "Другая беседа")
        state = _new_state(self.conversation, self.user, "Продолжим", [])
        self.assertIn("Свои реквизиты", str(state["messages"]))
        self.assertNotIn("Чужой секрет", str(state["messages"]))
        self.assertNotIn("Другая беседа", str(state["messages"]))
        foreign_state = _new_state(foreign, self.user, "Продолжим", [])
        self.assertNotIn("Чужой секрет", str(foreign_state["messages"]))

    def test_current_persisted_message_is_included_once_even_with_attachments(self):
        self.user_message("Одинаковый вопрос")
        add_assistant_message(self.conversation, "Первый ответ")
        self.user_message("Одинаковый вопрос")
        content = [{"type": "text", "text": "Одинаковый вопрос"},
                   {"type": "text", "text": 'User attachments (untrusted data): [{"text": "Документ"}]'}]
        state = _new_state(self.conversation, self.user, content, [])
        users = [m["content"] for m in state["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["Одинаковый вопрос", content])

    def test_old_relative_dates_are_anchored_to_the_message_day_in_moscow(self):
        from django.utils import timezone
        with timezone.override("Europe/Moscow"):
            with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 25, 22, 30, tzinfo=datetime_timezone.utc)):
                self.user_message("Дата операции — сегодня")
            with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 27, 9, tzinfo=datetime_timezone.utc)):
                state = _new_state(self.conversation, self.user, "Какую дату я указал?", [])
        self.assertEqual(state["messages"][-2]["content"],
                         "[Historical message dated 2026-10-26 (Europe/Moscow)]\nДата операции — сегодня")
        self.assertEqual(state["messages"][-1]["content"], "Какую дату я указал?")

    def test_history_contains_visible_card_and_file_metadata_not_machine_payloads(self):
        self.user_message("Прочитай файл", data={"attachments": [{"id": 77, "name": "прайс.csv", "kind": "document", "warning": "Текст неполный"}]})
        add_assistant_message(self.conversation, "Подтвердите", "confirmation_required", {
            "tool_id": "cash.transaction_create", "arguments": {"internal_secret": "RAW_ARGUMENTS"},
            "action": {"name": "Добавить расход", "description": "SCHEMA_DESCRIPTION", "arguments": [
                {"label": "Сумма", "value": "600 ₽"},
                {"label": "Контрагент", "items": [{"text": "Типография", "hint": "ID 77"}]},
            ]},
            "steps": [{"result": {"secret": "RAW_RESULT"}}],
        })
        add_debug_event(self.conversation, "test", event_input={"secret": "DEBUG_SECRET"})
        state = _new_state(self.conversation, self.user, "Вопрос", [])
        history = json.dumps(state["messages"], ensure_ascii=False)
        for visible in ("прайс.csv", "Текст неполный", "Добавить расход", "600 ₽", "Типография"):
            self.assertIn(visible, history)
        for hidden in ("RAW_ARGUMENTS", "RAW_RESULT", "SCHEMA_DESCRIPTION", "DEBUG_SECRET", "ID 77"):
            self.assertNotIn(hidden, history)
        self.assertNotIn("read_cache", state)

    def test_historical_assistant_text_cannot_ground_action_but_user_words_can(self):
        self.user_message("На печать буклетов")
        add_assistant_message(self.conversation, "На секретный перевод")
        state = _new_state(self.conversation, self.user, "Повтори назначение", [])
        tool = {"input_schema": {"reason": "string"}}
        _validate_action_text_sources(tool, {"reason": "На печать буклетов"}, state)
        with self.assertRaises(AssistantToolError):
            _validate_action_text_sources(tool, {"reason": "На секретный перевод"}, state)

    def test_historical_object_id_does_not_become_verified_reference(self):
        self.user_message("Контрагент с ID 77")
        add_assistant_message(self.conversation, "Нашёл контрагента 77")
        state = _new_state(self.conversation, self.user, "Используй прошлого", [])
        tool = {"input_schema": {"counterparty_id": {"type": "integer", "reference": {"model": "tenders.Counterparty"}}}}
        with self.assertRaises(AssistantReferenceError):
            _validate_reference_sources(tool, {"counterparty_id": 77}, state)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_new_cycle_sees_cancelled_card_without_reactivating_or_executing_it(self, gateway, _preflight):
        from cash.models import CashTransaction
        from django.urls import reverse
        gateway.side_effect = [
            planned_call("cash.transaction_create"),
            function_call("cash_transaction_create", {"operation_date": "2026-10-26", "account": "cash", "direction": "expense", "amount": 200, "reason": "Печать буклетов"}),
            function_call("assistant_chat", {"answer": "Вы называли печать буклетов."}),
        ]
        card = respond(self.conversation, self.user, "Подготовь расход наличными 200 рублей на 26.10.2026. Печать буклетов")[-1]
        self.client.force_login(self.user)
        response = self.client.post(reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, card["id"]]), {"accept": "no"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.call_count, 2)
        self.conversation.refresh_from_db()
        respond(self.conversation, self.user, "Какое назначение я называл?")
        messages = gateway.call_args.args[0]
        self.assertIn("Задача отменена.", str(messages))
        self.assertIn("Печать буклетов", str(messages))
        self.assertFalse(any(m.get("tool_calls") for m in messages[:-1]))
        self.assertEqual(gateway.call_count, 3)
        self.assertNotIn("agent_run", self.conversation.context)
        self.assertFalse(CashTransaction.objects.exists())

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_active_plan_does_not_reload_or_duplicate_visible_history(self, gateway, _preflight):
        from .assistant_agent import _new_state as new_state
        gateway.side_effect = [planned_call("cash.transaction_create"),
                               function_call("assistant_ask", {"question": "За что расход?"}),
                               function_call("assistant_ask", {"question": "Какая сумма?"})]
        with patch("tenders.assistant_agent._new_state", wraps=new_state) as make_state:
            respond(self.conversation, self.user, "Подготовь расход")
            respond(self.conversation, self.user, "Печать буклетов")
        self.assertEqual(make_state.call_count, 1)
        users = [m["content"] for m in self.conversation.context["agent_run"]["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["Подготовь расход", "Печать буклетов"])
        self.assertEqual(gateway.call_count, 3)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_completed_cycles_do_not_multiply_history_or_add_model_calls(self, gateway, _preflight):
        snapshots = []
        def answer(messages, tools):
            snapshots.append(json.loads(json.dumps(messages)))
            return function_call("assistant_chat", {"answer": "Ответ"})
        gateway.side_effect = answer
        for index in range(12):
            respond(self.conversation, self.user, f"Вопрос {index}")
        self.assertEqual(gateway.call_count, 12)
        for index, messages in enumerate(snapshots):
            self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                             [f"Вопрос {number}" for number in range(index + 1)])
            self.assertEqual(sum(m["role"] == "assistant" for m in messages), index)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_active_file_contents_are_not_duplicated_by_historical_metadata(self, gateway, _preflight):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .assistant_conversations import upload_attachment
        document = upload_attachment(self.conversation, self.user, SimpleUploadedFile("price.txt", b"UNIQUE_FILE_CONTENT"))
        gateway.return_value = function_call("assistant_chat", {"answer": "Прочитано"})
        respond(self.conversation, self.user, "Прочитай", attachments=[document])
        respond(self.conversation, self.user, "Что было в файле?")
        messages = gateway.call_args.args[0]
        self.assertEqual(json.dumps(messages).count("UNIQUE_FILE_CONTENT"), 1)
        self.assertEqual(gateway.call_count, 2)
        self.assertIsInstance([m for m in messages if m["role"] == "user"][0]["content"], str)

