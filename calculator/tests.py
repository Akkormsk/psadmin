import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import CalculatorSettings, Estimate, PriceItem
from .services import calculate_sheet_estimate


class SheetCalculatorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="calculator-manager", password="password")
        self.item = PriceItem.objects.create(category="paper", name="Тестовая бумага", unit_name="лист", unit_price="10.00")

    def test_manager_can_open_and_save_estimate(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("calculator_home")), "Листовая печать")
        response = self.client.post(reverse("calculator_home"), {"name": "Визитки", "comment": "Матовая бумага", "product_quantity": 100, "work_hours": "1.5", "lines_json": json.dumps([{"category": "paper", "item_id": self.item.pk, "quantity": 25, "custom": False}])})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Estimate.objects.get().lines.count(), 1)
        self.assertEqual(Estimate.objects.get().comment, "Матовая бумага")
        self.assertContains(self.client.get(reverse("calculator_home")), "Матовая бумага")

    def test_manager_can_delete_own_estimate(self):
        estimate = Estimate.objects.create(owner=self.user, name="Удалить")
        self.client.force_login(self.user)

        response = self.client.post(reverse("calculator_estimate_delete", args=[estimate.pk]))

        self.assertRedirects(response, reverse("calculator_home"))
        self.assertFalse(Estimate.objects.filter(pk=estimate.pk).exists())

    def test_wide_calculator_uses_its_formula(self):
        settings = CalculatorSettings.objects.create(hourly_rate=Decimal("550"))
        lines = [
            {"category": PriceItem.CATEGORY_WIDE_PAPER, "quantity": Decimal("0.7"), "unit_price": Decimal("85.80")},
            {"category": PriceItem.CATEGORY_WIDE_PRINT, "quantity": Decimal("0.7"), "unit_price": Decimal("42.00")},
        ]

        result = calculate_sheet_estimate(lines, 5, Decimal("0.5"), settings, Estimate.TYPE_WIDE)

        material = Decimal("89.46")
        coefficient = Decimal("1") + Decimal("1") / Decimal("0.7")
        self.assertEqual(result["cost_price"], material + Decimal("275"))
        self.assertEqual(result["standard"], material * Decimal("2") * coefficient + Decimal("550"))

    def test_manager_can_save_wide_estimate(self):
        item = PriceItem.objects.create(category=PriceItem.CATEGORY_WIDE_PAPER, name="Рулон", unit_name="пм", unit_price="13.72")
        self.client.force_login(self.user)

        response = self.client.post(reverse("calculator_home"), {
            "calculator_type": Estimate.TYPE_WIDE,
            "name": "Чертежи",
            "product_quantity": 5,
            "work_hours": "0.5",
            "lines_json": json.dumps([{"category": PriceItem.CATEGORY_WIDE_PAPER, "item_id": item.pk, "quantity": 2, "custom": False}]),
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Estimate.objects.get().calculator_type, Estimate.TYPE_WIDE)

    def test_canon_catalog_uses_manual_other_option_and_explicit_order(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("calculator_home"), {"calculator": Estimate.TYPE_WIDE})

        self.assertContains(response, "Плоттер Canon")
        self.assertFalse(PriceItem.objects.filter(category=PriceItem.CATEGORY_WIDE_PAPER, name="Другое").exists())
        ordered = list(PriceItem.objects.filter(category=PriceItem.CATEGORY_WIDE_PAPER).values_list("sort_order", flat=True))
        self.assertEqual(ordered, sorted(ordered))

    def test_saved_estimates_are_filtered_by_current_calculator(self):
        sheet = Estimate.objects.create(owner=self.user, name="Листовой", calculator_type=Estimate.TYPE_SHEET)
        wide = Estimate.objects.create(owner=self.user, name="Canon", calculator_type=Estimate.TYPE_WIDE)
        self.client.force_login(self.user)

        response = self.client.get(reverse("calculator_home"), {"calculator": Estimate.TYPE_WIDE})

        self.assertContains(response, wide.name)
        self.assertNotContains(response, sheet.name)

    def test_switch_link_from_saved_estimate_points_to_calculator_home(self):
        estimate = Estimate.objects.create(owner=self.user, name="Сохранённый", calculator_type=Estimate.TYPE_SHEET)
        self.client.force_login(self.user)

        response = self.client.get(reverse("calculator_estimate", args=[estimate.pk]))

        self.assertContains(response, "window.location.href='/calculator/?calculator='+this.value")
        self.assertContains(response, "Начать новый расчёт? Несохранённые изменения будут потеряны.")

    def test_new_calculation_does_not_replace_existing_estimate(self):
        first = Estimate.objects.create(owner=self.user, name="Первый", calculator_type=Estimate.TYPE_SHEET)
        self.client.force_login(self.user)

        response = self.client.post(reverse("calculator_home"), {
            "calculator_type": Estimate.TYPE_SHEET,
            "name": "Второй",
            "comment": "Новая версия",
            "product_quantity": 1,
            "work_hours": "0",
            "lines_json": "[]",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Estimate.objects.filter(owner=self.user).count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.name, "Первый")

    def test_admin_can_reorder_price_items_inside_category(self):
        admin = get_user_model().objects.create_superuser(username="price-admin", password="password")
        first = PriceItem.objects.create(category="paper", name="Первая", unit_price="1", sort_order=10)
        second = PriceItem.objects.create(category="paper", name="Вторая", unit_price="2", sort_order=20)
        self.client.force_login(admin)

        list_response = self.client.get(reverse("admin:calculator_sheetpriceitem_changelist"))
        self.assertContains(list_response, '<td class="field-drag_handle">', html=False)

        response = self.client.post(
            reverse("admin:calculator_sheetpriceitem_reorder"),
            data=json.dumps({"ids": [second.pk, first.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertLess(second.sort_order, first.sort_order)
