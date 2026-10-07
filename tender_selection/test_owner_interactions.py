from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tenders.models import OwnerFeedbackEvent, OwnerInteraction, TenderSourceItem

from .models import Tender


class OwnerInteractionAnswerTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("interaction-admin", password="x")
        self.client.force_login(self.admin)
        self.tender = Tender.objects.create(purchase_number="v2-question", title="Тестовая позиция")
        self.item = TenderSourceItem.objects.create(
            tender=self.tender, source_key="notification:test", source_type="notification", original_text="Блокнот"
        )
        self.interaction = OwnerInteraction.objects.create(
            tender=self.tender, source_item=self.item, question="Укажите тираж.", possible_answers={"options": ["100", "500"]}
        )

    def test_answer_is_persisted_and_resolves_only_this_question(self):
        response = self.client.post(
            reverse("tender_selection:owner_interaction_answer", args=[self.tender.pk, self.interaction.pk]),
            {"answer": "500"},
            secure=True,
        )

        self.assertRedirects(response, f"{reverse('tender_selection:detail', args=[self.tender.pk])}#owner-question-{self.interaction.pk}", fetch_redirect_response=False)
        self.interaction.refresh_from_db()
        self.assertEqual(self.interaction.status, "resolved")
        event = OwnerFeedbackEvent.objects.get(interaction=self.interaction)
        self.assertEqual(event.raw_text, "500")
        self.assertEqual(event.scope, "current_tender")
        self.assertEqual(event.actor, self.admin)

    def test_blank_answer_keeps_question_open(self):
        response = self.client.post(
            reverse("tender_selection:owner_interaction_answer", args=[self.tender.pk, self.interaction.pk]),
            {"answer": "   "},
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.interaction.refresh_from_db()
        self.assertEqual(self.interaction.status, "open")
        self.assertFalse(OwnerFeedbackEvent.objects.exists())
