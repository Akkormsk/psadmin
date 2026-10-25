from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from cash.models import CashTransaction
from tender_selection.models import Tender
from .assistant_conversations import create_conversation, respond
from .assistant_tools import AssistantToolError, available_tools, confirmation_arguments, openai_functions, validate_tool_call
from .models import TenderEstimate
from .test_assistant_conversations import function_call, planned_call, remember_reference


class AssistantConfirmationSafetyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("confirm", "confirm@example.test", "password")
        self.conversation = create_conversation(self.user)
        self.tender = Tender.objects.create(purchase_number="CONFIRM-001", title="Тестовая бумага")
        remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        self.client.force_login(self.user)
        gateway = patch("tenders.assistant_agent._gateway")
        self.gateway = gateway.start()
        self.addCleanup(gateway.stop)
        preflight = patch("tenders.assistant_agent.preflight")
        preflight.start()
        self.addCleanup(preflight.stop)

    def prepare(self):
        remember_reference(self.conversation, self.user, "tender_selection.Tender", self.tender.pk)
        self.gateway.side_effect = [planned_call("tender.send_to_calculation", sources={"tender_id": "verified"}),
            function_call("tender_send_to_calculation", {"tender_id": self.tender.pk}),
            function_call("assistant_finish", {"answer": "Готово"})]
        card = respond(self.conversation, self.user, "Передай тендер в расчёт")[-1]
        self.assertEqual(card["kind"], "confirmation_required")
        return card

    def url(self, card):
        return reverse("assistant_conversation_action_confirm", args=[self.conversation.pk, card["id"]])

    def test_invalid_references_never_produce_a_confirmation_card(self):
        for value in (372200255926000168, "1", True):
            with self.subTest(value=value), self.assertRaises(AssistantToolError):
                validate_tool_call("tender.send_to_calculation", self.user, {"tender_id": value})
        with self.assertRaises(AssistantToolError):
            validate_tool_call("provider.create", self.user, {"name": "Тест", "stage_ids": [999999]})
        self.gateway.side_effect = [planned_call("tender.send_to_calculation", sources={"tender_id": "verified"}),
            function_call("tender_send_to_calculation", {"tender_id": 372200255926000168}),
            function_call("assistant_ask", {"question": "Какой тендер нужно найти?"})]
        result = respond(self.conversation, self.user, "Передай закупку в расчёт")[-1]
        self.assertNotEqual(result["kind"], "confirmation_required")
        self.assertFalse(self.conversation.feedback_events.filter(payload__kind="confirmation_required").exists())
        self.assertFalse(TenderEstimate.objects.exists())

    def test_button_executes_once_and_independent_same_action_remains_allowed(self):
        card = self.prepare()
        self.assertFalse(TenderEstimate.objects.exists())
        self.assertEqual(self.client.post(self.url(card), {"accept": "yes"}).status_code, 200)
        estimate = TenderEstimate.objects.get(tender=self.tender)
        self.assertEqual(estimate.owner_id, self.user.pk)
        self.assertEqual(self.client.post(self.url(card), {"accept": "yes"}).status_code, 409)
        self.conversation.refresh_from_db()
        self.tender = Tender.objects.create(purchase_number="CONFIRM-002", title="Второй тендер")
        second = self.prepare()
        self.assertEqual(self.client.post(self.url(second), {"accept": "yes"}).status_code, 200)
        self.assertEqual(TenderEstimate.objects.count(), 2)
        self.assertEqual(TenderEstimate.objects.filter(tender=self.tender).count(), 1)

    def test_corrected_amount_invalidates_old_card_and_writes_only_new_values(self):
        args = {"operation_date": "2026-10-24", "account": "cash", "direction": "expense",
                "amount": 175, "reason": "Тестовая бумага"}
        self.gateway.side_effect = [planned_call("cash.transaction_create"),
            function_call("cash_transaction_create", args),
            function_call("cash_transaction_create", {**args, "amount": 200}),
            function_call("assistant_finish", {"answer": "Записано"})]
        old = respond(self.conversation, self.user, "Расход наличными175 за24 октября2026, Тестовая бумага")[-1]
        new = respond(self.conversation, self.user, "Поменяй сумму на200")[-1]
        self.assertEqual(self.client.post(self.url(old), {"accept": "yes"}).status_code, 409)
        self.assertFalse(CashTransaction.objects.exists())
        self.assertEqual(self.client.post(self.url(new), {"accept": "yes"}).status_code, 200)
        transaction = CashTransaction.objects.get()
        self.assertEqual(transaction.amount, 200)
        self.assertEqual(transaction.reason, args["reason"])
        self.assertEqual(transaction.account, args["account"])
        self.assertEqual(transaction.direction, args["direction"])
        self.assertEqual(transaction.operation_date.isoformat(), args["operation_date"])
        self.assertEqual(self.client.post(self.url(new), {"accept": "yes"}).status_code, 409)
        self.assertEqual(CashTransaction.objects.count(), 1)

    def test_other_user_cannot_confirm_and_cancellation_never_writes(self):
        card = self.prepare()
        other = get_user_model().objects.create_user("other-confirm", password="password")
        with self.assertRaises(AssistantToolError):
            validate_tool_call("tender.send_to_calculation", other, {"tender_id": self.tender.pk})
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url(card), {"accept": "yes"}).status_code, 404)
        self.client.force_login(self.user)
        self.gateway.side_effect = [function_call("assistant_ask", {"question": "Что изменить?"})]
        self.assertEqual(self.client.post(self.url(card), {"accept": "no"}).status_code, 200)
        self.assertEqual(self.client.post(self.url(card), {"accept": "yes"}).status_code, 409)
        self.assertFalse(TenderEstimate.objects.exists())
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, Tender.NEW)

    def test_deleted_reference_is_revalidated_when_the_button_is_pressed(self):
        card = self.prepare()
        self.tender.delete()
        response = self.client.post(self.url(card), {"accept": "yes"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TenderEstimate.objects.exists())

    def test_confirmation_shows_human_label_purchase_number_and_title(self):
        tool = next(t for t in available_tools(self.user) if t["id"] == "tender.send_to_calculation")
        arguments = {"tender_id": self.tender.pk}
        self.assertEqual(confirmation_arguments(tool, arguments), [{
            "label": "Тендер", "items": [{
                "text": f"{self.tender.purchase_number} — {self.tender.title}",
            }],
        }])
        self.assertEqual(arguments, {"tender_id": self.tender.pk})
        definitions, _ = openai_functions(self.user, area_id="tender_selection",
                                          capability_id="tender.send_to_calculation")
        definition = next(d for d in definitions if d["function"]["name"] == "tender_send_to_calculation")
        schema = definition["function"]["parameters"]["properties"]["tender_id"]
        self.assertNotIn("label", schema)
        self.assertNotIn("reference", schema)
        self.assertIn("READ", schema["description"])

    def test_explicit_label_precedes_model_field_and_reference_model_names(self):
        for declaration, value, ui in [
            ({"type": "string", "label": "Основание расхода"}, "Бумага", {"model": "cash.CashTransaction"}),
            ({"type": "integer", "label": "Выбранная закупка",
              "reference": {"model": "tender_selection.Tender", "label": ["purchase_number", "title"]}},
             self.tender.pk, None),
        ]:
            with self.subTest(label=declaration["label"]):
                name = "reason" if ui else "tender_id"
                card = confirmation_arguments({"input_schema": {name: declaration}, "ui": ui}, {name: value})
                self.assertEqual(card[0]["label"], declaration["label"])

    def test_generic_choices_and_conditional_references_do_not_change_arguments(self):
        from copy import deepcopy
        from uuid import uuid4
        from .models import OrderEstimate
        from .assistant_tools import AssistantTool, register_tool
        from . import assistant_tools

        order = OrderEstimate.objects.create(owner=self.user, order_number="ORDER-001", name="Заказной расчёт")
        tender = TenderEstimate.objects.create(owner=self.user, tender=self.tender,
            tender_number=self.tender.purchase_number, name="Тендерный расчёт")
        selector, identifier, choice = ["field_" + uuid4().hex for _ in range(3)]
        tool_id = "extension." + uuid4().hex
        register_tool(AssistantTool(tool_id, "Произвольное действие", "Тест", {
            selector: {"type": "boolean", "label": "Выбранный вид"},
            identifier: {"type": "integer", "label": "Объект", "reference": {
                "model": [{"when": {selector: True}, "model": "tenders.TenderEstimate"},
                          {"when": {selector: False}, "model": "tenders.OrderEstimate"}], "label": "name"}},
            choice: {"type": "string", "label": "Решение", "choices": [("arbitrary-code", "Понятное решение")]},
        }, False, True, True, lambda user, payload: payload))
        try:
            tool = next(t for t in available_tools(self.user) if t["id"] == tool_id)
            for selected, instance in [(True, tender), (False, order)]:
                with self.subTest(selected=selected):
                    args = {selector: selected, identifier: instance.pk, choice: "arbitrary-code"}
                    saved = deepcopy(args)
                    validate_tool_call(tool_id, self.user, args)
                    card = confirmation_arguments(tool, args)
                    self.assertEqual(card[1]["items"][0]["text"], instance.name)
                    self.assertEqual(card[2]["value"], "Понятное решение")
                    self.assertEqual(args, saved)
            with self.assertRaises(AssistantToolError):
                confirmation_arguments(tool, {identifier: tender.pk})
        finally:
            assistant_tools._TOOLS.pop(tool_id)

    def test_string_reference_is_resolved_without_converting_action_argument(self):
        args = {"manager": str(self.user.pk)}
        tool = {"input_schema": {"manager": {"type": "string", "label": "Менеджер",
            "reference": {"model": "auth.User", "label": "username"}}}}
        self.assertEqual(confirmation_arguments(tool, args)[0]["items"][0]["text"], self.user.username)
        self.assertEqual(args, {"manager": str(self.user.pk)})

    def test_presentation_metadata_does_not_change_parameter_contract(self):
        from .assistant_tools import _function_definition
        for kind in ["object", "object?", "array", "array?", "string", "integer?", "integer[]"]:
            with self.subTest(kind=kind):
                plain = _function_definition("extension.test", "Тест", {"value": kind})
                decorated = _function_definition("extension.test", "Тест", {"value": {
                    "type": kind.rstrip("?"), "required": not kind.endswith("?"),
                    "label": "Человекочитаемая подпись", "choices": [("code", "Подпись")],
                }})
                self.assertEqual(plain, decorated)

    def test_reference_object_fields_use_their_existing_choice_labels(self):
        transaction = CashTransaction.objects.create(created_by=self.user, operation_date="2026-10-24",
            account="card", direction="expense", amount=175, reason="Бумага")
        args = {"operation": transaction.pk}
        tool = {"input_schema": {"operation": {"type": "integer", "label": "Операция", "reference": {
            "model": "cash.CashTransaction", "label": ["account", "direction", "reason"],
        }}}}
        self.assertEqual(confirmation_arguments(tool, args)[0]["items"][0]["text"], "Карта — Расход — Бумага")
        self.assertEqual(args, {"operation": transaction.pk})
