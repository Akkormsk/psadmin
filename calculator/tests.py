import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Estimate, PriceItem


class SheetCalculatorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="calculator-manager", password="password")
        self.item = PriceItem.objects.create(category="paper", name="Тестовая бумага", unit_name="лист", unit_price="10.00")

    def test_manager_can_open_and_save_estimate(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("calculator_home")), "Листовая печать")
        response = self.client.post(reverse("calculator_home"), {"name": "Визитки", "product_quantity": 100, "work_hours": "1.5", "lines_json": json.dumps([{"category": "paper", "item_id": self.item.pk, "quantity": 25, "custom": False}])})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Estimate.objects.get().lines.count(), 1)
