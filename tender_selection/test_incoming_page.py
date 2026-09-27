from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tenders.models import TenderEstimate

from .models import FilterSettings, Organization, Tender


class IncomingPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(self.admin)
        settings = FilterSettings.load()
        settings.include_words, settings.exclude_words, settings.min_price = "", "", 0
        settings.save()
        self.now = timezone.now()

    def _tender(self, number, title, *, days, **fields):
        return Tender.objects.create(
            purchase_number=number, title=title, max_price=Decimal("500000"),
            collecting_finished_at=self.now + timedelta(days=days, hours=1), **fields,
        )

    def _page(self, **params):
        return self.client.get("/tender-selection/", {"view": "list", **params})

    def test_search_matches_title_number_and_customer(self):
        Organization.objects.create(inn="7700000001", name="Спорткомитет")
        self._tender("0322000047", "Сувенирная продукция", days=3, customer_inn="7700000001")
        self._tender("0134200029", "Папки картонные", days=3)

        def numbers(q):
            return [t.purchase_number for t in self._page(q=q).context["page_obj"].object_list]

        self.assertEqual(numbers("сувенир"), ["0322000047"])
        self.assertEqual(numbers("0134200"), ["0134200029"])
        self.assertEqual(numbers("спорткомитет"), ["0322000047"])

    def test_rows_know_days_left_and_urgency(self):
        self._tender("1", "Срочно", days=1)
        self._tender("2", "Скоро", days=2)
        self._tender("3", "Спокойно", days=3)

        rows = {t.purchase_number: t for t in self._page().context["page_obj"].object_list}

        self.assertEqual((rows["1"].days_left, rows["1"].deadline_state), (1, "urgent"))
        self.assertEqual((rows["2"].days_left, rows["2"].deadline_state), (2, "soon"))
        self.assertEqual((rows["3"].days_left, rows["3"].deadline_state), (3, ""))

    def test_segments_show_incoming_and_board_counts(self):
        self._tender("1", "Входящий", days=3)
        board_tender = self._tender("2", "В работе", days=3, review=Tender.INTERESTING, status=Tender.PUSHED)
        TenderEstimate.objects.create(owner=self.admin, tender=board_tender, tender_number="2", name="Расчёт")

        for view in ("list", "kanban"):
            response = self.client.get("/tender-selection/", {"view": view})
            self.assertEqual(response.context["nav_counts"], {"incoming": 1, "board": 1})

    def test_page_has_bulk_selection_and_no_word_editor(self):
        self._tender("1", "Входящий", days=3)
        response = self._page()

        self.assertContains(response, 'data-bulk-select')
        self.assertContains(response, 'class="ts-seg')
        self.assertNotContains(response, "Плюс/минус-слова")
