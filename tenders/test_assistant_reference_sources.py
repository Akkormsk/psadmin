"""Real READ/plan/confirmation checks; Gateway is scripted, never paid."""
import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from tender_selection.models import Tender
from .assistant_agent import _new_state, _normalise_plan, _validate_reference_sources
from .assistant_conversations import create_conversation, respond
from .assistant_tools import AssistantReferenceError, available_tools, AssistantTool, register_tool
from .models import TenderEstimate
from .test_assistant_conversations import function_call, planned_call, remember_reference


class AssistantReferenceSourceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("source", "source@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.tender = Tender.objects.create(purchase_number="SOURCE-001", title="Первый объект", review=Tender.INTERESTING)
        self.other = Tender.objects.create(purchase_number="SOURCE-002", title="Второй объект", review=Tender.INTERESTING)
        patcher = patch("tenders.assistant_agent._gateway")
        self.gateway = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("tenders.assistant_agent.preflight")
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, read="find", source=None, revision=None, keep=None):
        steps = list(keep or [])
        if read:
            steps.append({"id": read, "capability_id": "read:tender_selection.tenders",
                          "objective": read, "expected_result": "Найденный объект"})
        steps.append({"id": "act", "capability_id": "tender.send_to_calculation",
                      "objective": "Передать выбранный объект", "expected_result": "Расчёт"})
        return planned_call("tender.send_to_calculation", steps=steps, revision_reason=revision,
                            sources={"tender_id": source or read})

    def read(self, search):
        return function_call("read_tender_selection_tenders", {"plan": {
            "search": search, "fields": ["id", "purchase_number", "title"],
        }})

    def action(self, identifier=None):
        return function_call("tender_send_to_calculation", {"tender_id": identifier or self.tender.pk})

    def ask(self, text="Уточни объект?"):
        return function_call("assistant_ask", {"question": text})

    def test_required_source_is_enforced_before_any_business_call(self):
        self.gateway.side_effect = [planned_call("tender.send_to_calculation"), self.plan(), self.ask()]
        result = respond(self.conversation, self.user, "Передай объект в расчёт")[-1]
        state = self.conversation.context["agent_run"]
        self.assertEqual(state["status"], "WAITING_USER")
        self.assertFalse(any(s.get("plan_step_id") for s in state["steps"]))
        self.assertNotEqual(result["kind"], "confirmation_required")
        self.assertIn("обязательного reference", json.dumps(state["messages"], ensure_ascii=False))

    def test_empty_new_read_cannot_use_old_id_or_finish_and_waits_until_user_cancels(self):
        remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        self.gateway.side_effect = [self.plan(), self.read("ABSENT-777"), self.action(),
                                    function_call("assistant_finish", {"answer": "Не найдено"}), self.ask(),
                                    function_call("assistant_cancel", {"reason": "Пользователь согласился закрыть", "answer": "Задача закрыта, ничего не изменено", "new_task": False})]
        respond(self.conversation, self.user, "Найди ABSENT-777 и передай в расчёт")
        state = self.conversation.context["agent_run"]
        self.assertEqual(state["status"], "WAITING_USER")
        self.assertNotIn("waiting_confirmation", state)
        self.assertFalse(TenderEstimate.objects.exists())
        self.assertIn("не содержит выбранный объект", json.dumps(state["messages"], ensure_ascii=False))
        respond(self.conversation, self.user, "Да, закрой задачу")
        self.assertNotIn("agent_run", self.conversation.context)

    def test_empty_read_cannot_be_hidden_by_removing_action_from_plan(self):
        read_only = planned_call("read:tender_selection.tenders", steps=[{
            "id": "find", "capability_id": "read:tender_selection.tenders", "objective": "find", "expected_result": "Найденный объект",
        }], revision_reason="Ничего не найдено")
        self.gateway.side_effect = [self.plan(), self.read("ABSENT-777"), read_only, self.ask()]
        respond(self.conversation, self.user, "Найди и передай")
        self.assertEqual(len(self.conversation.context["agent_run"]["plan"]["steps"]), 2)
        self.assertIn("Пустой READ не отменяет ACTION", json.dumps(self.conversation.context, ensure_ascii=False))

    def test_found_object_has_correct_card_and_payload_unchanged(self):
        self.gateway.side_effect = [self.plan(), self.read(self.other.purchase_number), self.action(self.other.pk)]
        card = respond(self.conversation, self.user, "Найди второй объект и подготовь передачу")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(card["data"]["arguments"], {"tender_id": self.other.pk})
        self.assertIn(self.other.purchase_number, str(card["data"]["action"]))
        self.assertFalse(TenderEstimate.objects.exists())

    def test_retained_source_reuses_verified_object_without_read(self):
        source = remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        self.gateway.side_effect = [self.plan(read=None, source=source), self.action()]
        card = respond(self.conversation, self.user, "Передай ранее выбранный объект")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(self.gateway.call_count, 2)
        self.assertFalse(any(s["tool_id"].startswith("read:") for s in self.conversation.context["agent_run"]["steps"]))

    def test_ambiguous_result_requires_clarification_then_accepts_choice(self):
        self.gateway.side_effect = [self.plan(), self.read("объект"), self.action(), self.ask("Первый или второй?"), self.action(self.other.pk)]
        result = respond(self.conversation, self.user, "Передай объект")[-1]
        self.assertNotEqual(result["kind"], "confirmation_required")
        card = respond(self.conversation, self.user, "Второй")[-1]
        self.assertEqual(card["data"]["arguments"]["tender_id"], self.other.pk)

    def test_dialogue_can_explain_and_replan_search_without_losing_history(self):
        initial = {"id": "find", "capability_id": "read:tender_selection.tenders", "objective": "find", "expected_result": "Найденный объект"}
        self.gateway.side_effect = [self.plan(), self.read("ABSENT-777"), self.ask("Ничего не нашёл. Что уточним?"),
            self.ask("Искал по указанному номеру. Какие условия изменить?"),
            self.plan(read="find2", keep=[initial], revision="Пользователь уточнил искомый объект"),
            self.read(self.other.purchase_number), self.action(self.other.pk)]
        respond(self.conversation, self.user, "Найди ABSENT-777 и подготовь действие")
        task_id = self.conversation.context["agent_run"]["task_id"]
        explanation = respond(self.conversation, self.user, "Где ты искал?")[-1]
        self.assertIn("Искал по указанному номеру", explanation["text"])
        card = respond(self.conversation, self.user, "Я имел в виду SOURCE-002")[-1]
        state = self.conversation.context["agent_run"]
        self.assertEqual(state["task_id"], task_id)
        self.assertEqual(state["plan"]["version"], 2)
        self.assertEqual(len(state["plan"]["carried_execution"]), 1)
        self.assertEqual(card["data"]["arguments"]["tender_id"], self.other.pk)
        self.assertEqual(len([s for s in state["steps"] if s["tool_id"].startswith("read:")]), 2)

    def test_arbitrary_action_and_parameter_use_same_validation(self):
        from . import assistant_tools
        identifier, parameter = "extension." + uuid4().hex, "ref_" + uuid4().hex
        register_tool(AssistantTool(identifier, "Действие", "Тест", {
            parameter: {"type": "integer", "required": True, "reference": {"model": "tender_selection.Tender", "label": "title"}},
        }, False, True, True, lambda user, payload: payload, areas=("tenders",)))
        self.addCleanup(lambda: assistant_tools._TOOLS.pop(identifier))
        source = remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        state = _new_state(self.conversation, self.user, "Сделай", [])
        state["plan"] = _normalise_plan({"goal": "Сделай", "steps": [{"id": "arbitrary", "capability_id": identifier, "objective": "Действие", "reference_sources": [{"parameter": parameter, "read_id": source}]}]}, self.user, state=state)
        state["active_plan_step_id"] = "arbitrary"
        tool = next(item for item in available_tools(self.user) if item["id"] == identifier)
        payload = {parameter: self.tender.pk}
        _validate_reference_sources(tool, payload, state)
        self.assertEqual(payload, {parameter: self.tender.pk})
        with self.assertRaises(AssistantReferenceError):
            _validate_reference_sources(tool, {parameter: self.other.pk}, state)

    def test_unknown_forward_and_wrong_model_sources_are_rejected(self):
        source = remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        state = _new_state(self.conversation, self.user, "Сделай", [])
        for candidate in ("missing", "future"):
            with self.subTest(candidate=candidate), self.assertRaises(AssistantReferenceError):
                _normalise_plan({"goal": "Действие", "steps": [{"id": "act", "capability_id": "tender.send_to_calculation", "objective": "Действие", "reference_sources": [{"parameter": "tender_id", "read_id": candidate}]}]}, self.user, state=state)
        state["memory"]["recent_selections"][0]["entity"] = "tenders.Counterparty"
        with self.assertRaises(AssistantReferenceError):
            _normalise_plan({"goal": "Действие", "steps": [{"id": "act", "capability_id": "tender.send_to_calculation", "objective": "Действие", "reference_sources": [{"parameter": "tender_id", "read_id": source}]}]}, self.user, state=state)

    def test_source_change_requires_explicit_plan_revision(self):
        first = remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk, "first")
        second = remember_reference(self.conversation, self.user, "tender_selection.Tender", self.other.pk, "second")
        state = _new_state(self.conversation, self.user, "Сделай", [])
        def declaration(source, reason=None):
            return {"goal": "Сделай", "revision_reason": reason, "steps": [{"id": "act", "capability_id": "tender.send_to_calculation", "objective": "Действие", "reference_sources": [{"parameter": "tender_id", "read_id": source}]}]}
        state["plan"] = _normalise_plan(declaration(first), self.user, state=state)
        from .assistant_agent import AgentProtocolError
        with self.assertRaises(AgentProtocolError):
            _normalise_plan(declaration(second), self.user, state["plan"], state)
        revised = _normalise_plan(declaration(second, "Пользователь выбрал другой объект"), self.user, state["plan"], state)
        self.assertEqual(revised["steps"][0]["reference_sources"][0]["read_id"], second)

    def test_real_read_source_survives_finish_and_next_task(self):
        self.gateway.side_effect = [planned_call("read:tender_selection.tenders"), self.read(self.tender.purchase_number),
                                   function_call("assistant_finish", {"answer": "Найден первый объект"})]
        respond(self.conversation, self.user, "Найди SOURCE-001")
        source = self.conversation.context["agent_memory"]["recent_selections"][-1]["source_id"]
        self.gateway.side_effect = [self.plan(read=None, source=source), self.action()]
        card = respond(self.conversation, self.user, "Передай его в расчёт")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(self.gateway.call_count, 5)
        self.assertIn(source, json.dumps(self.conversation.context["agent_run"]["plan"]))

    def test_failed_read_does_not_authorize_old_reference(self):
        remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        self.gateway.side_effect = [self.plan(), function_call("read_tender_selection_tenders", {"plan": {"view": "invalid"}}), self.action(), self.ask()]
        result = respond(self.conversation, self.user, "Найди другой объект и передай")[-1]
        state = self.conversation.context["agent_run"]
        self.assertNotEqual(result["kind"], "confirmation_required")
        self.assertNotIn("waiting_confirmation", state)
        self.assertFalse(TenderEstimate.objects.exists())

    def test_optional_reference_requires_source_only_when_supplied(self):
        from . import assistant_tools
        identifier, parameter = "extension." + uuid4().hex, "ref_" + uuid4().hex
        register_tool(AssistantTool(identifier, "Действие", "Тест", {
            parameter: {"type": "integer?", "required": False, "reference": {"model": "tender_selection.Tender", "label": "title"}},
        }, False, True, True, lambda user, payload: payload, areas=("tenders",)))
        self.addCleanup(lambda: assistant_tools._TOOLS.pop(identifier))
        state = _new_state(self.conversation, self.user, "Сделай", [])
        state["plan"] = _normalise_plan({"goal": "Сделай", "steps": [{"id": "act", "capability_id": identifier, "objective": "Действие"}]}, self.user, state=state)
        state["active_plan_step_id"] = "act"
        tool = next(item for item in available_tools(self.user) if item["id"] == identifier)
        _validate_reference_sources(tool, {}, state)
        with self.assertRaises(AssistantReferenceError):
            _validate_reference_sources(tool, {parameter: self.tender.pk}, state)
