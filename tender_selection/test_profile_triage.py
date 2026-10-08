from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import FilterSettings, Tender, TenderDismissalFeedback


class ProfileTriageTests(TestCase):
    def test_gemini_marks_explainable_profile_verdicts_without_hiding(self):
        from .profile_triage import triage_tenders

        settings = FilterSettings.load()
        settings.profile_triage_enabled = True
        settings.save(update_fields=["profile_triage_enabled"])
        clear = Tender.objects.create(purchase_number="1", title="Поставка сувенирной продукции")
        doubt = Tender.objects.create(purchase_number="2", title="Изготовление брендированных материалов")
        unrelated = Tender.objects.create(purchase_number="3", title="Поставка полиграфического оборудования")
        with patch("tender_selection.profile_triage.chat_json", return_value={"data": {"items": [
            {"id": clear.pk, "verdict": "fit", "confidence": 0.92, "reason": "Сувенирная продукция входит в профиль."},
            {"id": doubt.pk, "verdict": "review", "confidence": 0.51, "reason": "Нужен состав продукции."},
            {"id": unrelated.pk, "verdict": "not_fit", "confidence": 0.08, "reason": "Оборудование, а не полиграфическая продукция."},
        ]}, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}):
            triage_tenders([clear.pk, doubt.pk, unrelated.pk])

        clear.refresh_from_db()
        doubt.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(clear.profile_signal, Tender.PROFILE_SIGNAL_CLEAR)
        self.assertEqual(doubt.profile_signal, Tender.PROFILE_SIGNAL_DOUBT)
        self.assertEqual(unrelated.profile_signal, Tender.PROFILE_SIGNAL_NOT_PROFILE)
        self.assertIsNotNone(unrelated.profile_checked_at)
        self.assertEqual(unrelated.profile_model, "gemini/gemini-3.1-flash-lite")
        self.assertIn("Оборудование", unrelated.profile_reason)
        self.assertEqual(unrelated.status, Tender.NEW)

    def test_invalid_agent_item_is_left_unchecked_for_safe_retry(self):
        from .profile_triage import triage_tenders

        settings = FilterSettings.load()
        settings.profile_triage_enabled = True
        settings.save(update_fields=["profile_triage_enabled"])
        tender = Tender.objects.create(purchase_number="bad-agent", title="Поставка пакетов для стерилизации")
        with patch("tender_selection.profile_triage.chat_json", return_value={"data": {"items": [
            {"id": tender.pk, "verdict": "other", "confidence": "unknown"},
        ]}, "usage": {}}):
            self.assertEqual(triage_tenders([tender.pk]), 0)

        tender.refresh_from_db()
        self.assertIsNone(tender.profile_checked_at)

    def test_list_shows_gemini_reason_without_hover(self):
        settings = FilterSettings.load()
        settings.min_price = 0
        settings.include_words = ""
        settings.exclude_words = ""
        settings.save(update_fields=["min_price", "include_words", "exclude_words"])
        tender = Tender.objects.create(
            purchase_number="reason-ui", title="Полиграфическое оборудование",
            profile_signal=Tender.PROFILE_SIGNAL_NOT_PROFILE,
            profile_reason="Это оборудование, а не заказ на изготовление продукции.",
        )
        self.client.force_login(get_user_model().objects.create_superuser("reason-ui", password="x"))

        response = self.client.get(reverse("tender_selection:list") + "?view=list", secure=True)

        self.assertContains(response, f'<span class="ts-profile-reason">{tender.profile_reason}</span>', html=True)

    def test_dismiss_reason_is_recorded_only_for_an_incoming_tender(self):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser("admin", password="x")
        tender = Tender.objects.create(purchase_number="4", title="Полиграфическое оборудование")
        self.client.force_login(admin)

        response = self.client.post(f"/tender-selection/{tender.pk}/dismiss/", {"reason": "not_profile"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(TenderDismissalFeedback.objects.get(tender=tender).reason, TenderDismissalFeedback.NOT_PROFILE)
