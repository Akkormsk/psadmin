from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender
from .assistant_agent import _new_state, _record_execution, reply
from .assistant_conversations import add_assistant_message, create_conversation
from .assistant_tools import execute_tool
from .models import TenderEstimate
from .test_assistant_conversations import function_call, planned_call


class AssistantReadCacheTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("cache", "cache@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.tender = Tender.objects.create(purchase_number="CACHE-001", title="Тестовая бумага")
        self.read_filters = [{"field": "tender.id", "op": "eq", "value": self.tender.pk}]
        self.client.force_login(self.user)
        gateway = patch("tenders.assistant_agent._gateway")
        self.gateway = gateway.start()
        self.addCleanup(gateway.stop)
        preflight = patch("tenders.assistant_agent.preflight")
        preflight.start()
        self.addCleanup(preflight.stop)

    def read_call(self):
        return function_call("read_tenders_tender_estimates", {"plan": {
            "fields": ["id", "name"],
            "filters": self.read_filters,
            "order_by": ["-id"], "limit": 10,
        }})

    def read_turn(self):
        self.gateway.side_effect = [planned_call("read:tenders.tender_estimates"), self.read_call(),
                                    function_call("assistant_finish", {"answer": "Проверено"})]
        result = reply(self.conversation, self.user, "Покажи расчёты")
        return result["data"]["steps"][-1]["result"]

    def confirm(self):
        event = self.conversation.feedback_events.filter(payload__kind="confirmation_required").latest("pk")
        response = self.client.post(reverse("assistant_conversation_action_confirm", args=[
            self.conversation.pk, event.pk,
        ]), {"accept": "yes"})
        self.assertEqual(response.status_code, 200)
        self.conversation.refresh_from_db()
        return response.json()["messages"][-1]

    def test_empty_read_then_confirmed_action_then_read_in_same_run(self):
        steps = [{"id": name, "capability_id": capability, "objective": name, "expected_result": name}
                 for name, capability in [("before", "read:tenders.tender_estimates"),
                                          ("create", "tender.send_to_calculation"),
                                          ("after", "read:tenders.tender_estimates")]]
        self.gateway.side_effect = [
            planned_call("read:tenders.tender_estimates", steps=steps), self.read_call(),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk}),
            self.read_call(), function_call("assistant_finish", {"answer": "Создано и проверено"}),
        ]
        card = reply(self.conversation, self.user, "Проверь расчёт, создай и перечитай")
        add_assistant_message(self.conversation, card["text"], card["kind"], card["data"])
        self.assertFalse(TenderEstimate.objects.exists())

        result = self.confirm()

        estimate = TenderEstimate.objects.get(tender=self.tender)
        reads = [s["result"] for s in result["data"]["steps"] if s["tool_id"].startswith("read:")]
        self.assertEqual(reads[0]["total_count"], 0)
        self.assertEqual(reads[1]["records"][0]["id"], estimate.pk)
        self.assertNotEqual(reads[1]["source_scope"], "cached complete result")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, Tender.PUSHED)
        self.assertEqual(TenderEstimate.objects.count(), 1)

    def test_action_invalidates_saved_empty_selection_and_preserves_object_reference(self):
        self.assertEqual(self.read_turn()["total_count"], 0)
        focus = {"entity": "tender_selection.Tender", "id": self.tender.pk}
        self.conversation.context["agent_memory"]["active_focus"] = focus
        self.conversation.save(update_fields=["context"])
        self.gateway.side_effect = [planned_call("tender.send_to_calculation"),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk}),
            function_call("assistant_finish", {"answer": "Создано"})]
        card = reply(self.conversation, self.user, "Передай выбранный тендер в расчёт")
        add_assistant_message(self.conversation, card["text"], card["kind"], card["data"])
        self.confirm()

        state = _new_state(self.conversation, self.user, "Покажи созданный расчёт", [])
        self.assertNotIn("read_cache", state)
        self.assertEqual(state["memory"]["active_focus"], focus)
        self.assertEqual(state["memory"]["recent_selections"][0]["record_ids"], [])
        result = self.read_turn()
        self.assertEqual(result["records"][0]["id"], TenderEstimate.objects.get().pk)

    def test_unchanged_read_reuses_cache_and_other_user_has_no_selection(self):
        estimate = TenderEstimate.objects.create(owner=self.user, tender=self.tender,
            tender_number=self.tender.purchase_number, name="Проверенный расчёт")
        self.read_filters = [{"field": "id", "op": "eq", "value": estimate.pk}]
        self.read_turn()
        with patch("tenders.assistant_agent.execute_tool", wraps=execute_tool) as executor:
            result = self.read_turn()
        executor.assert_not_called()
        self.assertEqual(result["source_scope"], "cached complete result")
        self.assertEqual(result["records"][0]["id"], estimate.pk)
        other = get_user_model().objects.create_superuser("cache-other", "other@example.test", "password")
        state = _new_state(create_conversation(other), other, "Покажи расчёты", [])
        self.assertNotIn("read_cache", state)
        self.assertEqual(state["memory"]["recent_selections"], [])

    def test_cancelled_or_failed_execution_does_not_invalidate_read(self):
        self.read_turn()
        state = _new_state(self.conversation, self.user, "Продолжим", [])
        state["plan"] = {"version": 1}
        for status, error in [("cancelled", "Отмена"), ("failed", "Ошибка")]:
            with self.subTest(status=status):
                candidate = deepcopy(state)
                _record_execution(candidate, "tender.send_to_calculation", None, status=status, error=error)
                self.assertEqual(candidate["read_cache"], state["read_cache"])
                self.assertFalse(TenderEstimate.objects.exists())

    def test_write_preserves_selected_ids_but_refreshes_related_model_data(self):
        from .assistant_agent import _finish_turn
        selection = execute_tool("read", self.user, {"plan": {
            "entity": "tender_selection.Tender", "fields": ["id", "title", "status"],
            "filters": [{"field": "id", "op": "eq", "value": self.tender.pk}],
        }})
        _finish_turn(self.conversation, {"steps": [
            {"tool_id": "read:tender_selection.tenders", "result": selection},
        ]})
        state = _new_state(self.conversation, self.user, "Передай выбранный тендер в расчёт", [])
        state["plan"] = {"version": 1}
        result = execute_tool("tender.send_to_calculation", self.user,
                              {"tender_id": self.tender.pk}, confirmed=True)
        _record_execution(state, "tender.send_to_calculation", result)
        _finish_turn(self.conversation, state)
        self.conversation.refresh_from_db()

        restored = _new_state(self.conversation, self.user, "Покажи этот тендер", [])
        self.assertNotIn("read_cache", restored)
        self.assertEqual(restored["memory"]["recent_selections"][0]["record_ids"], [self.tender.pk])
        self.assertEqual(restored["memory"]["active_focus"]["id"], self.tender.pk)
        fresh = execute_tool("read", self.user, {"plan": {
            "entity": "tender_selection.Tender", "fields": ["id", "status"],
            "filters": [{"field": "id", "op": "eq", "value": self.tender.pk}],
        }})
        self.assertEqual(fresh["records"][0]["status"], Tender.PUSHED)
        self.assertEqual(TenderEstimate.objects.filter(tender=self.tender).count(), 1)
