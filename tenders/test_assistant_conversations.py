import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import ConversationAttachment


class AssistantConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("assistant-owner", "owner@example.test", "password")
        self.client.force_login(self.user)

    def _conversation(self, title="Тест"):
        return self.client.post(reverse("assistant_conversation_new"), {"title": title}, secure=True).json()["conversation"]["id"]

    def _plan(self, steps, *, reply="Сначала проверю данные, затем подготовлю итог.", status="ready"):
        return {"reply": reply, "status": status, "steps": steps}

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_builds_a_plan_then_executes_its_steps(self, request, _preflight):
        plan = self._plan([
            {
                "id": "lost_tenders", "tool_id": "read", "purpose": "Найти проигранные тендеры за сентябрь",
                "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "contract_reduction_percent"]}},
                "inputs": [{"from": "user", "value": "сентябрь и статус проигран"}],
                "outputs": ["номера тендеров и фактическое снижение"],
            },
            {
                "id": "average_reduction", "tool_id": "compute", "purpose": "Вычислить среднее фактическое снижение",
                "arguments": {},
                "inputs": [{"from": "lost_tenders", "value": "значения фактического снижения"}],
                "outputs": ["среднее снижение"],
            },
            {
                "id": "top_three", "tool_id": "read", "purpose": "Получить три наибольших снижения",
                "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "contract_reduction_percent"]}},
                "inputs": [{"from": "lost_tenders", "value": "отобранные тендеры"}],
                "outputs": ["три тендера для финального ответа"],
            },
        ])
        request.side_effect = [
            plan,
            {
                "reply": "Читаю тендеры.", "status": "next", "basis": "user_supplied",
                "next_step": {
                    "tool_id": "read", "purpose": "Найти проигранные тендеры",
                    "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "contract_reduction_percent"], "filters": [], "order_by": "-id", "limit": 20}},
                },
            },
            {
                "reply": "Считаю количество найденных строк.", "status": "next", "basis": "verified_steps",
                "next_step": {
                    "tool_id": "compute", "purpose": "Посчитать найденные тендеры",
                    "arguments": {"operation": "count", "inputs": [{"step": 1, "path": "records[]"}]},
                },
            },
            {
                "reply": "Отбираю три записи.", "status": "next", "basis": "verified_steps",
                "next_step": {
                    "tool_id": "read", "purpose": "Получить три тендера",
                    "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "contract_reduction_percent"], "filters": [], "order_by": "-id", "limit": 3}},
                },
            },
            {"reply": "Готово.", "status": "final", "basis": "verified_steps"},
        ]

        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Найди проигранные тендеры за сентябрь, посчитай среднее и покажи три максимальных"}),
            content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 200)
        message = response.json()["messages"][-1]
        self.assertEqual(message["text"], "Готово.")
        self.assertEqual(message["kind"], "tool_result")
        self.assertEqual([step["tool_id"] for step in message["data"]["steps"]], ["read", "compute", "read"])
        self.assertEqual(request.call_count, 5)
        execution_prompt = json.loads(request.call_args_list[1].args[0])
        self.assertEqual([step["id"] for step in execution_prompt["execution_plan"]], ["lost_tenders", "average_reduction", "top_three"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_plan_cannot_invent_an_action(self, request, _preflight):
        request.return_value = self._plan([
            {
                "id": "invented", "tool_id": "make_everything", "purpose": "Несуществующее действие", "arguments": {},
                "inputs": [{"from": "user", "value": "запрос"}], "outputs": ["результат"],
            },
        ])

        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Сделай невозможное"}), content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("недоступную функцию", response.json()["detail"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_plan_requires_an_explicit_input_for_every_step(self, request, _preflight):
        request.return_value = self._plan([
            {
                "id": "lost_tenders", "tool_id": "read", "purpose": "Найти проигранные тендеры",
                "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number"]}},
                "inputs": [], "outputs": ["тендеры"],
            },
        ])

        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Найди проигранные тендеры за сентябрь"}),
            content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("входы", response.json()["detail"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_plan_asks_for_missing_user_data_without_steps(self, request, _preflight):
        request.return_value = self._plan([], reply="Укажите сумму и дату расхода.", status="needs_input")

        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 200)
        message = response.json()["messages"][-1]
        self.assertEqual(message["text"], "Укажите сумму и дату расхода.")
        self.assertTrue(message["data"]["task_pending"])

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_answer_to_a_clarification_continues_the_existing_plan(self, request, _preflight):
        request.side_effect = [
            self._plan([{
                "id": "read_tenders", "tool_id": "read", "purpose": "Прочитать тендеры",
                "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number"]}},
                "inputs": [{"from": "user", "value": "месяц"}], "outputs": ["тендеры"],
            }]),
            {"reply": "За какой месяц показать тендеры?", "status": "needs_input", "basis": "unavailable"},
            {
                "reply": "Читаю тендеры за сентябрь.", "status": "next", "basis": "user_supplied",
                "next_step": {
                    "tool_id": "read", "purpose": "Прочитать тендеры",
                    "arguments": {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number"], "filters": [], "order_by": "-id", "limit": 20}},
                },
            },
            {"reply": "Готово.", "status": "final", "basis": "verified_steps"},
        ]
        conversation_id = self._conversation()

        first = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Покажи тендеры"}), content_type="application/json", secure=True,
        )
        second = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "За сентябрь"}), content_type="application/json", secure=True,
        )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["messages"][-1]["data"]["task_pending"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["messages"][-1]["text"], "Готово.")
        self.assertEqual(request.call_count, 4)
        continuation_prompt = json.loads(request.call_args_list[2].args[0])
        self.assertEqual(continuation_prompt["latest_user_message"], "За сентябрь")
        self.assertEqual(continuation_prompt["execution_plan"][0]["id"], "read_tenders")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_action_in_plan_does_not_change_data(self, request, _preflight):
        from cash.models import CashTransaction

        request.side_effect = [self._plan([
            {
                "id": "create_expense", "tool_id": "cash.transaction_create", "purpose": "Создать операцию кассы",
                "arguments": {"operation_date": "2026-10-15", "account": "cash", "direction": "expense", "amount": "1", "reason": "тест"},
                "inputs": [{"from": "user", "value": "дата, счёт, направление, сумма и причина"}],
                "outputs": ["предложение операции для подтверждения"],
            },
        ]), {
            "reply": "Подготовил операцию кассы для подтверждения.", "status": "next", "basis": "user_supplied",
            "next_step": {
                "tool_id": "cash.transaction_create", "purpose": "Создать операцию кассы",
                "arguments": {"operation_date": "2026-10-15", "account": "cash", "direction": "expense", "amount": "1", "reason": "тест"},
            },
        }]

        response = self.client.post(
            reverse("assistant_conversation_message", args=[self._conversation()]),
            data=json.dumps({"message": "Добавь расход"}), content_type="application/json", secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CashTransaction.objects.filter(reason="тест").exists())
        self.assertEqual(response.json()["messages"][-1]["kind"], "confirmation_required")

    @patch("tenders.assistant_agent.preflight")
    @patch("tenders.assistant_agent._request")
    def test_attachment_stays_a_safe_reference_in_planning(self, request, _preflight):
        def fake_gateway(prompt):
            payload = json.loads(prompt)
            self.assertEqual(payload["attachments"][0]["name"], "price.csv")
            self.assertNotIn("path", payload["attachments"][0])
            return self._plan([], reply="Уточните, что сделать с прайсом.", status="needs_input")

        request.side_effect = fake_gateway
        conversation_id = self._conversation("Прайс")
        attachment = self.client.post(
            reverse("assistant_conversation_attachment", args=[conversation_id]),
            {"file": SimpleUploadedFile("price.csv", b"name,price\nshirt,500\n", content_type="text/csv")}, secure=True,
        ).json()["attachment"]

        reply = self.client.post(
            reverse("assistant_conversation_message", args=[conversation_id]),
            data=json.dumps({"message": "Это прайс", "attachment_ids": [attachment["id"]]}), content_type="application/json", secure=True,
        )

        self.assertEqual(reply.status_code, 200)
        self.assertEqual(ConversationAttachment.objects.count(), 1)
