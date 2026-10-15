import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import ConversationAttachment, OwnerInteraction


class AssistantConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-owner", "owner@example.test", "password")
        self.client.force_login(self.user)

    def _conversation(self, title="Тест"):
        return self.client.post(reverse("assistant_conversation_new"), {"title": title}, secure=True).json()["conversation"]["id"]

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_executes_read_then_restricted_compute_then_returns_verified_answer(self, request, _preflight):
        from tender_selection.models import Tender

        Tender.objects.create(purchase_number="LOST-1", title="Первый", outcome_status=Tender.OUTCOME_LOST, contract_reduction_percent="10")
        Tender.objects.create(purchase_number="LOST-2", title="Второй", outcome_status=Tender.OUTCOME_LOST, contract_reduction_percent="20")

        def fake_gateway(prompt):
            payload = json.loads(prompt)
            if not payload["completed_step_results"]:
                schema = next(item for item in payload["read_catalog"] if item["id"] == "tender_selection.Tender")
                self.assertIn("contract_reduction_percent", {field.split(":", 1)[0].split("->", 1)[0] for field in schema["fields"]})
                return {"reply": "Читаю проигранные тендеры.", "status": "next", "next_step": {
                    "tool_id": "read", "purpose": "получить фактическое снижение", "arguments": {"plan": {
                        "entity": "tender_selection.Tender", "fields": ["purchase_number", "contract_reduction_percent"],
                        "filters": [{"field": "outcome_status", "op": "eq", "value": Tender.OUTCOME_LOST}],
                    }},
                }}
            if len(payload["completed_step_results"]) == 1:
                self.assertEqual(payload["completed_step_results"][0]["result"]["total_count"], 2)
                return {"reply": "Считаю среднее по полученным значениям.", "status": "next", "next_step": {
                    "tool_id": "compute", "purpose": "среднее снижение", "arguments": {
                        "operation": "avg", "inputs": [{"step": 1, "path": "records[].contract_reduction_percent"}],
                    },
                }}
            self.assertEqual(payload["completed_step_results"][1]["result"]["value"], "15")
            return {"reply": "Среднее фактическое снижение по двум проигранным тендерам — 15%.", "status": "final", "basis": "verified_steps"}

        request.side_effect = fake_gateway
        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation("READ + compute")]),
            data=json.dumps({"message": "Найди проигранные тендеры и посчитай среднее снижение"}),
            content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 200)
        message = response.json()["messages"][-1]
        self.assertEqual(message["kind"], "text")
        self.assertIn("BACKEND → LLM", message["text"])
        self.assertIn("LLM → BACKEND", message["text"])
        self.assertIn('"tool_id": "read"', message["text"])
        self.assertIn('"tool_id": "compute"', message["text"])
        self.assertIn('"value": "15"', message["text"])
        self.assertEqual([step["tool_id"] for step in message["data"]["steps"]], ["read", "compute"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_invalid_read_plan_is_shown_then_repaired(self, request, _preflight):
        from tender_selection.models import Tender

        Tender.objects.create(purchase_number="READ-1", title="Проверка")

        def fake_gateway(prompt):
            payload = json.loads(prompt)
            if not payload.get("previous_backend_rejection"):
                return {"reply": "Читаю.", "status": "next", "next_step": {
                    "tool_id": "read", "purpose": "проверка", "arguments": {"plan": {
                        "entity": "tender_selection.Tender", "fields": ["purchase_number"], "unexpected": True,
                    }},
                }}
            self.assertEqual(payload["previous_backend_rejection"]["error"], "Некорректный план чтения")
            if not payload["completed_step_results"]:
                return {"reply": "Исправляю.", "status": "next", "next_step": {
                    "tool_id": "read", "purpose": "проверка", "arguments": {"plan": {
                        "entity": "tender_selection.Tender", "fields": ["purchase_number"],
                    }},
                }}
            return {"reply": "Готово.", "status": "final", "basis": "verified_steps"}

        request.side_effect = fake_gateway
        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Покажи тендеры"}), content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 200)
        text = response.json()["messages"][-1]["text"]
        self.assertIn('"unexpected": true', text)
        self.assertIn("Некорректный план чтения", text)
        self.assertGreaterEqual(text.count("LLM → BACKEND"), 3)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_confirmed_action_continues_the_same_task(self, request, _preflight):
        from cash.models import CashTransaction

        request.side_effect = [
            {"reply": "Подтвердите расход.", "status": "next", "next_step": {
                "tool_id": "cash.transaction_create", "purpose": "внести расход", "arguments": {
                    "operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Доставка",
                },
            }},
            {"reply": "Расход внесён и задача завершена.", "status": "final", "basis": "verified_steps"},
        ]
        conversation_id = self._conversation("Расход")
        first = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Добавь расход"}),
            content_type="application/json", secure=True,
        ).json()["messages"][-1]
        self.assertEqual(first["kind"], "confirmation_required")
        confirmed = self.client.post(
            reverse("assistant_conversation_action_confirm", args=[conversation_id, first["id"]]), {"accept": "yes"}, secure=True,
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(CashTransaction.objects.filter(reason="Доставка").exists())
        self.assertIn("TOOL → BACKEND", confirmed.json()["messages"][-1]["text"])
        self.assertIn("LLM → BACKEND", confirmed.json()["messages"][-1]["text"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_missing_data_is_asked_in_the_same_conversation(self, request, _preflight):
        request.side_effect = [
            {"reply": "Какова сумма?", "status": "needs_input"},
            {"reply": "Подтвердите расход.", "status": "next", "next_step": {
                "tool_id": "cash.transaction_create", "purpose": "внести расход", "arguments": {
                    "operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Доставка",
                },
            }},
        ]
        conversation_id = self._conversation("Уточнение")
        endpoint = reverse("assistant_conversation_message", args=[conversation_id])
        first = self.client.post(endpoint, data=json.dumps({"message": "Добавь расход на доставку"}), content_type="application/json", secure=True).json()["messages"][-1]
        second = self.client.post(endpoint, data=json.dumps({"message": "15 000"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.assertTrue(first["data"]["task_pending"])
        self.assertEqual(second["kind"], "confirmation_required")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_cancelled_confirmation_never_writes(self, request, _preflight):
        from cash.models import CashTransaction

        request.return_value = {"reply": "Подтвердите расход.", "status": "next", "next_step": {
            "tool_id": "cash.transaction_create", "purpose": "внести расход", "arguments": {
                "operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Отмена",
            },
        }}
        conversation_id = self._conversation()
        event = self.client.post(reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True).json()["messages"][-1]
        cancelled = self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, event["id"]]), {"accept": "no"}, secure=True)
        self.assertEqual(cancelled.status_code, 200)
        self.assertFalse(CashTransaction.objects.filter(reason="Отмена").exists())
        self.assertEqual(self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, event["id"]]), {"accept": "yes"}, secure=True).status_code, 409)
        self.assertNotIn("agent_run", OwnerInteraction.objects.get(pk=conversation_id).context)

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_confirmation_is_invalid_after_a_newer_message(self, request, _preflight):
        from cash.models import CashTransaction

        request.side_effect = [
            {"reply": "Подтвердите расход.", "status": "next", "next_step": {
                "tool_id": "cash.transaction_create", "purpose": "внести расход", "arguments": {
                    "operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Устаревший расход",
                },
            }},
            {"reply": "Какова новая сумма?", "status": "needs_input"},
        ]
        conversation_id = self._conversation()
        endpoint = reverse("assistant_conversation_message", args=[conversation_id])
        event = self.client.post(endpoint, data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True).json()["messages"][-1]
        self.client.post(endpoint, data=json.dumps({"message": "Погоди, изменю условия"}), content_type="application/json", secure=True)
        confirmed = self.client.post(reverse("assistant_conversation_action_confirm", args=[conversation_id, event["id"]]), {"accept": "yes"}, secure=True)
        self.assertEqual(confirmed.status_code, 409)
        self.assertFalse(CashTransaction.objects.filter(reason="Устаревший расход").exists())

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_final_without_a_verified_or_user_supplied_basis_is_rejected(self, request, _preflight):
        request.return_value = {"reply": "Придуманный итог", "status": "final", "basis": "verified_steps"}
        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]), data=json.dumps({"message": "Сколько тендеров?"}),
            content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("без проверенного результата", response.json()["detail"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_unregistered_action_is_rejected(self, request, _preflight):
        request.return_value = {"reply": "Делаю.", "status": "next", "next_step": {"tool_id": "invented.action", "purpose": "нет", "arguments": {}}}
        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]), data=json.dumps({"message": "Сделай невозможное"}),
            content_type="application/json", secure=True,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("вне энциклопедии", response.json()["detail"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request", return_value={"reply": "Вижу прайс.", "status": "final", "basis": "user_supplied"})
    def test_document_is_provided_by_safe_reference(self, request, _preflight):
        conversation_id = self._conversation("Прайс")
        attachment = self.client.post(
            reverse("assistant_conversation_attachment", args=[conversation_id]),
            {"file": SimpleUploadedFile("price.csv", b"name,price\nshirt,500\n", content_type="text/csv")}, secure=True,
        ).json()["attachment"]
        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]), data=json.dumps({"message": "Это прайс", "attachment_ids": [attachment["id"]]}),
            content_type="application/json", secure=True,
        )
        self.assertEqual(reply.status_code, 200)
        prompt = json.loads(request.call_args.args[0])
        self.assertEqual(prompt["attachments"], [{"id": attachment["id"], "name": "price.csv", "kind": "document", "text": "name,price\nshirt,500"}])
        self.assertNotIn("content", prompt["attachments"][0])

    def test_uploaded_attachment_is_removed_with_conversation_history(self):
        conversation_id = self._conversation("Прайс")
        attachment = self.client.post(
            reverse("assistant_conversation_attachment", args=[conversation_id]),
            {"file": SimpleUploadedFile("price.csv", b"name,price\nshirt,500\n", content_type="text/csv")}, secure=True,
        ).json()["attachment"]
        self.client.post(reverse("assistant_conversations_clear"), secure=True)
        self.assertFalse(ConversationAttachment.objects.filter(pk=attachment["id"]).exists())
        self.assertEqual(OwnerInteraction.objects.get(pk=conversation_id).status, "archived")

    def test_global_assistant_drawer_contract_is_unchanged(self):
        response = self.client.get(reverse("tender_home"), secure=True)
        self.assertContains(response, "assistant-context-summary")
        self.assertContains(response, "assistant-tool-result")
        self.assertContains(response, "assistant-message--pending")
        self.assertNotContains(response, "data-sewing-upload")
