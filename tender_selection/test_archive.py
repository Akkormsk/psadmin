from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tenders.models import TenderEstimate

from .models import ContractStat, Organization, Tender


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

    def test_dismissal_records_the_stage_where_the_tender_was_hidden(self):
        tender = Tender.objects.create(purchase_number="dismissed", title="Кружки")

        self.client.post(f"/tender-selection/{tender.pk}/dismiss/")
        tender.refresh_from_db()

        self.assertEqual(tender.status, Tender.DISMISSED)
        self.assertEqual(tender.archived_from_stage, "incoming")

    def test_dismissal_returns_to_the_screen_where_the_tender_was_opened(self):
        incoming = Tender.objects.create(purchase_number="incoming", title="Кружки")
        calculated = Tender.objects.create(
            purchase_number="calculated", title="Буклеты", review=Tender.INTERESTING, status=Tender.PUSHED,
        )
        estimate = TenderEstimate.objects.create(
            owner=self.admin, tender=calculated, tender_number="calculated", name="Буклеты",
        )

        incoming_response = self.client.post(
            f"/tender-selection/{incoming.pk}/dismiss/", {"next": "/tender-selection/?view=list"},
        )
        calculation_response = self.client.post(
            f"/tender-selection/estimate/{estimate.pk}/dismiss/", {"next": "/tender-selection/?view=kanban"},
        )

        self.assertRedirects(incoming_response, "/tender-selection/?view=list")
        self.assertRedirects(calculation_response, "/tender-selection/?view=kanban")

    def test_workspace_dismiss_returns_json_without_rendering_the_list_again(self):
        tender = Tender.objects.create(purchase_number="dismiss-ajax", title="Кружки")

        response = self.client.post(
            f"/tender-selection/{tender.pk}/dismiss/", {"reason": "not_profile"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"archived": True, "tender_id": tender.pk})

    def test_workspace_estimate_dismiss_returns_json_without_rendering_the_board_again(self):
        tender = Tender.objects.create(purchase_number="dismiss-estimate-ajax", title="Кружки", status=Tender.PUSHED)
        estimate = TenderEstimate.objects.create(owner=self.admin, tender=tender, tender_number=tender.purchase_number, name=tender.title)

        response = self.client.post(
            f"/tender-selection/estimate/{estimate.pk}/dismiss/", HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"archived": True, "tender_id": tender.pk})

    def test_workspace_review_returns_the_tender_id(self):
        tender = Tender.objects.create(purchase_number="review-ajax", title="Кружки")

        response = self.client.post(
            f"/tender-selection/{tender.pk}/review/", {"review": Tender.INTERESTING},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tender_id"], tender.pk)

    def test_archived_result_can_be_excluded_from_forecast(self):
        tender = self._archived("stat", "Кружки", source=Tender.MANUAL)
        stat = ContractStat.objects.create(
            law="fz44", purchase_number=tender.purchase_number, contract_reg_num="stat-contract",
            own_funnel=True,
        )

        response = self.client.post(f"/tender-selection/{tender.pk}/forecast/")
        stat.refresh_from_db()

        self.assertRedirects(response, f"/tender-selection/{tender.pk}/?from=archive")
        self.assertFalse(stat.forecast_included)

    def test_archived_tender_with_outcome_shows_result_instead_of_active_stage(self):
        tender = self._archived(
            "result", "Кружки", source=Tender.MANUAL,
            contract_price=Decimal("900.00"), contract_reduction_percent=Decimal("10.00"),
            outcome_checked_at=self.now,
        )

        response = self.client.get(f"/tender-selection/{tender.pk}/")

        self.assertTrue(response.context["is_archived"])
        self.assertTrue(response.context["show_outcome"])
        self.assertContains(response, "900.00")

    def test_archive_keeps_origin_and_full_history_without_calculation(self):
        tender = self._archived("history", "Кружки", source=Tender.MANUAL)

        response = self.client.get(f"/tender-selection/{tender.pk}/?from=archive")

        self.assertEqual(response.context["archive_stage_label"], "Входящие")
        self.assertContains(response, "Расчёт не создавался")
        self.assertContains(response, "Данные результата ещё не найдены")
        self.assertContains(response, 'href="/tender-selection/archive/"')

    def test_archived_origin_does_not_change_when_result_arrives(self):
        tender = self._archived("origin", "Кружки", source=Tender.MANUAL)
        tender.archived_from_stage = "incoming"
        tender.outcome_status = Tender.OUTCOME_LOST
        tender.save(update_fields=["archived_from_stage", "outcome_status"])

        response = self.client.get(f"/tender-selection/{tender.pk}/?from=archive")

        self.assertEqual(response.context["archive_stage_label"], "Входящие")
