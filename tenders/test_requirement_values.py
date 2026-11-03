from decimal import Decimal
from django.test import SimpleTestCase
from .requirement_values import equivalent, normalize, satisfies, normalized_fact


class RequirementValueTests(SimpleTestCase):
    def test_equivalence_is_distinct_from_inclusion_and_open_bounds(self):
        self.assertTrue(equivalent('не менее 380 г/м²', '>=380 gsm'))
        self.assertTrue(equivalent('0,38 кг/м²', '380 г/м2'))
        self.assertFalse(equivalent('>380 г/м²', '≥380 г/м²'))
        self.assertFalse(equivalent('380 г/м²', '≥380 г/м²'))
        requirement = normalize('≥380 г/м²')
        for number, expected in [('379', False), ('380', True), ('400', True)]:
            self.assertIs(satisfies(normalize(number + ' г/м²'), requirement), expected)
        self.assertFalse(satisfies(normalize('380 г/м²'), normalize('>380 г/м²')))

    def test_full_ranges_missing_units_and_dimensions_do_not_prove_a_match(self):
        requirement = normalize('не менее 380 г/м²')
        self.assertIsNone(satisfies(normalize('350-450 г/м²'), requirement))
        self.assertTrue(satisfies(normalize('380-450 г/м²'), requirement))
        self.assertFalse(satisfies(normalize('300-350 г/м²'), requirement))
        self.assertIsNone(satisfies(normalize('400'), requirement))
        self.assertIsNone(satisfies(normalize('400 г/м³'), requirement))
        self.assertTrue(equivalent('более 0; не более 12.5 мм', '>0 и <=1,25 см'))

    def test_explicit_sets_sequences_and_source_information_are_preserved(self):
        allowed = normalize({'kind': 'set', 'values': ['белый', 'чёрный']})
        offered = normalize({'kind': 'set', 'values': ['чёрный']})
        self.assertTrue(satisfies(offered, allowed))
        self.assertFalse(satisfies(allowed, offered))
        self.assertTrue(equivalent({'kind': 'set', 'values': ['a', 'b']}, {'kind': 'set', 'values': ['b', 'a']}))
        self.assertFalse(equivalent(['a', 'b'], ['b', 'a']))
        original = {'name': 'Плотность, г/м2', 'value': 'не менее 380', 'source': 'notification', 'provenance': {'field_path': ['item', 'range']}}
        fact = normalized_fact(original)
        self.assertEqual(fact['value'], original['value'])
        self.assertEqual(fact['provenance'], original['provenance'])
        self.assertEqual(fact['normalized_value']['lower'], '380')
        self.assertEqual(fact['normalized_value']['unit'], 'area_density')

    def test_dimensions_tolerances_and_boolean_values(self):
        self.assertTrue(equivalent('80×80 мм', '8 x 8 см'))
        self.assertTrue(equivalent('380 ± 5 г/м²', 'от 375 до 385 г/м²'))
        self.assertTrue(equivalent('100 мм ± 5%', '95-105 мм'))
        self.assertTrue(equivalent('да', True))
        self.assertTrue(equivalent('нет', False))
        self.assertFalse(satisfies(normalize('380'), normalize('!=380')))
        self.assertTrue(satisfies(normalize('381'), normalize('!=380')))
        self.assertTrue(equivalent('1 200,50 мм', '120.05 см'))

    def test_missing_values_never_prove_equivalence(self):
        self.assertFalse(equivalent(None, None))
        self.assertFalse(equivalent("", ""))
        self.assertIsNone(satisfies(normalize(None), normalize(None)))

    def test_unparsed_prose_is_not_reduced_to_its_first_number(self):
        self.assertEqual(normalize('Плотность 380, но возможны варианты').kind, 'text')
        self.assertFalse(equivalent('Плотность 380, но возможны варианты', '380'))
