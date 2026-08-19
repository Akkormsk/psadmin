from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import FinancialPeriod


class CalculationPeriodOptionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)

    @patch("finance.views.timezone.localdate", return_value=date(2026, 8, 19))
    def test_current_period_remains_available_when_viewing_previous_month(self, _localdate):
        response = self.client.get(reverse("finance:calculation"), {"period": "2026-07"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="2026-08"')
        self.assertContains(response, '<option value="2026-07" selected')

    @patch("finance.views.timezone.localdate", return_value=date(2026, 8, 19))
    def test_existing_period_without_orders_remains_available(self, _localdate):
        FinancialPeriod.objects.create(code="2026-06")

        response = self.client.get(reverse("finance:calculation"), {"period": "2026-07"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="2026-06"')
