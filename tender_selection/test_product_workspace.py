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

    def test_quantity_is_separate_and_gisp_is_an_inactive_placeholder(self):
        from html.parser import HTMLParser

        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.searches = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input' and attrs.get('type') == 'search':
                    self.searches.append(attrs)

        html = self.render_workspace()
        self.assertIn('<span class="ts-product-quantity">100 шт.</span>', html)
        self.assertIn('data-regime-indicator', html)
        self.assertIn('Количество', html)
        self.assertNotIn('ts-product-list-regime', html)
        controls = Controls()
        controls.feed(html)
        self.assertEqual(len(controls.searches), 2)  # Group and its product.
        for search in controls.searches:
            self.assertIn('disabled', search)
            self.assertNotIn('name', search)
            self.assertNotIn('value', search)
        self.assertIn('Наименование или реестровый номер', html)
        self.assertIn('Подключим позже', html)

    def test_1875_tooltip_keeps_the_actual_regime_and_none_is_not_green(self):
        from .national_regime import annotation
        for status in ('ban', 'restriction', 'advantage', 'none', 'unknown'):
            regime = annotation(status)
            html = render_to_string('tender_selection/_regime_indicator.html', {'regime': regime})
            self.assertIn('data-regime="' + status + '"', html)
            self.assertIn(regime['label'], html)
            self.assertIn(regime['description'], html)
        html = render_to_string('tender_selection/_1875.html', {'regime': annotation('none')})
        self.assertIn('ts-national-pill--gray', html)
        self.assertNotIn('ts-national-pill--green', html)
