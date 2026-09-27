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

    def _texts(self, **fields):
        estimate = TenderEstimate.objects.create(owner=self.user, tender=self.tender, tender_number="1", name="Расчёт", **fields)
        return [badge["text"] for badge in _estimate_card(estimate)["badges"]]

    def test_risk_from_evaluation_stays_on_later_stages(self):
        self.assertEqual(self._texts(summary_snapshot={"roi": "25.00"})[:2], ["риск: высокий", "ROI 25.00%"])

    def test_only_risk_roi_and_outcome_accumulate(self):
        texts = self._texts(
            summary_snapshot={"roi": "25.00"}, status=TenderEstimate.LOST, bid_number="ZK-2",
            protocol=PROTOCOL, actual_reduction_percent=Decimal("12.00"), outcome_checked_at=timezone.now(),
            outcome_source=TenderEstimate.OUTCOME_AUTO,
        )
        self.assertEqual(texts, ["риск: высокий", "ROI 25.00%", "Проигран"])
