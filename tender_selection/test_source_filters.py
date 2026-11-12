from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.urls import reverse
from .models import FilterSettings, Tender
from . import services, eat_service


class SourceFiltersTests(TestCase):
    def test_sources_validate_independently_of_laws(self):
        services.save_selection_settings({"sources": ["eat"], "laws": ["fz44"]})
        settings = FilterSettings.load()
        self.assertEqual(settings.sources, ["eat"])
        with self.assertRaises(ValueError):
            services.save_selection_settings({"sources": ["unknown"]})

    def test_disabled_eis_does_not_request_upstream(self):
        services.save_selection_settings({"sources": ["eat"]})
        with patch("tender_selection.gosplan.iter_purchases") as request:
            self.assertTrue(services.run_pull().ok)
            self.assertTrue(services.run_keyword_pull().ok)
        request.assert_not_called()

    def test_disabled_eat_does_not_request_upstream(self):
        services.save_selection_settings({"sources": ["eis"]})
        with patch("tender_selection.eat_api.listing") as request:
            self.assertIsNone(eat_service.run_eat_pull())
        request.assert_not_called()

    def test_incoming_source_filter_does_not_change_saved_records(self):
        admin = get_user_model().objects.create_superuser("source-admin", "source@example.test", "test")
        self.client.force_login(admin)
        for source in ("eis", "eat"):
            Tender.objects.create(purchase_number=source, source=source, title=source,
                                  collecting_finished_at=timezone.now() + timedelta(days=2))
        response = self.client.get(reverse("tender_selection:list"), {"view": "list", "source": "eat"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["source_filter"], "eat")
        self.assertEqual(Tender.objects.count(), 2)
