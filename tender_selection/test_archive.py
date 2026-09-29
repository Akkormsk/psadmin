from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tenders.models import TenderEstimate

from .models import Organization, Tender


class ArchiveTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        self.now = timezone.now()

    def _archived(self, number, title, *, review=Tender.UNREVIEWED, estimate_status=None, days_ago=1, **fields):
        tender = Tender.objects.create(
            purchase_number=number, title=title, review=review, status=Tender.DISMISSED,
            archived_at=self.now - timedelta(days=days_ago),
            **({"outcome_status": estimate_status} if estimate_status else {}),
            **({"contract_reduction_percent": Decimal("40.60")} if estimate_status == Tender.OUTCOME_LOST else {}),
            **fields,
        )
        if estimate_status:
            TenderEstimate.objects.create(
                owner=self.admin, tender=tender, tender_number=number, name=title,
            )
        return tender

    def _numbers(self, **params):
        response = self.client.get("/tender-selection/archive/", params)
        self.assertEqual(response.status_code, 200)
        return [row["tender"].purchase_number for row in response.context["page_obj"].object_list], response

    def test_stage_is_the_reason_and_each_stage_is_counted(self):
        self._archived("incoming", "Кружки")
        self._archived("evaluation", "Флаги", review=Tender.INTERESTING)
        self._archived("calculation", "Буклеты", review=Tender.INTERESTING, estimate_status=Tender.OUTCOME_DRAFT)
        self._archived("unprofitable", "Ручки", review=Tender.INTERESTING, estimate_status=Tender.OUTCOME_NOT_PARTICIPATED)
        self._archived("lost", "Календари", review=Tender.INTERESTING, estimate_status=Tender.OUTCOME_LOST)
        self._archived("won", "Футболки", review=Tender.INTERESTING, estimate_status=Tender.OUTCOME_WON)

        _, response = self._numbers()
        counts = {stage["key"]: stage["count"] for stage in response.context["stages"]}

        self.assertEqual(counts, {"all": 6, "incoming": 1, "evaluation": 1, "calculation": 2, "bidding": 0, "published": 0, "lost": 1, "won": 1})
        self.assertCountEqual(self._numbers(stage="calculation")[0], ["calculation", "unprofitable"])
        self.assertEqual(self._numbers(stage="lost")[0], ["lost"])

    def test_search_matches_title_number_and_customer(self):
        Organization.objects.create(inn="7700000001", name="Спорткомитет")
        self._archived("0172200004526000010", "Полиграфия", customer_inn="7700000001")
        self._archived("0373100073126000016", "Сувениры")

        self.assertEqual(self._numbers(q="полиграф")[0], ["0172200004526000010"])
        self.assertEqual(self._numbers(q="0373100073")[0], ["0373100073126000016"])
        self.assertEqual(self._numbers(q="спорткомитет")[0], ["0172200004526000010"])

    def test_newest_archived_first_by_default(self):
        self._archived("older", "А", days_ago=5)
        self._archived("newer", "Б", days_ago=1)
        self.assertEqual(self._numbers()[0], ["newer", "older"])

    def test_live_tenders_are_not_in_archive(self):
        Tender.objects.create(purchase_number="live", title="Живой")
        self.assertEqual(self._numbers()[0], [])

    def test_restoring_a_tender_with_estimate_returns_it_to_work(self):
        tender = self._archived("calc", "Буклеты", review=Tender.INTERESTING, estimate_status=Tender.OUTCOME_DRAFT)

        self.client.post(f"/tender-selection/{tender.pk}/restore/")
        tender.refresh_from_db()

        self.assertEqual(tender.status, Tender.PUSHED)
        self.assertIsNone(tender.archived_at)

    def test_restoring_an_incoming_tender_returns_it_to_incoming(self):
        tender = self._archived("incoming", "Кружки")

        self.client.post(f"/tender-selection/{tender.pk}/restore/")
        tender.refresh_from_db()

        self.assertEqual(tender.status, Tender.NEW)
