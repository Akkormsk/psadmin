from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import OrderRecord


class FinancialAccountingTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(username="admin", password="password")
        self.manager = user_model.objects.create_user(username="manager", password="password")
        OrderRecord.objects.create(
            order_number="1001", gross_profit="150000.00", accounting_period="2026-04",
            manager=self.manager, created_by=self.manager,
        )

    def test_admin_sees_company_profit(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("payroll:index"), {"period": "2026-04"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Прибыль компании")
        self.assertEqual(response.context["order_total"], 150000)

    def test_manager_has_no_access(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("payroll:index")).status_code, 302)
