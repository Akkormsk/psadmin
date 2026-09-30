from django.test import SimpleTestCase

from .integrations.fsprint_rizograf import CHOICES, PAPER_PRICES, RizografError, calculate_price


class RizografPriceTests(SimpleTestCase):
    def test_exact_tier_threshold_uses_that_tier(self):
        result = calculate_price(1000, "standard_ru_80")
        self.assertEqual(result["price_per_copy"], 0.6)  # 0.25 печать + 0.35 бумага
        self.assertEqual(result["total"], 600.0)

    def test_quantity_between_tiers_uses_the_lower_threshold(self):
        result = calculate_price(150, "standard_ru_80")
        self.assertEqual(result["price_per_copy"], 0.89)  # тираж 150 -> тариф от 100 (0.54) + бумага 0.35

    def test_quantity_below_smallest_tier_uses_the_smallest_tier_as_a_floor(self):
        result = calculate_price(5, "standard_ru_80")
        self.assertEqual(result["price_per_copy"], 3.5)  # тариф от 10 (3.15) + бумага 0.35

    def test_quantity_above_largest_tier_uses_the_largest_tier(self):
        result = calculate_price(50000, "standard_ru_80")
        self.assertEqual(result["price_per_copy"], 0.54)  # 0.19 печать + 0.35 бумага

    def test_a3_doubles_the_whole_per_copy_price(self):
        a4 = calculate_price(1000, "standard_ru_80", format="A4")
        a3 = calculate_price(1000, "standard_ru_80", format="A3")
        self.assertEqual(a3["price_per_copy"], round(a4["price_per_copy"] * 2, 4))

    def test_unknown_paper_is_reported_not_guessed(self):
        with self.assertRaises(RizografError):
            calculate_price(1000, "неизвестная бумага")

    def test_unknown_format_is_reported_not_guessed(self):
        with self.assertRaises(RizografError):
            calculate_price(1000, "standard_ru_80", format="A5")

    def test_every_paper_choice_is_a_real_accepted_key(self):
        choice_keys = {choice["value"] for choice in CHOICES["paper_key"]}
        self.assertEqual(choice_keys, set(PAPER_PRICES))
        for key in choice_keys:
            calculate_price(1000, key)  # не должно бросать RizografError

    def test_every_format_choice_is_accepted(self):
        for choice in CHOICES["format"]:
            calculate_price(1000, "standard_ru_80", format=choice["value"])
