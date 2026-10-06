from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from tenders.models import TenderEstimate

from . import protocols, services
from .models import Tender

DATA = Path(__file__).parent / "test_data"


def _page(name):
    return (DATA / name).read_text(encoding="utf-8")


QUOTATION_PROTOCOL = {
    "name": "Протокол подведения итогов",
    "url": "https://zakupki.gov.ru/protocol",
    "nmck": "1801340.00",
    "applications_total": 5,
    "failed_reason": "",
    "participants": [
        {"id": "ZK-420728", "rank": 1, "price": "1070000.00", "rejected": False, "result": "", "reject_reason": "", "submitted_at": ""},
        {"id": "ZK-421419", "rank": 2, "price": "1148700.00", "rejected": False, "result": "", "reject_reason": "", "submitted_at": ""},
    ],
}


class ParseEisPagesTests(SimpleTestCase):
    def test_quotation_bid_list_lists_every_participant_by_rank(self):
        protocol = protocols.parse_bid_list(_page("eis_bid_list_quotation.html"))

        self.assertEqual(protocol["applications_total"], 5)
        self.assertEqual(protocol["nmck"], "1801340.00")
        self.assertEqual(protocol["failed_reason"], "")
        self.assertEqual([p["rank"] for p in protocol["participants"]], [1, 2, 3, 4, 5])
        self.assertEqual(protocol["participants"][0]["id"], "ZK-420728")
        self.assertEqual(protocol["participants"][0]["price"], "1070000.00")
        self.assertEqual(protocol["participants"][0]["submitted_at"], "11.09.2026 13:09")
        self.assertEqual(protocols.winner(protocol)["id"], "ZK-420728")

    def test_failed_auction_keeps_rejected_bid_and_reason(self):
        protocol = protocols.parse_bid_list(_page("eis_bid_list_auction_failed.html"))

        admitted, rejected = protocol["participants"]
        self.assertEqual((admitted["id"], admitted["rank"], admitted["price"]), ("127", 1, "978544.50"))
        self.assertEqual((rejected["id"], rejected["rejected"], rejected["rank"]), ("211", True, None))
        self.assertIn("Несоответствие информации", rejected["reject_reason"])
        self.assertIn("только одной заявки", protocol["failed_reason"])

    def test_supplier_results_point_to_final_protocol(self):
        found = protocols.parse_supplier_results(_page("eis_supplier_results_quotation.html"))

        self.assertTrue(found["name"].startswith("Протокол подведения итогов"))
        self.assertEqual(
            found["url"],
            "https://zakupki.gov.ru/epz/order/notice/zk20/view/protocol/protocol-main-info.html"
            "?regNumber=0172200004526000010&type=izk&version=1",
        )
        self.assertIn("protocol-bid-list.html?regNumber=0172200004526000010", protocols.bid_list_url(found["url"]))

    def test_supplier_results_url_is_built_from_notice_url(self):
        self.assertEqual(
            protocols.supplier_results_url("https://zakupki.gov.ru/epz/order/notice/zk44/view/common-info.html?regNumber=1"),
            "https://zakupki.gov.ru/epz/order/notice/zk20/view/supplier-results.html?regNumber=1",
        )
        self.assertEqual(protocols.supplier_results_url("https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=1"), "")


class FindOursTests(SimpleTestCase):
    def test_matches_application_number_ignoring_case_and_spaces(self):
        self.assertEqual(protocols.find_ours(QUOTATION_PROTOCOL, bid_number=" zk-421419 ", bid_price=None)["rank"], 2)

    def test_matches_bid_amount(self):
        self.assertEqual(protocols.find_ours(QUOTATION_PROTOCOL, bid_number="", bid_price=Decimal("1070000"))["rank"], 1)

    def test_unknown_when_nothing_entered_or_nothing_matches(self):
        self.assertIsNone(protocols.find_ours(QUOTATION_PROTOCOL, bid_number="", bid_price=None))
        self.assertIsNone(protocols.find_ours(QUOTATION_PROTOCOL, bid_number="ZK-1", bid_price=Decimal("1")))


class ApplyProtocolTests(TestCase):
    """Исход торгов и протокол теперь живут на Tender, не на TenderEstimate —
    расчёт можно пересчитать/удалить, а факт того, что случилось с тендером,
    должен остаться."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")
        self.tender = Tender.objects.create(
            purchase_number="0172200004526000010",
            eis_url="https://zakupki.gov.ru/epz/order/notice/zk44/view/common-info.html?regNumber=0172200004526000010",
            collecting_finished_at=timezone.now() - timedelta(days=1),
            max_price=Decimal("1801340.00"),
            outcome_status=Tender.OUTCOME_PENDING,
        )

    def _estimate(self, status=None, bid_number="", bid_price=None):
        if status is not None:
            self.tender.outcome_status = status
        self.tender.bid_number = bid_number
        self.tender.bid_price = bid_price
        self.tender.save()
        return TenderEstimate.objects.create(
            owner=self.user, tender=self.tender, tender_number=self.tender.purchase_number, name="Полиграфия",
        )

    def _check(self, estimate):
        with patch.object(protocols, "fetch_protocol", return_value=QUOTATION_PROTOCOL):
            return services.check_protocol(self.tender)

    def test_identified_winner_becomes_won(self):
        estimate = self._estimate(bid_number="ZK-420728")
        self.assertTrue(self._check(estimate))
        self.tender.refresh_from_db()

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_WON)
        self.assertEqual(self.tender.contract_price, Decimal("1070000.00"))
        self.assertEqual(self.tender.contract_reduction_percent, Decimal("40.60"))
        self.assertEqual(self.tender.outcome_source, Tender.OUTCOME_AUTO)

    def test_identified_loser_becomes_lost(self):
        estimate = self._estimate(bid_price=Decimal("1148700"))
        self._check(estimate)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_LOST)

    def test_unidentified_moves_to_published_result(self):
        estimate = self._estimate()
        self._check(estimate)
        self.tender.refresh_from_db()

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_PUBLISHED)
        self.assertEqual(self.tender.protocol["participants"][0]["id"], "ZK-420728")
        self.assertEqual(self.tender.contract_reduction_percent, Decimal("40.60"))

    def test_finished_estimate_keeps_its_status_but_gets_protocol(self):
        estimate = self._estimate(status=Tender.OUTCOME_NOT_PARTICIPATED)
        self._check(estimate)
        self.tender.refresh_from_db()

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_NOT_PARTICIPATED)
        self.assertEqual(self.tender.contract_price, Decimal("1070000.00"))

    def test_no_protocol_yet_only_marks_the_check(self):
        estimate = self._estimate()
        with patch.object(protocols, "fetch_protocol", return_value=None):
            self.assertFalse(services.check_protocol(self.tender))
        self.tender.refresh_from_db()

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_PENDING)
        self.assertIsNotNone(self.tender.protocol_checked_at)
        self.assertEqual(self.tender.protocol, {})

    def test_entering_bid_on_published_result_decides_status(self):
        estimate = self._estimate()
        self._check(estimate)
        self.tender.refresh_from_db()

        services.set_our_bid(estimate, bid_number="ZK-421419", bid_price=None)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_LOST)


class RetryPendingProtocolsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")

    def _estimate(self, number, *, closed_ago=timedelta(days=1), checked_ago=None, status=Tender.OUTCOME_PENDING):
        now = timezone.now()
        tender = Tender.objects.create(
            purchase_number=number, collecting_finished_at=now - closed_ago,
            outcome_status=status, protocol_checked_at=now - checked_ago if checked_ago is not None else None,
        )
        return TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number=number, name=number,
        )

    def _checked_numbers(self):
        with patch.object(services, "check_protocol", return_value=False) as check:
            services.retry_pending_protocols(pause=0)
        return {call.args[0].purchase_number for call in check.call_args_list}

    def test_bidding_after_deadline_and_finished_without_protocol_are_checked(self):
        self._estimate("closed")
        self._estimate("lost-earlier", status=Tender.OUTCOME_LOST)
        self.assertEqual(self._checked_numbers(), {"closed", "lost-earlier"})

    def test_open_drafts_and_recent_checks_are_skipped(self):
        self._estimate("still-open", closed_ago=-timedelta(days=1))
        self._estimate("draft", status=Tender.OUTCOME_DRAFT)
        self._estimate("just-checked", checked_ago=timedelta(minutes=5))
        self.assertEqual(self._checked_numbers(), set())

    def test_legacy_archived_tender_without_deadline_is_checked(self):
        Tender.objects.create(
            purchase_number="legacy-archive", status=Tender.DISMISSED,
            outcome_status=Tender.OUTCOME_DRAFT,
        )

        self.assertEqual(self._checked_numbers(), {"legacy-archive"})

    def test_archived_calculation_is_checked_even_if_it_was_not_a_draft(self):
        Tender.objects.create(
            purchase_number="archived-calculation", status=Tender.DISMISSED,
            outcome_status=Tender.OUTCOME_NOT_PARTICIPATED,
        )

        self.assertEqual(self._checked_numbers(), {"archived-calculation"})


class ProtocolCardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        self.tender = Tender.objects.create(
            purchase_number="0172200004526000010", max_price=Decimal("1801340.00"),
            outcome_status=Tender.OUTCOME_PUBLISHED, protocol=QUOTATION_PROTOCOL, bid_number="ZK-421419",
        )
        self.estimate = TenderEstimate.objects.create(
            owner=self.admin, tender=self.tender, tender_number=self.tender.purchase_number, name="Полиграфия",
        )

    def _card(self):
        with patch("tender_selection.views.notification_for", return_value=None), \
                patch("tender_selection.views.extras_for", return_value=([], [])):
            return self.client.get(f"/tender-selection/{self.tender.pk}/")

    def test_result_card_lists_participants_and_marks_ours(self):
        response = self._card()

        self.assertContains(response, "ZK-420728")
        self.assertContains(response, '<tr class="ts-protocol__ours">', count=1)
        self.assertContains(response, "ZK-421419 · мы")
        self.assertContains(response, "40.60%")

    def test_result_card_shows_winner_and_contract_dates(self):
        self.tender.contract_winner_inn = "771978661830"
        self.tender.contract_exe_start = "2026-11-01"
        self.tender.contract_exe_end = "2026-12-31"
        self.tender.outcome_status = Tender.OUTCOME_WON
        self.tender.save()

        response = self._card()

        self.assertContains(response, "Победитель: ИНН 771978661830 · мы.")
        self.assertContains(response, "Исполнение контракта: с 01.11.2026 по 31.12.2026.")

    def test_saving_bid_amount_decides_status(self):
        self.tender.bid_number = ""
        self.tender.save()

        self.client.post(f"/tender-selection/estimate/{self.estimate.pk}/bid/", {"bid_number": "", "bid_price": "1 070 000,00"})
        self.tender.refresh_from_db()

        self.assertEqual(self.tender.bid_price, Decimal("1070000.00"))
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_WON)

    def test_pending_tender_shows_saved_bid_and_result_date(self):
        self.tender.outcome_status = Tender.OUTCOME_PENDING
        self.tender.bid_number = "7"
        self.tender.bid_price = Decimal("1200000.00")
        self.tender.protocol = {}
        self.tender.save()
        bidding_at = timezone.now() + timedelta(days=2)
        notification = {
            "source": {
                "commonInfo": {"ETP": {"name": "РТС-тендер", "url": "https://www.rts-tender.ru/"}},
                "notificationInfo": {"procedureInfo": {
                    "collectingInfo": {"endDT": "2026-10-05T09:00:00+03:00"},
                    "biddingDate": bidding_at.isoformat(),
                    "summarizingDate": "2026-10-07+03:00",
                }},
            },
        }
        with patch("tender_selection.views.notification_for", return_value=notification), \
                patch("tender_selection.views.extras_for", return_value=([], [])):
            response = self.client.get(f"/tender-selection/{self.tender.pk}/")

        self.assertContains(response, "Наша заявка сохранена")
        self.assertContains(response, "№ 7")
        self.assertContains(response, "Рассмотрение заявок")
        self.assertContains(response, "Подведение итогов — 07.10.2026")
        self.assertContains(response, "Приём заявок завершён.")
        self.assertContains(response, "Следующее событие")
        self.assertContains(response, "Перейти на РТС-тендер")


class OutcomeAndExtrasRegressionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        self.tender = Tender.objects.create(
            purchase_number="0172200010426000028",
            eis_url="https://zakupki.gov.ru/epz/order/notice/zk44/view/common-info.html?regNumber=0172200010426000028",
            law="fz44",
            outcome_status=Tender.OUTCOME_PENDING,
        )
        self.estimate = TenderEstimate.objects.create(
            owner=self.admin,
            tender=self.tender,
            tender_number=self.tender.purchase_number,
            name="Сувенирная продукция",
        )

    def test_protocol_check_receives_tender_not_estimate(self):
        with patch("tender_selection.views.check_protocol", return_value=False) as check, \
                patch("tender_selection.views.fetch_tender_outcome", return_value={"found": False}) as fetch:
            response = self.client.post(
                reverse("tender_selection:enter_outcome", args=[self.estimate.pk]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        check.assert_called_once_with(self.tender)
        fetch.assert_called_once_with(self.tender)
        self.assertEqual(response.json(), {"lifecycle_changed": False, "refresh_detail": True})

    def test_extras_refresh_records_check_time(self):
        with patch("tender_selection.services.gosplan.fetch_clarifications", return_value=[]), \
                patch("tender_selection.services.gosplan.fetch_complaints", return_value=[]):
            services.extras_for(self.tender)

        self.tender.refresh_from_db()
        self.assertIsNotNone(self.tender.extras_checked_at)


class ContractWinnerReconciliationTests(TestCase):
    OUR_INN = "771978661830"

    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")
        self.tender = Tender.objects.create(
            purchase_number="0172200004526000010",
            outcome_status=Tender.OUTCOME_PUBLISHED, protocol=QUOTATION_PROTOCOL,
            contract_price=Decimal("1070000.00"), contract_reduction_percent=Decimal("40.60"),
            outcome_checked_at=timezone.now() - services.CONTRACT_RECHECK - timedelta(minutes=1),
        )
        self.estimate = TenderEstimate.objects.create(
            owner=self.user, tender=self.tender, tender_number=self.tender.purchase_number, name="Полиграфия",
        )

    def _reconcile(self, contracts):
        with patch.dict("os.environ", {"COMPANY_INN": self.OUR_INN}), \
                patch.object(services.gosplan, "fetch_contracts", return_value=contracts) as fetch:
            services.retry_pending_outcomes()
        self.tender.refresh_from_db()
        return fetch

    def test_contract_with_our_inn_marks_won_and_keeps_protocol_figures(self):
        self._reconcile([{"price": 1070000, "suppliers": [self.OUR_INN], "reg_num": "123", "exe_start": "2026-11-01", "exe_end": "2026-12-31"}])

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_WON)
        self.assertEqual(self.tender.contract_reduction_percent, Decimal("40.60"))
        self.assertEqual(self.tender.contract_winner_inn, self.OUR_INN)
        self.assertEqual(str(self.tender.contract_exe_start), "2026-11-01")
        self.assertEqual(str(self.tender.contract_exe_end), "2026-12-31")

    def test_contract_with_someone_else_marks_lost(self):
        self._reconcile([{"price": 1070000, "suppliers": ["7700000000"]}])
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_LOST)

    def test_no_contract_yet_waits_and_postpones_next_check(self):
        self._reconcile([])

        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_PUBLISHED)
        self.assertGreater(self.tender.outcome_checked_at, timezone.now() - timedelta(minutes=1))

    def test_recently_checked_result_is_not_queried(self):
        self.tender.outcome_checked_at = timezone.now()
        self.tender.save()

        fetch = self._reconcile([{"price": 1, "suppliers": [self.OUR_INN]}])

        fetch.assert_not_called()
        self.assertEqual(self.tender.outcome_status, Tender.OUTCOME_PUBLISHED)

    def test_archived_result_without_winner_is_rechecked(self):
        self.tender.status = Tender.DISMISSED
        self.tender.outcome_status = Tender.OUTCOME_DRAFT
        self.tender.contract_winner_inn = ""
        self.tender.save()

        self._reconcile([{"price": 1070000, "suppliers": [self.OUR_INN], "reg_num": "123", "exe_end": "2026-12-31"}])

        self.assertEqual(self.tender.contract_winner_inn, self.OUR_INN)
        self.assertEqual(str(self.tender.contract_exe_end), "2026-12-31")
