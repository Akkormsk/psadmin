from django.test import SimpleTestCase

from .sewing_price_list import canonical_data_from_rows


class SewingPriceListTests(SimpleTestCase):
    def test_keeps_real_values_but_marks_currency_and_formula_unconfirmed(self):
        rows = [
            ["", "", "", "", "Курс $", 80],
            ["", "", "", "", "+%", 82.4],
            ["Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"],
            ["Футболка", "Классическая мужская", "Кулирка", "", "Классическая мужская футболка", 414.8],
            ["", "Классическая женская", "Кулирка", 100, "Классическая женская футболка", 414.8],
        ]

        data = canonical_data_from_rows(rows)

        self.assertTrue(data["requires_confirmation"])
        self.assertEqual(data["formula_status"], "unresolved")
        self.assertIsNone(data["pricing"]["currency"])
        self.assertEqual(data["pricing"]["exchange_rate"], "80")
        variant = data["pricing"]["variants"]["Футболка | Классическая женская | Кулирка"]
        self.assertEqual(variant["minimum_quantity"], "100")
        self.assertEqual(variant["unit_price"], "414.8")
