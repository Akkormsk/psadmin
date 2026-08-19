from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from payroll.models import OrderRecord

from .models import FinancialPeriod, KpiTier, ManagerKpiRate, ManagerSettings, PayrollLine


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


class PersonalFinanceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.manager = user_model.objects.create_user(
            username="manager",
            first_name="Ирина",
            last_name="Менеджер",
            password="test-password",
        )
        self.other_manager = user_model.objects.create_user(
            username="other",
            first_name="Павел",
            last_name="Другой",
            password="test-password",
        )
        ManagerSettings.objects.filter(user=self.manager).update(shift_rate="1000", leave_shift_rate="500")
        ManagerSettings.objects.filter(user=self.other_manager).update(shift_rate="2000", leave_shift_rate="1000")
        for threshold in (0, 100000, 200000, 300000):
            tier = KpiTier.objects.create(threshold=threshold)
            ManagerKpiRate.objects.create(manager=self.manager, tier=tier, percent="15")
            ManagerKpiRate.objects.create(manager=self.other_manager, tier=tier, percent="10")
        OrderRecord.objects.create(
            order_number="1001",
            gross_profit="200000",
            record_type=OrderRecord.RECORD_ORDER,
            accounting_period="2026-08",
            manager=self.manager,
            created_by=self.manager,
        )
        OrderRecord.objects.create(
            order_number="1002",
            gross_profit="10000",
            record_type=OrderRecord.RECORD_DESIGN,
            accounting_period="2026-08",
            manager=self.manager,
            created_by=self.manager,
        )
        self.client.force_login(self.manager)

    def test_manager_sees_only_own_automatically_calculated_payroll(self):
        response = self.client.get(reverse("finance:calculation"), {"period": "2026-08"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ирина Менеджер")
        self.assertNotContains(response, "Павел Другой")
        self.assertNotContains(response, "Прибыль компании")
        line = PayrollLine.objects.get(period__code="2026-08", manager=self.manager)
        self.assertEqual(line.order_profit, 200000)
        self.assertEqual(line.kpi_bonus, 30000)
        self.assertEqual(line.design_amount, 10000)
        self.assertEqual(line.design_percent, 90)
        rendered_line = response.context["lines"][0]
        self.assertEqual(rendered_line.design_pay, 9000)
        self.assertEqual(rendered_line.total, 39000)
        self.assertNotContains(response, f'name="design_amount_{line.pk}"')

    def test_manager_can_change_only_own_shift_counts(self):
        self.client.get(reverse("finance:calculation"), {"period": "2026-08"})
        own_line = PayrollLine.objects.get(period__code="2026-08", manager=self.manager)
        other_line = PayrollLine.objects.get(period__code="2026-08", manager=self.other_manager)
        own_line.deductions = 700
        own_line.advance = 5000
        own_line.save(update_fields=("deductions", "advance"))

        self.client.post(
            reverse("finance:calculation"),
            {
                "period": "2026-08",
                "action": "close",
                f"work_shifts_{own_line.pk}": "12",
                f"leave_shifts_{own_line.pk}": "2",
                f"deductions_{own_line.pk}": "0",
                f"advance_{own_line.pk}": "0",
                f"work_shifts_{other_line.pk}": "31",
            },
        )

        own_line.refresh_from_db()
        other_line.refresh_from_db()
        self.assertEqual(own_line.work_shifts, 12)
        self.assertEqual(own_line.leave_shifts, 2)
        self.assertEqual(own_line.deductions, 700)
        self.assertEqual(own_line.advance, 5000)
        self.assertEqual(other_line.work_shifts, 0)
        self.assertFalse(own_line.period.is_closed)

    def test_admin_can_reorder_employees_and_set_design_percent(self):
        admin = get_user_model().objects.create_superuser(username="finance-admin", password="test-password")
        self.client.force_login(admin)

        response = self.client.post(
            reverse("finance:manager_settings"),
            {
                "employee_order": f"{self.other_manager.pk},{self.manager.pk}",
                f"shift_rate_{self.manager.pk}": "1000",
                f"leave_rate_{self.manager.pk}": "500",
                f"design_percent_{self.manager.pk}": "80",
                f"shift_rate_{self.other_manager.pk}": "2000",
                f"leave_rate_{self.other_manager.pk}": "1000",
                f"design_percent_{self.other_manager.pk}": "75",
            },
        )

        self.assertRedirects(response, reverse("finance:manager_settings"))
        manager_settings = ManagerSettings.objects.get(user=self.manager)
        other_settings = ManagerSettings.objects.get(user=self.other_manager)
        self.assertEqual(manager_settings.design_percent, 80)
        self.assertEqual(other_settings.design_percent, 75)
        self.assertLess(other_settings.sort_order, manager_settings.sort_order)

        calculation = self.client.get(reverse("finance:calculation"), {"period": "2026-08"})
        lines = list(calculation.context["lines"])
        self.assertEqual([line.manager_id for line in lines[:2]], [self.other_manager.pk, self.manager.pk])
        self.assertEqual(lines[-1].kind, PayrollLine.PRINTER)
