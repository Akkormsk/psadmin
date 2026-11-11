"""EAT integration safety tests: no network calls or AI."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from xml.etree import ElementTree as ET

from django.test import TestCase
from django.utils import timezone

from . import eat_api, eat_service
from .models import FilterSettings, Tender


class EatApiContractTests(TestCase):
    def test_request_xml_has_xsd_namespaces_and_unqualified_ext_system(self):
        body = eat_api._xml_message("requestOrderList", "300911501", "example")
        root = ET.fromstring(body)
        self.assertEqual(root.tag, f"{{{eat_api.OBJECT_NS}}}requestOrderList")
        self.assertEqual(root.attrib[f"{{{eat_api.EAT_NS}}}Version"], "2.0.0")
        self.assertEqual(root.find("extSystem").text, "300911501")

    def test_published_request_has_qualified_dates(self):
        now = timezone.now()
        body = eat_api._xml_message("requestOrdersListLitePublished", "300911501", "example",
                                    dates=(now-timedelta(days=7), now))
        root = ET.fromstring(body)
        self.assertIsNotNone(root.find(f"{{{eat_api.EAT_NS}}}dateFrom"))
        self.assertIsNotNone(root.find(f"{{{eat_api.EAT_NS}}}dateTo"))

    def test_only_explicit_type_purchase_confirms_law(self):
        def sample(text):
            root = ET.Element("responseProcessingResult")
            ET.SubElement(root, "typePurchase").text = text
            return root
        self.assertEqual(eat_api.legal_basis(sample("Закупка до 600 000 руб. (п.4 ч.1 ст.93 Закона №44-ФЗ)")), "fz44")
        self.assertIsNone(eat_api.legal_basis(sample("Малая закупка")))
        self.assertEqual(eat_api.legal_basis(sample("Закупка по 223-ФЗ")), "fz223")

    def test_keyword_match_min_price_and_deadline(self):
        now = timezone.now()
        def item(title, number, amount, expiry):
            root = ET.Element("orders")
            for key, value in {"regName": title, "regNumber": number, "contractSum": str(amount),
                               "startTime": (now-timedelta(days=2)).isoformat(),
                               "expireTime": expiry.isoformat()}.items():
                ET.SubElement(root, key).text = value
            return root
        rows = [
            item("Сувенирная продукция", "1", 410000, now + timedelta(days=1)),
            item("Картридж для принтера", "2", 700000, now + timedelta(days=1)),
            item("Футболки с нанесением", "3", 200000, now + timedelta(days=1)),
            item("Сувенирная продукция", "4", 400000, now - timedelta(days=1)),
        ]
        from .filtering import parse_terms
        actual = eat_api.shortlist(rows, minimum=Decimal(300000), include=parse_terms("сувенир,футбол"),
                                   exclude=[], now=now, days=7, mode="active_only")
        self.assertEqual([x["purchase_number"] for x in actual], ["1"])


class EatIntegrationTests(TestCase):
    def setUp(self):
        s = FilterSettings.load()
        s.include_words = "сувенир"
        s.exclude_words = ""
        s.min_price = Decimal("300000")
        s.save()
        self.now = timezone.now()

    def candidate(self, number="100000000000000001"):
        return {
            "purchase_number": number, "title": "Сувенирная продукция",
            "max_price": Decimal("410000"), "published_at": self.now - timedelta(days=1),
            "collecting_finished_at": self.now + timedelta(days=1),
        }

    def detail(self, law="44-ФЗ"):
        root = ET.Element("responseProcessingResult")
        for k, v in {"typePurchase": "Закупка по " + law,
                     "WWWReference": "https://agregatoreat.ru/purchases/example",
                     "OrderNumber": "100000000000000001"}.items():
            ET.SubElement(root, k).text = v
        return root

    @patch("tender_selection.eat_api.listing")
    @patch("tender_selection.eat_api.shortlist")
    @patch("tender_selection.eat_api.notification")
    def test_creates_and_reuses_tender_without_resetting_user_choices(self, notification, shortlist, listing):
        listing.return_value = ([], "active_only")
        shortlist.return_value = [self.candidate()]
        notification.return_value = self.detail()
        result = eat_service.run_eat_pull()
        self.assertTrue(result.ok)
        tender = Tender.objects.get(purchase_number="100000000000000001")
        self.assertEqual(tender.source, Tender.EAT)
        self.assertEqual(tender.law, "fz44")
        tender.review = Tender.INTERESTING
        tender.save(update_fields=["review"])
        eat_service.run_eat_pull()
        self.assertEqual(Tender.objects.filter(purchase_number=tender.purchase_number).count(), 1)
        tender.refresh_from_db()
        self.assertEqual(tender.review, Tender.INTERESTING)

    @patch("tender_selection.eat_api.listing")
    @patch("tender_selection.eat_api.shortlist")
    @patch("tender_selection.eat_api.notification")
    def test_unverified_law_does_not_create_tender(self, notification, shortlist, listing):
        listing.return_value = ([], "published")
        shortlist.return_value = [self.candidate()]
        notification.return_value = self.detail("неизвестному закону")
        eat_service.run_eat_pull()
        self.assertFalse(Tender.objects.filter(source=Tender.EAT).exists())

    @patch("tender_selection.eat_api.listing")
    @patch("tender_selection.eat_api.shortlist")
    @patch("tender_selection.eat_api.notification")
    def test_does_not_overwrite_an_eis_record(self, notification, shortlist, listing):
        existing = Tender.objects.create(law="fz44", purchase_number=self.candidate()["purchase_number"],
                                         source=Tender.EIS, title="Исходная ЕИС")
        listing.return_value = ([], "published")
        shortlist.return_value = [self.candidate()]
        notification.return_value = self.detail()
        eat_service.run_eat_pull()
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Исходная ЕИС")
        self.assertEqual(existing.source, Tender.EIS)
