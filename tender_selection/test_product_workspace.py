from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class ProductWorkspaceTests(SimpleTestCase):
    def render_workspace(self, source_id=42):
        item = {
            'source_id': source_id, 'name': 'Блокнот', 'quantity': 100, 'unit': 'шт.',
            'national_regime': {'status': 'unknown'},
            'tz_rows': [{'name': 'Формат', 'value': 'А5'}],
            'composition_question': 'Выберите состав', 'composition_question_id': 9,
            'composition_options': ['С обложкой', 'Без обложки'],
            'production_route': {'source_item_id': 42, 'label': 'Маршрут готов', 'state': 'ready'},
            'components': [{'name': 'Обложка', 'quantity_per_parent': 1, 'unit': 'шт.'}],
        }
        group = {
            'key': 17, 'name': 'Подарочный набор',
            'name_parts': [{'text': 'Подарочный набор'}], 'items': [item],
            'original': {'quantity': 100, 'unit': 'набор', 'price': 250, 'sum': 25000},
            'national_regime': {'status': 'unknown'}, 'supply_kind': 'kit',
        }
        return render_to_string('tender_selection/_analysis_result.html', {
            'tender': SimpleNamespace(pk=7), 'display_items': [item],
            'display_item_groups': [group], 'display_item_count': 1,
        })

    def test_group_and_component_have_separate_workspaces(self):
        html = self.render_workspace()
        self.assertIn('data-products-workspace', html)
        self.assertIn('data-product-key="source-42"', html)
        self.assertIn('data-product-key="group-17"', html)
        self.assertIn('Формат', html)
        self.assertIn('А5', html)
        self.assertIn('Обложка', html)
        self.assertIn('data-product-close', html)
        self.assertEqual(html.count('id="owner-question-9"'), 1)
        self.assertNotIn('data-source-route=', html)
        self.assertEqual(html.count('data-product-calculation-body'), 1)
        self.assertIn('data-owner-answer', html)
        self.assertNotIn('data-build-route', html)

    def test_empty_products_do_not_create_an_empty_inspector(self):
        html = render_to_string('tender_selection/_analysis_result.html', {
            'tender': SimpleNamespace(pk=7),
        })
        self.assertNotIn('data-products-workspace', html)

    def test_group_component_without_source_keeps_matching_selection_key(self):
        html = self.render_workspace(source_id=None)
        self.assertIn('data-product-key="group-17-0"', html)
        self.assertIn('data-product-target="group-17-0"', html)
