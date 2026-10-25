import json
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tender_selection.models import Tender
from .assistant_conversations import create_conversation, respond
from .models import TenderEstimate
from .test_assistant_conversations import function_call, planned_call


class AssistantPageContextTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("page-context", "page@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.tender = Tender.objects.create(purchase_number="PAGE-001", title="Открытая карточка", review=Tender.INTERESTING)
        self.snapshots = []
        gateway = patch("tenders.assistant_agent._gateway")
        self.gateway = gateway.start()
        self.addCleanup(gateway.stop)
        preflight = patch("tenders.assistant_agent.preflight")
        preflight.start()
        self.addCleanup(preflight.stop)

    def answers(self, replies):
        stream = iter(replies)
        def gateway(messages, tools):
            self.snapshots.append(deepcopy(messages))
            return next(stream)
        self.gateway.side_effect = gateway

    def context(self, identifier=None):
        identifier = identifier or self.tender.pk
        return {"page": f"/tender-selection/{identifier}/", "label": "Тендеры", "tender_id": str(identifier)}

    def test_current_page_reaches_first_request_and_exact_read_prepares_card(self):
        steps = [{"id": "find", "capability_id": "read:tender_selection.tenders", "objective": "Прочитать открытый объект"},
                 {"id": "act", "capability_id": "tender.send_to_calculation", "objective": "Подготовить карточку"}]
        self.answers([planned_call("tender.send_to_calculation", steps=steps, sources={"tender_id": "find"}),
            function_call("read_tender_selection_tenders", {"plan": {"view": None, "fields": ["id", "purchase_number", "title"], "filters": [{"field": "id", "op": "eq", "value": self.tender.pk}]}}),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk})])
        context = self.context()
        card = respond(self.conversation, self.user, "Подготовь передачу этого открытого тендера", context=context)[-1]
        self.assertIn(json.dumps(context, ensure_ascii=False), self.snapshots[0][0]["content"])
        self.assertEqual(card["kind"], "confirmation_required")
        self.assertEqual(card["data"]["arguments"], {"tender_id": self.tender.pk})
        self.assertFalse(TenderEstimate.objects.exists())

    def test_navigation_updates_context_during_same_task_and_missing_context_clears_it(self):
        self.answers([planned_call("read:tender_selection.tenders"), function_call("assistant_ask", {"question": "Что показать?"}),
                      function_call("assistant_ask", {"question": "Показать новую карточку?"}),
                      function_call("assistant_ask", {"question": "Какой объект выбрать?"})])
        respond(self.conversation, self.user, "Посмотри этот объект", context=self.context())
        task_id = self.conversation.context["agent_run"]["task_id"]
        other = Tender.objects.create(purchase_number="PAGE-002", title="Новая карточка")
        second = self.context(other.pk)
        respond(self.conversation, self.user, "Теперь открыт другой", context=second)
        self.assertEqual(self.conversation.context["agent_run"]["task_id"], task_id)
        self.assertIn(json.dumps(second, ensure_ascii=False), self.snapshots[-1][0]["content"])
        self.assertNotIn(self.context()["page"], self.snapshots[-1][0]["content"])
        respond(self.conversation, self.user, "Я ушёл со страницы")
        self.assertNotIn("Current page context", self.snapshots[-1][0]["content"])
        self.assertEqual(self.conversation.context["agent_run"]["page_context"], {})

    def test_page_id_does_not_replace_verified_read_source(self):
        steps = [{"id": "read", "capability_id": "read:tender_selection.tenders", "objective": "Прочитать объект"},
                 {"id": "act", "capability_id": "tender.send_to_calculation", "objective": "Подготовить карточку"}]
        self.answers([planned_call("tender.send_to_calculation", steps=steps, sources={"tender_id": "read"}),
            function_call("read_tender_selection_tenders", {"plan": {"view": None, "search": "ABSENT-PAGE-999", "fields": ["id"]}}),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk}),
            function_call("assistant_ask", {"question": "Поиск пустой. Уточни объект?"})])
        result = respond(self.conversation, self.user, "Подготовь действие", context=self.context())[-1]
        self.assertNotEqual(result["kind"], "confirmation_required")
        self.assertIn("не содержит выбранный объект", json.dumps(self.conversation.context, ensure_ascii=False))
        self.assertFalse(TenderEstimate.objects.exists())

    def test_context_is_bounded_and_not_inherited_from_conversation_creation(self):
        self.conversation.context["business_context"] = self.context()
        self.conversation.save(update_fields=["context"])
        self.answers([planned_call("read:tender_selection.tenders"), function_call("assistant_ask", {"question": "Какой объект?"})])
        respond(self.conversation, self.user, "Что открыто?", context={"page": "/cash/", "label": "x" * 1000, "secret": "do not include"})
        context = self.conversation.context["agent_run"]["page_context"]
        self.assertEqual(set(context), {"page", "label"})
        self.assertEqual(len(context["label"]), 240)
        self.assertNotIn(self.context()["page"], self.snapshots[0][0]["content"])
        self.assertNotIn("do not include", self.snapshots[0][0]["content"])
