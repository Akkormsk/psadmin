from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import OrderRecord


class OrderRecordAccessTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(
            username="admin", password="password"
        )
        self.manager = user_model.objects.create_user(
            username="manager", first_name="Ирина", last_name="Иванова", password="password"
        )
        self.other_manager = user_model.objects.create_user(
            username="other-manager", first_name="Ольга", last_name="Королева", password="password"
        )
        self.manager_record = OrderRecord.objects.create(
            order_number="1001",
            gross_profit="150000.00",
            accounting_period="2026-04",
            manager=self.manager,
            created_by=self.manager,
        )
        self.other_record = OrderRecord.objects.create(
            order_number="1002",
            gross_profit="250000.00",
            accounting_period="2026-05",
            manager=self.other_manager,
            created_by=self.other_manager,
        )

    def test_manager_sees_only_own_records(self):
        self.client.force_login(self.manager)

        response = self.client.get(reverse("payroll:orderrecord_list"))

        self.assertContains(response, self.manager_record.order_number)
        self.assertNotContains(response, self.other_record.order_number)
        self.assertContains(response, "Валовая прибыль по выборке")
        self.assertEqual(response.context["total_gross_profit"], Decimal("150000.00"))

    def test_admin_sees_all_records_and_can_filter_by_manager_and_period(self):
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("payroll:orderrecord_list"),
            {"manager": self.manager.pk, "period": "2026-04"},
        )

        self.assertContains(response, self.manager_record.order_number)
        self.assertNotContains(response, self.other_record.order_number)
        self.assertContains(response, "Ирина Иванова")
        self.assertContains(response, "Валовая прибыль по выборке")

    def test_admin_can_delete_any_record(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("payroll:orderrecord_delete", args=[self.manager_record.pk])
        )

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.assertFalse(OrderRecord.objects.filter(pk=self.manager_record.pk).exists())

    def test_manager_can_create_design_record_and_sees_badge(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("payroll:orderrecord_create"),
            {
                "record_type": OrderRecord.RECORD_DESIGN,
                "order_number": "1003",
                "gross_profit": "12000",
                "accounting_period": "2026-08",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = OrderRecord.objects.get(order_number="1003")
        self.assertEqual(record.record_type, OrderRecord.RECORD_DESIGN)
        self.assertContains(response, "Макет")

    def test_design_accepts_text_instead_of_order_number(self):
        self.client.force_login(self.manager)

        response = self.client.post(reverse("payroll:orderrecord_create"), {
            "record_type": OrderRecord.RECORD_DESIGN,
            "order_number": "Макет вывески у входа",
            "gross_profit": "12000",
            "accounting_period": "2026-08",
        })

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.assertTrue(OrderRecord.objects.filter(order_number="Макет вывески у входа").exists())

    def test_order_rejects_text_in_order_number(self):
        self.client.force_login(self.manager)

        response = self.client.post(reverse("payroll:orderrecord_create"), {
            "record_type": OrderRecord.RECORD_ORDER,
            "order_number": "Заказ без номера",
            "gross_profit": "12000",
            "accounting_period": "2026-08",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Номер заказа должен содержать только цифры")
        self.assertFalse(OrderRecord.objects.filter(order_number="Заказ без номера").exists())

    def test_manager_can_edit_own_record(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("payroll:orderrecord_update", args=[self.manager_record.pk]),
            {
                f"edit-{self.manager_record.pk}-record_type": OrderRecord.RECORD_DESIGN,
                f"edit-{self.manager_record.pk}-order_number": "2001",
                f"edit-{self.manager_record.pk}-gross_profit": "18000",
                f"edit-{self.manager_record.pk}-accounting_period": "2026-08",
            },
        )

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.manager_record.refresh_from_db()
        self.assertEqual(self.manager_record.manager, self.manager)
        self.assertEqual(self.manager_record.record_type, OrderRecord.RECORD_DESIGN)
        self.assertEqual(self.manager_record.order_number, "2001")
        self.assertEqual(self.manager_record.gross_profit, 18000)

    def test_manager_cannot_edit_another_managers_record(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("payroll:orderrecord_update", args=[self.other_record.pk]),
            {
                f"edit-{self.other_record.pk}-record_type": OrderRecord.RECORD_DESIGN,
                f"edit-{self.other_record.pk}-order_number": "9999",
                f"edit-{self.other_record.pk}-gross_profit": "1",
                f"edit-{self.other_record.pk}-accounting_period": "2026-08",
            },
        )

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.other_record.refresh_from_db()
        self.assertEqual(self.other_record.order_number, "1002")

    def test_admin_can_reassign_record_to_another_manager(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("payroll:orderrecord_update", args=[self.manager_record.pk]),
            {
                f"edit-{self.manager_record.pk}-record_type": OrderRecord.RECORD_ORDER,
                f"edit-{self.manager_record.pk}-order_number": self.manager_record.order_number,
                f"edit-{self.manager_record.pk}-gross_profit": self.manager_record.gross_profit,
                f"edit-{self.manager_record.pk}-accounting_period": self.manager_record.accounting_period,
                f"edit-{self.manager_record.pk}-manager": self.other_manager.pk,
            },
        )

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.manager_record.refresh_from_db()
        self.assertEqual(self.manager_record.manager, self.other_manager)

# Create your tests here.
