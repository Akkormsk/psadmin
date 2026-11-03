from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

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

    def test_calculation_hides_expired_immediately_but_keeps_bidding_and_pinned_cards(self):
        for number, deadline, outcome, pin in [
            ('expired', -2, Tender.OUTCOME_DRAFT, False),
            ('future', 2, Tender.OUTCOME_DRAFT, False),
            ('bidding', -2, Tender.OUTCOME_PENDING, False),
            ('pinned', -2, Tender.OUTCOME_DRAFT, True),
        ]:
            tender = self._tender(number, number, days=deadline, status=Tender.PUSHED, outcome_status=outcome,
                kanban_visible_until=self.now + timedelta(days=1) if pin else None)
            TenderEstimate.objects.create(owner=self.admin, tender=tender, tender_number=number, name=number)
        response = self.client.get('/tender-selection/', {'view': 'kanban'})
        calculation = next(column for column in response.context['columns'] if column['key'] == 'calculation')
        self.assertEqual(len(calculation['cards']), 2)
        self.assertEqual(response.context['nav_counts']['board'], 3)
        self.assertNotContains(response, '>expired<')
        from tender_selection.views import _board_count
        self.assertEqual(_board_count(FilterSettings.load()), 3)

    def test_temporary_board_return_preserves_deadline_and_expires_normally(self):
        from .services import purge_stale
        tender = self._tender('0000000000000000001', 'Сувенир', days=-2, review=Tender.INTERESTING)
        original_deadline = tender.collecting_finished_at
        from django.core.management import call_command
        from io import StringIO
        output = StringIO()
        call_command('restore_tender_board', tender_number=tender.purchase_number, keep_hours=168, apply=True, stdout=output)
        self.assertIn('"visible": true', output.getvalue())
        purge_stale()
        response = self.client.get('/tender-selection/', {'view': 'kanban'})
        review = next(column for column in response.context['columns'] if column['key'] == 'review')
        self.assertEqual(len(review['cards']), 1)
        tender.refresh_from_db()
        self.assertEqual(tender.collecting_finished_at, original_deadline)
        self.assertFalse(tender.estimates.exists())
        tender.kanban_visible_until = self.now - timedelta(seconds=1)
        tender.save(update_fields=['kanban_visible_until'])
        purge_stale()
        tender.refresh_from_db()
        self.assertEqual(tender.status, Tender.DISMISSED)

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
        self._tender("1", "Срочно", days=0)
        self._tender("2", "Скоро", days=1)
        self._tender("3", "Спокойно", days=2)

        rows = {t.purchase_number: t for t in self._page().context["page_obj"].object_list}

        self.assertEqual((rows["1"].days_left, rows["1"].deadline_state), (0, "urgent"))
        self.assertEqual((rows["2"].days_left, rows["2"].deadline_state), (1, "soon"))
        self.assertEqual((rows["3"].days_left, rows["3"].deadline_state), (2, ""))

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

    def test_rows_have_an_id_for_immediate_removal_after_archiving(self):
        tender = self._tender("1", "Входящий", days=3)

        response = self._page()

        self.assertContains(response, f'data-tender-id="{tender.pk}"')

    def test_list_reuses_its_filtered_rows_for_the_incoming_counter(self):
        self._tender("1", "Входящий", days=3)

        with patch("tender_selection.views._incoming_count", side_effect=AssertionError("second full scan")):
            response = self._page()

        self.assertEqual(response.status_code, 200)

    def test_hidden_tenders_toggle_is_direct_and_names_what_it_shows(self):
        self._tender("expired", "Старый", days=-2)

        response = self._page()

        self.assertContains(response, "Показать скрытые · 1")
        self.assertNotContains(response, ">Фильтры<")
        self.assertContains(response, "Настроить отбор")

    def test_every_kanban_column_sorts_by_submission_deadline(self):
        early = self._tender("early", "Ближайший", days=1, review=Tender.INTERESTING, status=Tender.PUSHED)
        late = self._tender("late", "Дальний", days=5, review=Tender.INTERESTING, status=Tender.PUSHED)
        TenderEstimate.objects.create(owner=self.admin, tender=early, tender_number="early", name="Ближайший")
        TenderEstimate.objects.create(owner=self.admin, tender=late, tender_number="late", name="Дальний")

        response = self.client.get("/tender-selection/", {"view": "kanban"})
        calculation = next(column for column in response.context["columns"] if column["key"] == "calculation")

        self.assertEqual([card["pk"] for card in calculation["cards"]], [early.pk, late.pk])
