import json
from copy import deepcopy
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from cash.models import CashTransaction
from .assistant_agent import _model_read_result
from .assistant_conversations import create_conversation, respond
from .test_assistant_conversations import function_call, planned_call


class AssistantReadContextTests(SimpleTestCase):
    def result(self):
        return {
            "entity": "example.Record", "source_id": "verified-source",
            "query": {"filters": [{"field": "state", "op": "eq", "value": "draft"}]},
            "records": [{"id": 7, "name": "Пример", "state": "draft", "amount": "12.30", "date": "2026-10-26"}],
            "aggregates": {"sum_amount": "12.30"}, "total_count": 3, "returned_count": 1,
            "sources": [{"entity": "example.Record", "id": 7}],
            "source_scope": "returned records only; aggregates cover all matching records",
            "display": {
                "entity": "Запись", "fields": [{"name": "state", "label": "Состояние"}],
                "records": [{"id": 7, "name": "Пример", "state": "Черновик", "amount": "12.30", "date": "26.10.2026"}],
                "aggregates": {"sum_amount": "12.30"},
            },
        }

    def test_projection_keeps_display_and_only_differing_raw_values(self):
        original = self.result()
        saved = deepcopy(original)
        result = _model_read_result(original)
        self.assertEqual(original, saved)
        self.assertEqual(result["records"], original["display"]["records"])
        self.assertEqual(result["raw_values"], [{"state": "draft", "date": "2026-10-26"}])
        self.assertNotIn("records", result["display"])
        self.assertNotIn("aggregates", result["display"])
        self.assertEqual(result["display"]["fields"], original["display"]["fields"])
        for key in ("entity", "source_id", "sources", "query", "source_scope", "total_count", "returned_count", "aggregates"):
            self.assertEqual(result[key], original[key])
        self.assertLess(len(json.dumps(result)), len(json.dumps(original)))

    def test_row_alignment_restores_every_original_value_without_truncation(self):
        original = self.result()
        original["records"] *= 50
        original["display"]["records"] *= 50
        original["records"] = [dict(row, id=i) for i, row in enumerate(original["records"])]
        original["display"]["records"] = [dict(row, id=i) for i, row in enumerate(original["display"]["records"])]
        result = _model_read_result(original)
        restored = [{**row, **raw} for row, raw in zip(result["records"], result["raw_values"])]
        self.assertEqual(restored, original["records"])
        self.assertEqual(len(result["records"]), 50)

    def test_identical_values_need_no_second_table(self):
        original = self.result()
        original["display"]["records"] = deepcopy(original["records"])
        result = _model_read_result(original)
        self.assertNotIn("raw_values", result)
        self.assertEqual(result["records"], original["records"])

    def test_empty_and_grouped_results_preserve_boundaries_and_markers(self):
        original = self.result()
        original["records"] = [{"period": None, "empty_period": True, "amount": "0.00"}]
        original["display"]["records"] = [{"period": None, "amount": "0.00"}]
        result = _model_read_result(original)
        self.assertEqual(result["records"], original["display"]["records"])
        self.assertEqual(result["raw_values"], [{"empty_period": True}])
        original["records"] = []
        original["display"]["records"] = []
        self.assertEqual(_model_read_result(original)["records"], [])

    def test_missing_or_misaligned_display_is_left_intact(self):
        for display in (None, {}, {"records": []}, {"records": ["invalid"]}, {"records": [{"id": 999}]}):
            original = self.result()
            original["display"] = display
            self.assertEqual(_model_read_result(original), original)

    def test_projection_that_does_not_save_space_keeps_original_format(self):
        original = {"records": [{"value": "code"}], "display": {"records": [{"value": "Name"}]}}
        self.assertEqual(_model_read_result(original), original)

    def test_json_types_precision_and_distinct_aggregates_are_preserved(self):
        original = self.result()
        original["records"] = [{"id": 1, "value": 0, "timestamp": "2026-10-26T01:02:03.123456+00:00"}]
        original["display"]["records"] = [{"id": 1, "value": False, "timestamp": "2026-10-26 04:02:03.123456+03:00"}]
        original["display"]["aggregates"] = {"sum_amount": "12,30"}
        result = _model_read_result(original)
        self.assertIs(type(result["raw_values"][0]["value"]), int)
        self.assertEqual(result["display"]["aggregates"], {"sum_amount": "12,30"})
        restored = [{**row, **raw} for row, raw in zip(result["records"], result["raw_values"])]
        self.assertEqual(json.dumps(restored), json.dumps(original["records"]))


class AssistantReadContextIntegrationTests(TestCase):
    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._gateway")
    def test_only_model_receipt_is_compacted_backend_keeps_verified_raw_result(self, gateway, _preflight):
        user = get_user_model().objects.create_superuser("read-context", "read-context@example.test", "password")
        conversation = create_conversation(user)
        transaction = CashTransaction.objects.create(operation_date=date(2026, 10, 26), account="cash", direction="expense", amount="12.30", reason="Синтетическая запись", created_by=user)
        gateway.side_effect = [
            planned_call("read:cash.transactions"),
            function_call("read_cash_transactions", {"plan": {"fields": ["id", "operation_date", "direction", "amount"], "filters": [{"field": "id", "op": "eq", "value": transaction.pk}]}}, "read-proof"),
            function_call("assistant_ask", {"question": "Что ещё показать?"}),
        ]
        respond(conversation, user, "Покажи расход за 26 октября 2026")
        messages = gateway.call_args_list[-1].args[0]
        receipt = json.loads(next(item["content"] for item in messages if item.get("tool_call_id") == "read-proof"))["result"]
        conversation.refresh_from_db()
        backend = next(step["result"] for step in conversation.context["agent_run"]["steps"] if step["tool_id"] == "read:cash.transactions")
        self.assertEqual(backend["records"][0]["operation_date"], "2026-10-26")
        self.assertEqual(backend["records"][0]["direction"], "expense")
        self.assertEqual(receipt["records"], backend["display"]["records"])
        self.assertNotIn("records", receipt["display"])
        self.assertEqual(receipt["raw_values"][0]["operation_date"], "2026-10-26")
        self.assertEqual(receipt["raw_values"][0]["direction"], "expense")
        for key in ("sources", "source_id", "query", "total_count", "returned_count", "source_scope"):
            self.assertEqual(receipt[key], backend[key])
        self.assertEqual(conversation.context["agent_run"]["read_cache"]["records"], backend["records"])

    def test_compact_context_preserves_sources_annotations_and_attachment_whitespace(self):
        from .assistant_agent import _finish_turn, _new_state, _system_prompt, _user_content
        from .assistant_read import execute
        from .assistant_tools import available_capability_map, capability_map_text

        user = get_user_model().objects.create_superuser('compact-input', 'compact@example.test', 'password')
        conversation = create_conversation(user)
        transaction = CashTransaction.objects.create(operation_date=date(2026, 10, 27), account='cash', direction='expense',
            amount='123.45', reason='Текст  с  пробелами\nи новой строкой', created_by=user)
        result = execute(user, {'entity': 'cash.CashTransaction', 'fields': ['id', 'amount', 'reason'],
            'filters': [{'field': 'id', 'op': 'eq', 'value': transaction.pk}]})
        result['source_id'] = 'synthetic-compact-source'
        _finish_turn(conversation, {'steps': [{'tool_id': 'read:cash.transactions', 'result': result}]})
        saved_memory = deepcopy(conversation.context['agent_memory'])
        state = _new_state(conversation, user, 'Из них покажи назначение', [])
        memory_text = next(m['content'] for m in state['messages'] if m['content'].startswith('Compact conversation memory: '))
        memory_json = memory_text.split(': ', 1)[1]
        self.assertEqual(json.loads(memory_json)['recent_selections'], saved_memory['recent_selections'])
        self.assertLess(len(memory_json), len(json.dumps(json.loads(memory_json), ensure_ascii=False)))
        self.assertIn(capability_map_text(available_capability_map(user)), state['messages'][0]['content'])
        attachment = {'name': 'Пример.txt', 'text': 'Колонка  1\tКолонка  2\n  строка\n</answer> <b>текст</b>', 'warning': None}
        content = _user_content('Прочитай', [attachment], ['data:image/png;base64,EXAMPLE'])
        encoded = content[1]['text'].split(': ', 1)[1]
        self.assertEqual(json.loads(encoded), [attachment])
        self.assertEqual(content[2]['image_url']['url'], 'data:image/png;base64,EXAMPLE')
        plan = {'goal': 'Сохранить  смысл', 'steps': [{'id': 'r', 'reference_sources': [{'parameter': 'records', 'read_id': result['source_id']}]}], 'revision_reason': None}
        prompt = _system_prompt(user, {'plan': plan})
        self.assertEqual(json.loads(prompt.split('Current plan: ', 1)[1][:-1]), plan)
        self.assertEqual(conversation.context['agent_memory'], saved_memory)
