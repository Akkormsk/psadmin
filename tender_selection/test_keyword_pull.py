from unittest.mock import patch

from django.test import TestCase

from . import gosplan, services
from .models import FilterSettings, Tender


class RunKeywordPullTests(TestCase):
    """Обход не по категориям ОКПД2, а по тем же плюс-словам, что уже
    используются для показа/скрытия «Входящих» — находит и то, что
    классификатор в принципе не видит (нет кода ОКПД2 или код не тот)."""

    def setUp(self):
        self.settings = FilterSettings.load()
        self.settings.include_words = "сувенир, футболк+принт"
        self.settings.save()

    def _record(self, number, **extra):
        return {"purchase_number": number, "object_info": "Тест", "max_price": 400000, **extra}

    def test_searches_by_first_stem_of_each_entry(self):
        with patch.object(gosplan, "iter_purchases", return_value=iter([])) as iter_purchases:
            services.run_keyword_pull(max_requests=10, pause=0)

        queried = [call.kwargs["params"].get("object_info") for call in iter_purchases.call_args_list]
        # "футболк+принт" -> ищем по первому слову ("футболк"), не по литералу с плюсом.
        self.assertEqual(queried, ["сувенир", "футболк"])

    def test_creates_tenders_from_matched_records(self):
        with patch.object(gosplan, "iter_purchases", side_effect=[iter([self._record("111")]), iter([])]):
            services.run_keyword_pull(max_requests=10, pause=0)

        self.assertTrue(Tender.objects.filter(purchase_number="111").exists())

    def test_cursor_advances_and_wraps_around(self):
        with patch.object(gosplan, "iter_purchases", return_value=iter([])):
            services.run_keyword_pull(max_requests=1, pause=0)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.keyword_pull_cursor, 1)

        with patch.object(gosplan, "iter_purchases", return_value=iter([])):
            services.run_keyword_pull(max_requests=1, pause=0)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.keyword_pull_cursor, 0)

    def test_resumes_from_the_saved_cursor(self):
        self.settings.keyword_pull_cursor = 1
        self.settings.save()

        with patch.object(gosplan, "iter_purchases", return_value=iter([])) as iter_purchases:
            services.run_keyword_pull(max_requests=1, pause=0)

        self.assertEqual(iter_purchases.call_args_list[0].kwargs["params"].get("object_info"), "футболк")

    def test_no_include_words_is_a_harmless_noop(self):
        self.settings.include_words = ""
        self.settings.save()

        with patch.object(gosplan, "iter_purchases") as iter_purchases:
            run = services.run_keyword_pull(max_requests=10, pause=0)

        iter_purchases.assert_not_called()
        self.assertTrue(run.ok)

    def test_gosplan_error_is_recorded_not_raised(self):
        with patch.object(gosplan, "iter_purchases", side_effect=gosplan.GosplanError("boom")):
            run = services.run_keyword_pull(max_requests=10, pause=0)

        self.assertFalse(run.ok)
        self.assertIn("boom", run.error)
