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
            username="manager", password="password"
        )
        self.other_manager = user_model.objects.create_user(
            username="other-manager", password="password"
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

    def test_admin_sees_all_records_and_can_filter_by_manager_and_period(self):
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("payroll:orderrecord_list"),
            {"manager": self.manager.pk, "period": "2026-04"},
        )

        self.assertContains(response, self.manager_record.order_number)
        self.assertNotContains(response, self.other_record.order_number)
        self.assertContains(response, self.manager.username)
        self.assertContains(response, "Валовая прибыль по выборке")

    def test_admin_can_delete_any_record(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("payroll:orderrecord_delete", args=[self.manager_record.pk])
        )

        self.assertRedirects(response, reverse("payroll:orderrecord_list"))
        self.assertFalse(OrderRecord.objects.filter(pk=self.manager_record.pk).exists())

# Create your tests here.
