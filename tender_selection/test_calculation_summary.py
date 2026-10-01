from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tenders.models import TenderEstimate, TenderSettings
from tenders.services import verdict_for

from .models import Tender


class CalculationSummaryTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.tender = Tender.objects.create(
            purchase_number="1",
            source=Tender.MANUAL,
            status=Tender.PUSHED,
            review=Tender.INTERESTING,
        )
        TenderSettings.objects.update_or_create(
            pk=1,
            defaults={"roi_good_percent": Decimal("30"), "roi_thin_percent": Decimal("15")},
        )
        self.estimate = TenderEstimate.objects.create(
            owner=self.admin,
            tender=self.tender,
            tender_number="1",
            name="Расчёт",
            russia_delivery=Decimal("100"),
            vat_rate_snapshot=Decimal("5"),
            summary_snapshot={
                "is_incomplete": False,
                "nmck_total": "2000.00",
                "purchase_total": "1000.00",
                "rrp_total": "1600.00",
                "net_profit": "420.00",
                "roi": "35.59",
            },
        )

    def test_verdict_contains_cost_nmck_and_profit_at_roi_boundaries(self):
        verdict = verdict_for(self.estimate, self.tender)

        self.assertEqual(verdict["nmck_total"], Decimal("2000.00"))
        self.assertEqual(verdict["purchase_total"], Decimal("1000.00"))
        self.assertEqual(verdict["price_thresholds"]["target_profit"], Decimal("352.94"))
        self.assertEqual(verdict["price_thresholds"]["floor_profit"], Decimal("175.07"))

    def test_tender_page_shows_completed_calculation_summary(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))

        self.assertContains(response, "Себестоимость")
        self.assertContains(response, "НМЦК")
        self.assertContains(response, "Прибыль при целевом ROI")
        self.assertContains(response, "Прибыль при минимальном ROI")

    def test_summary_remains_visible_after_transition_to_bidding(self):
        self.tender.outcome_status = Tender.OUTCOME_PENDING
        self.tender.save(update_fields=["outcome_status"])
        self.client.force_login(self.admin)

        response = self.client.get(reverse("tender_selection:detail", args=[self.tender.pk]))

        self.assertContains(response, "Себестоимость")
        self.assertContains(response, "Прибыль при целевом ROI")


class BiddingTransitionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.tender = Tender.objects.create(
            purchase_number="1",
            source=Tender.MANUAL,
            status=Tender.PUSHED,
            review=Tender.INTERESTING,
        )
        self.estimate = TenderEstimate.objects.create(
            owner=self.admin,
            tender=self.tender,
            tender_number="1",
            name="Расчёт",
            summary_snapshot={"is_incomplete": True, "roi": "0.00"},
        )
        self.client.force_login(self.admin)

    def test_incomplete_calculation_cannot_move_to_bidding(self):
        response = self.client.post(
            reverse("tender_pipeline_estimate_status", args=[self.estimate.pk]),
            {"status": Tender.OUTCOME_PENDING},
        )

        self.assertEqual(response.status_code, 400)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_DRAFT)

    def test_bidding_button_appears_only_for_completed_calculation(self):
        detail_url = reverse("tender_selection:detail", args=[self.tender.pk])

        self.assertNotContains(self.client.get(detail_url), "На торги →")

        self.estimate.summary_snapshot["is_incomplete"] = False
        self.estimate.summary_snapshot.update({
            "purchase_total": "1000.00",
            "nmck_total": "2000.00",
            "rrp_total": "1600.00",
            "net_profit": "420.00",
        })
        self.estimate.save(update_fields=["summary_snapshot"])

        self.assertContains(self.client.get(detail_url), "На торги →")

    def test_completed_calculation_can_move_to_bidding(self):
        self.estimate.summary_snapshot["is_incomplete"] = False
        self.estimate.save(update_fields=["summary_snapshot"])

        response = self.client.post(
            reverse("tender_pipeline_estimate_status", args=[self.estimate.pk]),
            {"status": Tender.OUTCOME_PENDING},
        )

        self.assertEqual(response.status_code, 302)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_PENDING)

