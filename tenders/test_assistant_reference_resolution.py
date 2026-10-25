from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from cash.models import CashTransaction
from tender_selection.models import Tender
from .assistant_conversations import create_conversation, respond
from .assistant_tools import (
    AssistantTool, available_capability_map, capability_map_text, execute_tool, register_tool,
)
from .models import Counterparty, CounterpartyKnowledgeVersion, TenderEstimate
from .test_assistant_conversations import function_call, planned_call, remember_reference


class AssistantReferenceResolutionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("resolution", "resolution@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.tender = Tender.objects.create(purchase_number="M113-001", title="Тестовая поставка",
                                           review=Tender.INTERESTING)
        self.snapshots = []
        gateway = patch("tenders.assistant_agent._gateway")
        self.gateway = gateway.start()
        self.addCleanup(gateway.stop)
        preflight = patch("tenders.assistant_agent.preflight")
        preflight.start()
        self.addCleanup(preflight.stop)

    def answers(self, answers):
        stream = iter(answers)
        def gateway(messages, _tools):
            self.snapshots.append(deepcopy(messages))
            return next(stream)
        self.gateway.side_effect = gateway

    @staticmethod
    def plan(goal="Найти запись и подготовить действие", *, revision=None, read=True, action=True):
        steps = []
        if read:
            steps.append({"id": "find", "capability_id": "read:tender_selection.tenders",
                          "objective": "Найти запись", "expected_result": "Проверенный ID"})
        if action:
            steps.append({"id": "prepare", "capability_id": "tender.send_to_calculation",
                          "objective": "Подготовить действие", "expected_result": "Карточка подтверждения"})
        return planned_call("tender.send_to_calculation", goal, revision_reason=revision, steps=steps,
                            sources={"tender_id": "find" if read else "verified"} if action else None)

    def read(self, search):
        return function_call("read_tender_selection_tenders", {"plan": {
            "view": None, "search": search, "fields": ["id", "purchase_number", "title"], "order_by": ["id"],
        }})

    def remember(self, entity, identifier):
        remember_reference(self.conversation, self.user, entity, identifier)

    def test_plan_map_includes_live_reference_metadata_for_an_unknown_action(self):
        from . import assistant_tools
        identifier = "extension." + uuid4().hex
        parameter = "object_" + uuid4().hex
        register_tool(AssistantTool(identifier, "Произвольное действие", "Тест", {
            parameter: {"type": "integer", "required": True,
                        "reference": {"model": "tenders.Counterparty", "label": "name"}},
        }, False, True, True, lambda user, payload: payload, areas=("tenders",)))
        try:
            entries = [c for area in available_capability_map(self.user) for c in area["capabilities"]]
            action = next(c for c in entries if c["id"] == identifier)
            self.assertEqual(action["references"], [{"parameter": parameter, "model": "tenders.Counterparty",
                                                     "required": True, "read": ["read:tenders.counterparties"]}])
            self.assertIn(parameter, capability_map_text(available_capability_map(self.user)))
            self.assertNotIn("input_schema", action)
            other = get_user_model().objects.create_user("resolution-other")
            self.assertNotIn(identifier, capability_map_text(available_capability_map(other)))
        finally:
            assistant_tools._TOOLS.pop(identifier)

    def test_number_read_action_confirmation_uses_actual_id(self):
        self.answers([self.plan(), self.read(self.tender.purchase_number),
                      function_call("tender_send_to_calculation", {"tender_id": self.tender.pk})])
        card = respond(self.conversation, self.user, "Подготовь передачу закупки M113-001 в расчёт")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(card["data"]["arguments"], {"tender_id": self.tender.pk})
        self.assertIn('"parameter":"tender_id"', self.snapshots[0][0]["content"])
        self.assertIn('"read":["read:tender_selection.tenders"]', self.snapshots[0][0]["content"])
        self.assertFalse(TenderEstimate.objects.exists())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, Tender.NEW)

    def test_counterparty_name_is_resolved_for_another_real_action(self):
        party = Counterparty.objects.create(name="Мастерская Тест", created_by=self.user)
        version = CounterpartyKnowledgeVersion.objects.create(counterparty=party, created_by=self.user)
        self.remember("tenders.CounterpartyKnowledgeVersion", version.pk)
        self.answers([planned_call("provider.knowledge_remove", sources={"counterparty_id": "find", "knowledge_version_id": "verified"}, steps=[
            {"id": "find", "capability_id": "read:tenders.counterparties", "objective": "Найти контрагента",
             "expected_result": "Проверенный ID"},
            {"id": "prepare", "capability_id": "provider.knowledge_remove", "objective": "Подготовить удаление черновика",
             "expected_result": "Карточка"},
        ]), function_call("read_tenders_counterparties", {"plan": {"search": party.name, "fields": ["id", "name"]}}),
            function_call("provider_knowledge_remove", {"counterparty_id": party.pk, "knowledge_version_id": version.pk})])
        card = respond(self.conversation, self.user, "Подготовь удаление черновой версии знаний Мастерской Тест")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(card["data"]["arguments"]["counterparty_id"], party.pk)
        self.assertEqual(CounterpartyKnowledgeVersion.objects.filter(pk=version.pk).count(), 1)
        version.refresh_from_db()
        self.assertEqual(version.status, CounterpartyKnowledgeVersion.STATUS_DRAFT)

    def test_verified_context_id_and_new_record_need_no_read(self):
        self.remember("tender_selection.Tender", self.tender.pk)
        self.answers([self.plan(read=False), function_call("tender_send_to_calculation", {"tender_id": self.tender.pk})])
        card = respond(self.conversation, self.user, "Подготовь передачу выбранного тендера")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(self.gateway.call_count, 2)
        fresh = create_conversation(self.user)
        self.answers([planned_call("cash.transaction_create"), function_call("cash_transaction_create", {
            "operation_date": "2026-10-24", "account": "cash", "direction": "expense", "amount": 175, "reason": "Пробная бумага",
        })])
        card = respond(fresh, self.user, "Подготовь расход наличными175 за24 октября2026, Пробная бумага")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertFalse(CashTransaction.objects.exists())
        self.assertFalse(TenderEstimate.objects.exists())

    def test_absence_is_read_and_ambiguity_waits_for_business_choice(self):
        self.answers([self.plan(), self.read("M113-ABSENT"),
            function_call("assistant_ask", {"question": "По указанным признакам записей нет. Уточни название?"})])
        result = respond(self.conversation, self.user, "Найди закупку M113-ABSENT и подготовь передачу")[-1]
        reads = [s for s in result["data"]["steps"] if s["tool_id"].startswith("read:")]
        self.assertEqual(reads[-1]["result"]["total_count"], 0)
        self.assertFalse(TenderEstimate.objects.exists())

        second = Tender.objects.create(purchase_number="M113-002", title=self.tender.title, review=Tender.INTERESTING)
        conversation = create_conversation(self.user)
        self.answers([self.plan(), self.read(self.tender.title),
            function_call("assistant_ask", {"question": "Выбрать первую или вторую закупку?"}),
            function_call("tender_send_to_calculation", {"tender_id": second.pk})])
        respond(conversation, self.user, "Подготовь передачу Тестовой поставки")
        self.assertNotIn("waiting_confirmation", conversation.context["agent_run"])
        card = respond(conversation, self.user, "Вторую закупку")[-1]
        self.assertEqual(card["data"]["arguments"], {"tender_id": second.pk})
        self.assertFalse(TenderEstimate.objects.exists())

    def test_repeated_reference_errors_stop_at_the_existing_execution_limit(self):
        from .assistant_agent import MAX_TOOL_CALLS
        from .services import TenderAIError
        self.remember("tender_selection.Tender", self.tender.pk)
        self.answers([self.plan(read=False)] + [
            function_call("tender_send_to_calculation", {"tender_id": 0}) for _ in range(MAX_TOOL_CALLS)
        ])
        with self.assertRaisesRegex(TenderAIError, "Достигнут лимит"):
            respond(self.conversation, self.user, "Подготовь передачу закупки M113-001")
        self.assertEqual(self.gateway.call_count, MAX_TOOL_CALLS + 1)
        self.assertFalse(TenderEstimate.objects.exists())

    def test_invalid_reference_restores_plan_metadata_and_model_revises_without_user_id(self):
        self.remember("tender_selection.Tender", self.tender.pk)
        self.answers([self.plan(read=False), function_call("tender_send_to_calculation", {"tender_id": 0}),
            self.plan(revision="Ссылка не разрешена; нужен поиск записи"), self.read(self.tender.purchase_number),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk})])
        card = respond(self.conversation, self.user, "Подготовь передачу закупки M113-001")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(card["data"]["arguments"], {"tender_id": self.tender.pk})
        self.assertIn("existing references:", self.snapshots[2][0]["content"])
        self.assertIn("not a search result", self.snapshots[2][0]["content"])
        self.assertEqual(self.conversation.context["agent_run"]["plan"]["version"], 2)
        self.assertFalse(TenderEstimate.objects.exists())
