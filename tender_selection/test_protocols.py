from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
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
    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")
        self.tender = Tender.objects.create(
            purchase_number="0172200004526000010",
            eis_url="https://zakupki.gov.ru/epz/order/notice/zk44/view/common-info.html?regNumber=0172200004526000010",
            collecting_finished_at=timezone.now() - timedelta(days=1),
            max_price=Decimal("1801340.00"),
        )

    def _estimate(self, status=TenderEstimate.PENDING, **fields):
        return TenderEstimate.objects.create(
            owner=self.user, tender=self.tender, tender_number=self.tender.purchase_number,
            name="Полиграфия", status=status, **fields,
        )

    def _check(self, estimate):
        with patch.object(protocols, "fetch_protocol", return_value=QUOTATION_PROTOCOL):
            return services.check_protocol(estimate)

    def test_identified_winner_becomes_won(self):
        estimate = self._estimate(bid_number="ZK-420728")
        self.assertTrue(self._check(estimate))
        estimate.refresh_from_db()

        self.assertEqual(estimate.status, TenderEstimate.WON)
        self.assertEqual(estimate.actual_price, Decimal("1070000.00"))
        self.assertEqual(estimate.actual_reduction_percent, Decimal("40.60"))
        self.assertEqual(estimate.outcome_source, TenderEstimate.OUTCOME_AUTO)

    def test_identified_loser_becomes_lost(self):
        estimate = self._estimate(bid_price=Decimal("1148700"))
        self._check(estimate)
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, TenderEstimate.LOST)

    def test_unidentified_moves_to_published_result(self):
        estimate = self._estimate()
        self._check(estimate)
        estimate.refresh_from_db()

        self.assertEqual(estimate.status, TenderEstimate.PUBLISHED)
        self.assertEqual(estimate.protocol["participants"][0]["id"], "ZK-420728")
        self.assertEqual(estimate.actual_reduction_percent, Decimal("40.60"))

    def test_finished_estimate_keeps_its_status_but_gets_protocol(self):
        estimate = self._estimate(status=TenderEstimate.NOT_PARTICIPATED)
        self._check(estimate)
        estimate.refresh_from_db()

        self.assertEqual(estimate.status, TenderEstimate.NOT_PARTICIPATED)
        self.assertEqual(estimate.actual_price, Decimal("1070000.00"))

    def test_no_protocol_yet_only_marks_the_check(self):
        estimate = self._estimate()
        with patch.object(protocols, "fetch_protocol", return_value=None):
            self.assertFalse(services.check_protocol(estimate))
        estimate.refresh_from_db()

        self.assertEqual(estimate.status, TenderEstimate.PENDING)
        self.assertIsNotNone(estimate.protocol_checked_at)
        self.assertEqual(estimate.protocol, {})

    def test_entering_bid_on_published_result_decides_status(self):
        estimate = self._estimate()
        self._check(estimate)
        estimate.refresh_from_db()

        services.set_our_bid(estimate, bid_number="ZK-421419", bid_price=None)
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, TenderEstimate.LOST)


class RetryPendingProtocolsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("manager")

    def _estimate(self, number, *, closed_ago=timedelta(days=1), checked_ago=None, status=TenderEstimate.PENDING):
        now = timezone.now()
        tender = Tender.objects.create(purchase_number=number, collecting_finished_at=now - closed_ago)
        return TenderEstimate.objects.create(
            owner=self.user, tender=tender, tender_number=number, name=number, status=status,
            protocol_checked_at=now - checked_ago if checked_ago is not None else None,
        )

    def _checked_numbers(self):
        with patch.object(services, "check_protocol", return_value=False) as check:
            services.retry_pending_protocols(pause=0)
        return {call.args[0].tender_number for call in check.call_args_list}

    def test_bidding_after_deadline_and_finished_without_protocol_are_checked(self):
        self._estimate("closed")
        self._estimate("lost-earlier", status=TenderEstimate.LOST)
        self.assertEqual(self._checked_numbers(), {"closed", "lost-earlier"})

    def test_open_drafts_and_recent_checks_are_skipped(self):
        self._estimate("still-open", closed_ago=-timedelta(days=1))
        self._estimate("draft", status=TenderEstimate.DRAFT)
        self._estimate("just-checked", checked_ago=timedelta(minutes=5))
        self.assertEqual(self._checked_numbers(), set())


class ProtocolCardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        self.tender = Tender.objects.create(purchase_number="0172200004526000010", max_price=Decimal("1801340.00"))
        self.estimate = TenderEstimate.objects.create(
            owner=self.admin, tender=self.tender, tender_number=self.tender.purchase_number, name="Полиграфия",
            status=TenderEstimate.PUBLISHED, protocol=QUOTATION_PROTOCOL, bid_number="ZK-421419",
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

    def test_saving_bid_amount_decides_status(self):
        self.estimate.bid_number = ""
        self.estimate.save()

        self.client.post(f"/tender-selection/estimate/{self.estimate.pk}/bid/", {"bid_number": "", "bid_price": "1 070 000,00"})
        self.estimate.refresh_from_db()

        self.assertEqual(self.estimate.bid_price, Decimal("1070000.00"))
        self.assertEqual(self.estimate.status, TenderEstimate.WON)
