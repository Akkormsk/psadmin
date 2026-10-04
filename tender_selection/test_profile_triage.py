from unittest.mock import patch

from django.test import TestCase

from .models import FilterSettings, Tender, TenderDismissalFeedback


class ProfileTriageTests(TestCase):
    def test_marks_only_uncertain_or_unrelated_tenders(self):
        from .profile_triage import triage_tenders

        settings = FilterSettings.load()
        settings.profile_triage_enabled = True
        settings.save(update_fields=["profile_triage_enabled"])
        clear = Tender.objects.create(purchase_number="1", title="Поставка сувенирной продукции")
        doubt = Tender.objects.create(purchase_number="2", title="Изготовление брендированных материалов")
        unrelated = Tender.objects.create(purchase_number="3", title="Поставка полиграфического оборудования")
        with patch("tender_selection.profile_triage.decide_matrix", return_value=({
            f"t{clear.pk}": {"noul": 0.92}, f"t{doubt.pk}": {"noul": 0.51}, f"t{unrelated.pk}": {"noul": 0.08},
        }, {"prompt_tokens": 1, "completion_tokens": 1})):
            triage_tenders([clear.pk, doubt.pk, unrelated.pk])

        clear.refresh_from_db()
        doubt.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(clear.profile_signal, Tender.PROFILE_SIGNAL_CLEAR)
        self.assertEqual(doubt.profile_signal, Tender.PROFILE_SIGNAL_DOUBT)
        self.assertEqual(unrelated.profile_signal, Tender.PROFILE_SIGNAL_NOT_PROFILE)
        self.assertIsNotNone(unrelated.profile_checked_at)

    def test_dismiss_reason_is_recorded_only_for_an_incoming_tender(self):
        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.create_superuser("admin", password="x")
        tender = Tender.objects.create(purchase_number="4", title="Полиграфическое оборудование")
        self.client.force_login(admin)

        response = self.client.post(f"/tender-selection/{tender.pk}/dismiss/", {"reason": "not_profile"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(TenderDismissalFeedback.objects.get(tender=tender).reason, TenderDismissalFeedback.NOT_PROFILE)
