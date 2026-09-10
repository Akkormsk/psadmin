from decimal import Decimal

from django.test import SimpleTestCase

from .cascade import Criterion


class CriterionChecksTests(SimpleTestCase):
    def criterion(self, label="Длина", value="6 см", operator="=", unit="см"):
        return Criterion(label=label, raw_value=value, concept=label, operator=operator, value=value, unit=unit)

    def check(self, criterion, attributes):
        from .criterion_checks import deterministic_cells

        return deterministic_cells({"attributes": attributes}, [criterion], Decimal("0.05"))

    def test_measurement_units_and_five_percent_boundary(self):
        for value, verdict in [("58 мм", "y"), ("57 мм", "y"), ("56.9 мм", "n"), ("63 мм", "y"), ("64 мм", "n")]:
            with self.subTest(value=value):
                cells = self.check(self.criterion(), [{"name": "Длина", "value": value}])
                self.assertEqual(cells[1][0], verdict)
                self.assertIn(value, cells[1][1])

    def test_generic_new_property_uses_same_comparator(self):
        criterion = self.criterion("Амплитуда перемещения", "2 см", ">=", "см")
        self.assertEqual(self.check(criterion, [{"name": "Амплитуда перемещения", "value": "19 мм"}])[1][0], "y")

    def test_incompatible_or_missing_units_are_left_to_ai(self):
        for value in ("6", "6 г", "примерно 6 см", "5-7 см"):
            self.assertEqual(self.check(self.criterion(), [{"name": "Длина", "value": value}]), {})

    def test_unlabelled_dimensions_are_not_guessed(self):
        self.assertEqual(self.check(self.criterion(), [{"name": "Размер товара (см)", "value": "1 х 2,5 х 6"}]), {})

    def test_packaging_dimensions_cannot_prove_product_dimensions(self):
        self.assertEqual(self.check(self.criterion(), [{"name": "Длина (упаковки)", "value": "6 см"}]), {})

    def test_conflicting_values_are_not_marked_as_matches(self):
        self.assertEqual(self.check(self.criterion(), [{"name": "Длина", "value": "6 см"}, {"name": "Длина", "value": "8 см"}]), {})

    def test_exact_interface_is_a_literal_not_a_number_with_tolerance(self):
        criterion = self.criterion("Интерфейс", "USB 2.0", "=", "")
        self.assertEqual(self.check(criterion, [{"name": "Интерфейс", "value": "USB 2.0"}])[1][0], "y")
        self.assertEqual(self.check(criterion, [{"name": "Интерфейс", "value": "USB 2.1"}]), {})
        self.assertEqual(self.check(criterion, [{"name": "Интерфейс", "value": "не поддерживает USB 2.0"}]), {})

    def test_unrelated_description_cannot_prove_a_property(self):
        from .criterion_checks import deterministic_cells

        self.assertEqual(deterministic_cells({"description": "длина 6 см"}, [self.criterion()]), {})

    def test_options_require_an_explicit_any_or_all_operator(self):
        criterion = self.criterion("Материал", "металл; пластик", "~", "")
        criterion.options = ["металл", "пластик"]
        attrs = [{"name": "Материал", "value": "пластик"}]
        self.assertEqual(self.check(criterion, attrs), {})
        criterion.operator = "in"
        self.assertEqual(self.check(criterion, attrs)[1][0], "y")
        criterion.operator = "all"
        self.assertEqual(self.check(criterion, attrs), {})

    def test_unchecked_rows_do_not_shift_selected_row_numbers(self):
        from .criterion_checks import deterministic_cells

        unchecked = self.criterion()
        unchecked.checked = False
        cells = deterministic_cells({"attributes": [{"name": "Длина", "value": "6 см"}]}, [unchecked, self.criterion()])
        self.assertEqual(set(cells), {1})
