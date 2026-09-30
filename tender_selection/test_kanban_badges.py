from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tenders.models import TenderEstimate

from .models import Tender
from .views import _estimate_card

PROTOCOL = {"participants": [
    {"id": "ZK-1", "rank": 1, "price": "1000.00", "rejected": False},
    {"id": "ZK-2", "rank": 2, "price": "1100.00", "rejected": False},
]}


class AccumulatedBadgesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")
        self.tender = Tender.objects.create(
            purchase_number="1", review=Tender.INTERESTING, status=Tender.PUSHED,
            risk_assessment={"risk_level": "high", "risk_factors": []}, risk_checked_at=timezone.now(),
        )

    def _texts(self, *, tender_fields=None, **fields):
        if tender_fields:
            for key, value in tender_fields.items():
                setattr(self.tender, key, value)
            self.tender.save()
        estimate = TenderEstimate.objects.create(owner=self.user, tender=self.tender, tender_number="1", name="Расчёт", **fields)
        return [badge["text"] for badge in _estimate_card(estimate)["badges"]]

    def test_risk_from_evaluation_stays_on_later_stages(self):
        self.assertEqual(self._texts(summary_snapshot={"roi": "25.00"})[:2], ["риск: высокий", "ROI 25.00%"])

    def test_only_risk_roi_and_outcome_accumulate(self):
        texts = self._texts(
            summary_snapshot={"roi": "25.00"},
            tender_fields={
                "outcome_status": Tender.OUTCOME_LOST, "bid_number": "ZK-2", "protocol": PROTOCOL,
                "contract_reduction_percent": Decimal("12.00"), "outcome_checked_at": timezone.now(),
                "outcome_source": Tender.OUTCOME_AUTO,
            },
        )
        self.assertEqual(texts, ["риск: высокий", "ROI 25.00%", "Проигран"])


class LegacyUnreviewedTenderInCalculationTests(TestCase):
    """Тендеры, перенесённые в расчёт по старой схеме без отметки «в работу»."""

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.tender = Tender.objects.create(
            purchase_number="1", review=Tender.UNREVIEWED, status=Tender.PUSHED,
            notification_raw={"source": {}}, risk_checked_at=timezone.now(), risk_assessment={"preliminary": True},
        )
        self.estimate = TenderEstimate.objects.create(owner=self.admin, tender=self.tender, tender_number="1", name="Расчёт")

    def test_board_card_shows_risk_badge(self):
        self.assertEqual(_estimate_card(self.estimate)["badges"][0]["text"], "риск: ожидает")

    def test_card_runs_full_assessment_instead_of_preliminary(self):
        from unittest.mock import patch

        self.client.force_login(self.admin)
        with patch("tender_selection.views.notification_for", return_value={"source": {}}), \
                patch("tender_selection.views.extras_for", return_value=([], [])):
            response = self.client.get(f"/tender-selection/{self.tender.pk}/")

        self.assertContains(response, "<p class=\"ts-doc-loading\" data-risk-autostart>")


class IncompleteRoiBadgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")

    def test_roi_is_greyed_out_while_calculation_is_incomplete(self):
        estimate = TenderEstimate.objects.create(
            owner=self.user, tender_number="1", name="Черновик",
            summary_snapshot={"roi": "1900.00", "is_incomplete": True},
        )
        self.assertEqual(_estimate_card(estimate)["badges"][0], {"state": "pending", "text": "ROI 1900.00%"})

    def test_roi_keeps_its_colour_once_calculation_is_complete(self):
        estimate = TenderEstimate.objects.create(
            owner=self.user, tender_number="1", name="Готово",
            summary_snapshot={"roi": "40.00", "is_incomplete": False},
        )
        self.assertEqual(_estimate_card(estimate)["badges"][0]["state"], "ok")


class CustomerOnKanbanCardTests(TestCase):
    def test_found_card_shows_organization_name_when_known(self):
        from .models import Organization
        from .views import _found_tender_card

        Organization.objects.create(inn="7700000001", name="Спорткомитет")
        tender = Tender.objects.create(purchase_number="1", customer_inn="7700000001")
        tender.org = Organization.objects.get(inn="7700000001")

        self.assertEqual(_found_tender_card(tender)["customer"], "Спорткомитет")

    def test_found_card_falls_back_to_inn_when_organization_unknown(self):
        from .views import _found_tender_card

        tender = Tender.objects.create(purchase_number="1", customer_inn="7700000002")
        tender.org = None

        self.assertEqual(_found_tender_card(tender)["customer"], "ИНН 7700000002")

    def test_kanban_review_cards_carry_customer_name(self):
        from .models import Organization

        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)
        Organization.objects.create(inn="7700000003", name="Комитет по спорту")
        Tender.objects.create(purchase_number="1", title="Кружки", customer_inn="7700000003", review=Tender.INTERESTING)

        response = self.client.get("/tender-selection/?view=kanban")

        self.assertContains(response, "Комитет по спорту")
