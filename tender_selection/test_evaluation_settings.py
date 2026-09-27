from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from tenders.models import TenderSettings

from .models import FilterSettings

URL = "/tender-selection/evaluation-settings/"
VALID = {
    "risk_warning_days": "10", "risk_critical_days": "5",
    "roi_good_percent": "35", "roi_thin_percent": "20", "vat_rate": "7",
}


class EvaluationSettingsTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser("admin", password="x")
        self.client.force_login(admin)

    def test_kanban_links_to_settings(self):
        response = self.client.get("/tender-selection/?view=kanban")
        self.assertContains(response, f'href="{URL}"')

    def test_page_shows_current_values(self):
        response = self.client.get(URL)
        self.assertContains(response, 'name="roi_good_percent" value="30')
        self.assertContains(response, 'name="risk_critical_days" value="7"')

    def test_saving_updates_risk_roi_and_vat(self):
        self.client.post(URL, VALID)

        filters = FilterSettings.load()
        tender_settings = TenderSettings.objects.get(pk=1)
        self.assertEqual((filters.risk_warning_days, filters.risk_critical_days), (10, 5))
        self.assertEqual((tender_settings.roi_good_percent, tender_settings.roi_thin_percent), (Decimal("35"), Decimal("20")))
        self.assertEqual(tender_settings.vat_rate, Decimal("7"))

    def test_inconsistent_thresholds_are_rejected(self):
        for broken in ({"risk_critical_days": "10"}, {"roi_thin_percent": "35"}, {"vat_rate": "abc"}):
            response = self.client.post(URL, {**VALID, **broken})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(FilterSettings.load().risk_warning_days, 14)
            self.assertFalse(TenderSettings.objects.filter(roi_good_percent=Decimal("35")).exists())

    def test_filter_settings_form_no_longer_touches_risk_days(self):
        filters = FilterSettings.load()
        filters.risk_warning_days, filters.risk_critical_days = 20, 9
        filters.save()

        self.client.post("/tender-selection/settings/", {"min_price": "300000", "window_days": "7"})

        filters.refresh_from_db()
        self.assertEqual((filters.risk_warning_days, filters.risk_critical_days), (20, 9))
