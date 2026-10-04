from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from . import services, word_audit
from .models import FilterSettings, IncomingTrace, Tender, TenderDismissalFeedback, WordAudit


def _settings(plus="футболк, кружк", minus="медицин"):
    settings = FilterSettings.load()
    settings.include_words, settings.exclude_words = plus, minus
    settings.save()
    return settings


class IncomingTraceTests(TestCase):
    def test_purge_archives_shown_tenders_and_traces_only_the_hidden_ones(self):
        """Показанные (прошли плюс/минус-фильтр) — источник статистики по
        торгам, поэтому архивируются насовсем, а не удаляются. Скрытые
        фильтром по-прежнему удаляются, для них остаётся только след
        IncomingTrace (для аудита слов)."""
        _settings()
        now = timezone.now()
        Tender.objects.create(purchase_number="1", title="Футболки хлопковые", collecting_finished_at=now - timedelta(days=30))
        Tender.objects.create(purchase_number="2", title="Медицинские бланки", collecting_finished_at=now - timedelta(days=30))

        services.purge_stale()

        shown = Tender.objects.get(purchase_number="1")
        self.assertEqual(shown.status, Tender.DISMISSED)
        self.assertIsNotNone(shown.archived_at)
        self.assertFalse(Tender.objects.filter(purchase_number="2").exists())
        self.assertFalse(IncomingTrace.objects.filter(purchase_number="1").exists())
        trace = IncomingTrace.objects.get(purchase_number="2")
        self.assertTrue(trace.filtered_out)


class WordStatsTests(TestCase):
    def setUp(self):
        _settings(plus="футболк, кружк, зонт", minus="медицин")
        now = timezone.now()
        Tender.objects.create(purchase_number="t1", title="Футболки с логотипом", review=Tender.INTERESTING)
        dismissed = Tender.objects.create(purchase_number="d1", title="Кружки фарфоровые", status=Tender.DISMISSED, opened_at=now)
        TenderDismissalFeedback.objects.create(tender=dismissed, reason=TenderDismissalFeedback.NOT_PROFILE)
        IncomingTrace.objects.create(purchase_number="i1", title="Кружки термо", filtered_out=False)
        Tender.objects.create(purchase_number="h1", title="Медицинские футболки", collecting_finished_at=now + timedelta(days=3))
        Tender.objects.create(purchase_number="h2", title="Шопперы с печатью", collecting_finished_at=now + timedelta(days=3))

    def test_counts_what_each_word_let_in_and_what_became_of_it(self):
        stats = word_audit.word_stats()
        plus = {row["word"]: row for row in stats["plus"]}

        self.assertEqual((plus["футболк"]["taken"], plus["футболк"]["dismissed"]), (1, 0))
        self.assertEqual((plus["кружк"]["taken"], plus["кружк"]["dismissed"], plus["кружк"]["ignored"]), (0, 1, 1))
        self.assertEqual(plus["зонт"]["passed"], 0)
        minus = {row["word"]: row for row in stats["minus"]}
        self.assertEqual(minus["медицин"]["hidden"], 1)

    def test_archived_untouched_tender_is_not_counted_as_a_manual_rejection(self):
        """purge_stale теперь архивирует показанные, но нетронутые тендеры
        (status=DISMISSED, opened_at пусто) — это не сигнал «не наш профиль»,
        в отличие от ручного отказа с открытой карточкой."""
        Tender.objects.create(purchase_number="arch1", title="Кружки сувенирные", status=Tender.DISMISSED)

        stats = word_audit.word_stats()

        plus = {row["word"]: row for row in stats["plus"]}
        self.assertEqual(plus["кружк"]["dismissed"], 1)  # только d1 (открытый вручную)
        self.assertEqual(plus["кружк"]["ignored"], 2)  # i1 (трасса) + arch1 (архив, не открыт)

    def test_effects_of_a_proposed_word_are_computed_by_backend(self):
        self.assertEqual(word_audit.term_effects("шоппер", minus=False)["opens_hidden"], 1)
        self.assertEqual(word_audit.term_effects("футболк", minus=False)["opens_hidden"], 0)  # минус-слово сильнее
        effects = word_audit.term_effects("логотип", minus=True)
        self.assertEqual(effects["hits_taken"], 1)


class AuditRunTests(TestCase):
    def setUp(self):
        _settings()
        self.user = get_user_model().objects.create_superuser("admin", password="x")
        Tender.objects.create(purchase_number="t1", title="Футболки с логотипом", review=Tender.INTERESTING)
        Tender.objects.create(purchase_number="h1", title="Поставка шопперов с печатью", collecting_finished_at=timezone.now() + timedelta(days=3))

    def test_run_sends_titles_and_stores_suggestions_with_backend_effects(self):
        answer = {
            "data": {
                "add_plus": [{"word": "шоппер", "why": "сумки с печатью — наш профиль", "examples": ["Поставка шопперов с печатью"]}],
                "add_minus": [{"word": "логотип", "why": "шум"}],
                "remove_plus": [], "remove_minus": [],
            },
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        with patch.object(word_audit, "chat_json", return_value=answer) as chat:
            audit = word_audit.run_audit(self.user)

        prompt = chat.call_args.args[1]
        self.assertIn("Футболки с логотипом", prompt)
        self.assertIn("Поставка шопперов с печатью", prompt)
        self.assertEqual(WordAudit.objects.count(), 1)
        plus = audit.result["add_plus"][0]
        self.assertEqual((plus["word"], plus["effects"]["opens_hidden"]), ("шоппер", 1))
        self.assertEqual(audit.result["add_minus"][0]["effects"]["hits_taken"], 1)

    def test_other_dismissals_are_not_sent_as_profile_rejections(self):
        profile = Tender.objects.create(purchase_number="p1", title="Полиграфическое оборудование")
        other = Tender.objects.create(purchase_number="o1", title="Поставка футболок", status=Tender.DISMISSED, opened_at=timezone.now())
        TenderDismissalFeedback.objects.create(tender=profile, reason=TenderDismissalFeedback.NOT_PROFILE)
        TenderDismissalFeedback.objects.create(tender=other, reason=TenderDismissalFeedback.OTHER)
        with patch.object(word_audit, "chat_json", return_value={"data": {}, "usage": {}}) as chat:
            word_audit.run_audit(self.user)

        prompt = chat.call_args.args[1]
        self.assertIn("ПОДТВЕРЖДЕНО: НЕ НАШ ПРОФИЛЬ", prompt)
        self.assertIn("Полиграфическое оборудование", prompt)
        self.assertNotIn("Поставка футболок", prompt)


class ApplySuggestionsTests(TestCase):
    def test_apply_adds_and_removes_only_selected_words(self):
        settings = _settings(plus="футболк, кружк", minus="медицин")

        word_audit.apply_words(add_plus=["шоппер", "Футболк"], add_minus=["бланк"], remove_plus=["кружк"], remove_minus=[])
        settings.refresh_from_db()

        self.assertEqual(settings.include_words, "футболк\nшоппер")
        self.assertEqual(settings.exclude_words, "медицин\nбланк")

    def test_audit_page_and_apply_endpoint(self):
        _settings()
        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)

        self.assertEqual(self.client.get("/tender-selection/word-audit/").status_code, 200)
        self.client.post("/tender-selection/word-audit/apply/", {"add_plus": ["шоппер"]})
        self.assertIn("шоппер", FilterSettings.load().include_words)

    def test_applied_suggestion_disappears_from_the_pending_audit(self):
        """Принятое предложение больше не должно всплывать при следующем
        открытии страницы — иначе можно применить его повторно."""
        _settings()
        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)
        WordAudit.objects.create(result={
            "add_plus": [{"word": "шоппер", "why": "часто встречается", "examples": []}],
            "add_minus": [{"word": "бланк", "why": "мусор", "examples": []}],
            "remove_plus": [], "remove_minus": [],
        })

        response = self.client.post("/tender-selection/word-audit/apply/", {"add_plus": ["шоппер"]}, follow=True)

        self.assertNotContains(response, 'value="шоппер"')
        audit = WordAudit.objects.first()
        self.assertEqual(audit.result["add_plus"], [])
        self.assertEqual(len(audit.result["add_minus"]), 1)

    def test_topic_grouped_suggestions_use_the_same_add_plus_mechanism(self):
        """Пропущенная тематика — это просто несколько add_plus-предложений с
        одинаковым непустым ``topic`` (группировка только для отображения),
        отдельного механизма/дублирования верх-низ больше нет; применяются и
        пропадают они так же, как обычные add_plus."""
        answer = {
            "data": {
                "add_plus": [
                    {"word": "бланочн", "topic": "Полиграфия", "why": "профиль", "examples": []},
                    {"word": "журнал", "topic": "Полиграфия", "why": "профиль", "examples": []},
                ],
                "add_minus": [], "remove_plus": [], "remove_minus": [],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        _settings()
        admin = get_user_model().objects.create_superuser("admin", password="x")
        with patch.object(word_audit, "chat_json", return_value=answer):
            audit = word_audit.run_audit(admin)
        self.assertEqual([row["topic"] for row in audit.result["add_plus"]], ["Полиграфия", "Полиграфия"])

        self.client.force_login(admin)
        self.client.post("/tender-selection/word-audit/apply/", {"add_plus": ["бланочн"]})

        audit.refresh_from_db()
        self.assertEqual([row["word"] for row in audit.result["add_plus"]], ["журнал"])
