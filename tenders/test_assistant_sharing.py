from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from cash.models import CashTransaction
from tender_selection.models import Tender
from .assistant_conversations import add_assistant_message, add_debug_event, create_conversation, upload_attachment
from .models import KnowledgeRecord, OwnerFeedbackEvent, OwnerInteraction, TenderEstimate


class AssistantSharingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("share-owner", password="password")
        self.other = get_user_model().objects.create_user("share-other", password="password")
        self.conversation = create_conversation(self.user)
        add_assistant_message(self.conversation, "Пример ответа", "tool_result", {})
        add_debug_event(self.conversation, "example.stage", event_output={"ok": True})
        self.client.force_login(self.user)

    def link(self, conversation=None):
        response = self.client.post(reverse("assistant_conversation_link", args=[(conversation or self.conversation).pk]))
        self.assertEqual(response.status_code, 200)
        return response.json()["url"]

    def test_link_reads_existing_conversation_and_journal_without_login_or_copy(self):
        before = (OwnerInteraction.objects.count(), OwnerFeedbackEvent.objects.count())
        url = self.link()
        response = Client().get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation"]["id"], self.conversation.pk)
        self.assertEqual(response.json()["messages"][0]["text"], "Пример ответа")
        self.assertEqual(response.json()["debug_journal"][0]["stage"], "example.stage")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual((OwnerInteraction.objects.count(), OwnerFeedbackEvent.objects.count()), before)
        add_assistant_message(self.conversation, "Новый ответ")
        self.assertEqual(len(Client().get(url).json()["messages"]), 2)

    def test_link_generation_requires_owner_and_post(self):
        endpoint = reverse("assistant_conversation_link", args=[self.conversation.pk])
        self.assertEqual(Client().post(endpoint).status_code, 302)
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(endpoint).status_code, 404)

    def test_unsigned_tampered_and_other_conversation_links_fail(self):
        url = self.link()
        endpoint = reverse("assistant_conversation_export", args=[self.conversation.pk])
        self.assertEqual(Client().get(endpoint).status_code, 404)
        self.assertEqual(Client().get(url + "x").status_code, 404)
        other = create_conversation(self.other)
        wrong = reverse("assistant_conversation_export", args=[other.pk]) + "?" + urlsplit(url).query
        self.assertEqual(Client().get(wrong).status_code, 404)
        self.assertEqual(Client().post(url).status_code, 405)

    def test_new_conversation_link_points_to_new_conversation(self):
        first = self.link()
        second = create_conversation(self.user)
        second_url = self.link(second)
        self.assertNotEqual(first, second_url)
        self.assertEqual(Client().get(second_url).json()["conversation"]["id"], second.pk)
        self.assertEqual(Client().get(first).json()["conversation"]["id"], self.conversation.pk)

    def test_clear_deletes_open_and_archived_history_but_preserves_business_data(self):
        archived = create_conversation(self.user)
        archived.status = "archived"
        archived.save(update_fields=["status"])
        add_assistant_message(archived, "Архивный ответ")
        upload_attachment(self.conversation, self.user, SimpleUploadedFile("sample.txt", b"example"))
        feedback = self.conversation.feedback_events.first()
        knowledge = KnowledgeRecord.objects.create(feedback_event=feedback, scope_type="test", payload={"fact": "keep"})
        tender = Tender.objects.create(purchase_number="SHARE-001", title="Не удалять")
        estimate = TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="SHARE-001", name="Не удалять")
        transaction = CashTransaction.objects.create(operation_date="2026-10-25", account="cash", direction="expense", amount=1, reason="Не удалять", created_by=self.user)
        unrelated = OwnerInteraction.objects.create(question="Вопрос по бизнес-объекту", context={"owner_id": self.user.pk})
        url = self.link()
        ids = [self.conversation.pk, archived.pk]
        response = self.client.post(reverse("assistant_conversations_clear"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertFalse(OwnerInteraction.objects.filter(pk__in=ids).exists())
        self.assertFalse(OwnerFeedbackEvent.objects.filter(interaction_id__in=ids).exists())
        from .models import ConversationAttachment
        self.assertFalse(ConversationAttachment.objects.filter(conversation_id__in=ids).exists())
        knowledge.refresh_from_db()
        self.assertIsNone(knowledge.feedback_event_id)
        self.assertEqual(knowledge.payload, {"fact": "keep"})
        self.assertTrue(Tender.objects.filter(pk=tender.pk).exists())
        self.assertTrue(TenderEstimate.objects.filter(pk=estimate.pk).exists())
        self.assertTrue(CashTransaction.objects.filter(pk=transaction.pk).exists())
        self.assertTrue(OwnerInteraction.objects.filter(pk=unrelated.pk).exists())
        self.assertEqual(Client().get(url).status_code, 404)

    def test_clear_does_not_touch_other_owner_or_their_link(self):
        other_conversation = create_conversation(self.other)
        self.client.force_login(self.other)
        url = self.link(other_conversation)
        self.client.force_login(self.user)
        self.client.post(reverse("assistant_conversations_clear"))
        self.assertTrue(OwnerInteraction.objects.filter(pk=other_conversation.pk).exists())
        self.assertEqual(Client().get(url).status_code, 200)

    def test_clear_requires_login_post_and_csrf(self):
        endpoint = reverse("assistant_conversations_clear")
        self.assertEqual(Client().post(endpoint).status_code, 302)
        self.assertEqual(self.client.get(endpoint).status_code, 400)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(endpoint).status_code, 403)
        self.assertTrue(OwnerInteraction.objects.filter(pk=self.conversation.pk).exists())
