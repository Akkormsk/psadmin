from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tenders.models import TenderEstimate

from . import services
from .models import FilterSettings, Tender


class RiskTriggerTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        settings = FilterSettings.load()
        settings.include_words = "кружки"
        settings.save()

    def _reviewed_tender(self, title="Поставка флагов"):
        return Tender.objects.create(
            purchase_number="1", title=title, review=Tender.INTERESTING,
            collecting_finished_at=timezone.now() + timedelta(days=5),
            notification_raw={"source": {}},
        )

    def test_background_retry_assesses_reviewed_tender_even_without_plus_words(self):
        tender = self._reviewed_tender()
        with patch.object(services, "notification_for", return_value={"source": {}}), \
                patch.object(services, "risk_assessment_for", return_value={"risk_factors": []}) as assess:
            attempted, _ = services.retry_pending_risks()

        self.assertEqual(attempted, 1)
        self.assertEqual(assess.call_args.args[0].pk, tender.pk)

    def test_evaluation_card_starts_assessment_without_a_button(self):
        tender = self._reviewed_tender()
        with patch("tender_selection.views.notification_for", return_value={"source": {}}), \
                patch("tender_selection.views.extras_for", return_value=([], [])):
            response = self.client.get(f"/tender-selection/{tender.pk}/")

        self.assertContains(response, "data-risk-autostart")
        self.assertNotContains(response, "data-risk-start>")


class NotParticipatedArchiveTests(TestCase):
    def test_not_participated_moves_tender_to_archive(self):
        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)
        tender = Tender.objects.create(purchase_number="1", status=Tender.PUSHED, review=Tender.INTERESTING)
        estimate = TenderEstimate.objects.create(owner=admin, tender=tender, tender_number="1", name="Ручки")

        self.client.post(f"/tenders/pipeline/{estimate.pk}/status/", {"status": TenderEstimate.NOT_PARTICIPATED})
        tender.refresh_from_db()

        self.assertEqual(tender.status, Tender.DISMISSED)
        self.assertIsNotNone(tender.archived_at)
