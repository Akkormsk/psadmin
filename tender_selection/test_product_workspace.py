from types import SimpleNamespace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import skipUnless

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class ProductWorkspaceTests(SimpleTestCase):
    @skipUnless(os.environ.get('PSADMIN_DOM_NODE_PATH'), 'Optional lightweight DOM dependencies not supplied')
    def test_saved_route_client_flow_uses_real_rendered_script_without_network(self):
        result = {'stage': 'training_dialogue', 'session_id': 1, 'route': {'processes': [
            {'id': 'stock', 'name': 'Закупка', 'kind': 'catalog', 'details': []},
            {'id': 'print', 'name': 'Печать', 'kind': 'production', 'process_id': 1, 'details': []},
            {'id': 'pack', 'name': 'Упаковка', 'kind': 'other', 'details': []}]},
            'catalog_steps': {'stock': {'catalog_selection': {'price': '0', 'cost_total': '0',
                'supplier_name': 'Поставщик <тест>'}}},
            'production_steps': {'print': {'counterparty_name': 'Печатник <тест>', 'unit_cost': '12.50',
                'total_cost': '1250', 'price_source_type': 'internal_calculator'}}}
        fixture = {'result': result,
            'script': render_to_string('tender_selection/_product_calculation_script.html'),
            'route': render_to_string('tender_selection/_product_route.html', {
                'tender': SimpleNamespace(pk=7), 'route': {'state': 'ready', 'label': 'Маршрут',
                    'assumption': 'Уточняется материал', 'interaction': {'options': ['Вариант'], 'url': '/answer'},
                    'steps': [{'id': 1, 'position': 1, 'name': 'Печать'}]}})}
        from django.conf import settings
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'route.json'
            path.write_text(json.dumps(fixture, ensure_ascii=False))
            checked = subprocess.run(['node', str(settings.BASE_DIR / 'scripts/check_product_route_ui.cjs'), str(path)],
                env={**os.environ, 'NODE_PATH': os.environ['PSADMIN_DOM_NODE_PATH']}, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

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
        html = self.render_workspace()
        self.assertIn('<span class="ts-product-quantity">100 шт.</span>', html)
        self.assertIn('data-regime-indicator', html)
        self.assertIn('Количество', html)
        self.assertNotIn('ts-product-list-regime', html)
        self.assertEqual(html.count('class="ts-product-gisp"'), 2)
        self.assertNotIn('type="search"', html)
        self.assertNotIn('Наименование или реестровый номер', html)
        self.assertIn('disabled>Подобрать продукцию</button>', html)
        self.assertIn('По описанию и ТЗ товара', html)
        self.assertIn('Ещё не подобран', html)
        self.assertIn('Подключим позже', html)

    def test_1875_tooltip_keeps_the_actual_regime_and_none_is_not_green(self):
        from .national_regime import annotation
        for status in ('ban', 'restriction', 'advantage', 'none', 'unknown'):
            regime = annotation(status)
            html = render_to_string('tender_selection/_regime_indicator.html', {'regime': regime})
            self.assertIn('data-regime="' + status + '"', html)
            self.assertIn(regime['label'], html)
            self.assertIn(regime['description'], html)
            self.assertIn('ts-national-pill ts-national-pill--', html)
            self.assertIn('aria-hidden="true">1875</span>', html)
            self.assertNotIn('ts-regime-dot', html)
        html = render_to_string('tender_selection/_1875.html', {'regime': annotation('none')})
        self.assertIn('ts-national-pill--gray', html)
        self.assertNotIn('ts-national-pill--green', html)

    def test_saved_route_uses_the_existing_chain_and_expandable_step_blocks(self):
        route = {'state': 'ready', 'label': 'Маршрут · 2 этапа', 'steps': [
            {'id': 1, 'position': 1, 'name': 'Закупка', 'description': 'Подбор продукции'},
            {'id': 2, 'position': 2, 'name': 'Печать', 'description': 'Нанесение'}]}
        html = render_to_string('tender_selection/_product_route.html', {'route': route})
        self.assertIn('<ol class="training-route__steps">', html)
        self.assertEqual(html.count('class="training-route__step"'), 2)
        self.assertEqual(html.count('Предварительно'), 2)
        self.assertEqual(html.count('disabled>Настроить этап'), 2)
        self.assertNotIn('0,00', html)

    def test_failed_document_binding_does_not_invent_route_operations(self):
        message = 'Сведения документов не сопоставлены с товарами.'
        html = render_to_string('tender_selection/_product_route.html', {
            'route': {'state': 'pending', 'steps': [], 'label': 'Маршрут не построен'},
            'route_blocked_reason': message})
        self.assertIn(message, html)
        self.assertNotIn('class="training-route__step"', html)

    def test_route_states_keep_saved_operations_and_unknown_executor_explicit(self):
        for state, message in (
            ('stale', 'Сохранённый маршрут устарел'),
            ('building', 'Маршрут строится'),
            ('ready', 'Предварительный маршрут'),
        ):
            with self.subTest(state=state):
                html = render_to_string('tender_selection/_product_route.html', {'route': {
                    'state': state, 'assumption': 'Материал требует уточнения', 'steps': [
                        {'id': 1, 'position': 1, 'name': 'Печать'}]}})
                self.assertIn(message, html)
                self.assertIn('data-product-route-context', html)
                self.assertIn('data-route-executor>Не выбран', html)
                self.assertIn('data-route-price-source>Не задан', html)
                self.assertIn('class="training-route__step"', html)
                self.assertIn('— / ед.', html)
                self.assertNotIn('0,00', html)

    def test_route_footer_distinguishes_no_cost_zero_partial_and_saved_cost(self):
        for state, total, unit, complete, label in (
            ('unavailable', None, None, False, 'Себестоимость товара'),
            ('current', 0, 0, True, 'Себестоимость товара'),
            ('partial', 120, 12, False, 'Рассчитанная часть стоимости'),
            ('stale', 120, 12, True, 'Прежняя себестоимость'),
        ):
            with self.subTest(state=state):
                html = render_to_string('tender_selection/_product_financials.html', {
                    'finance_view': 'footer', 'finance': {'state': state, 'purchase_total': total,
                        'purchase_unit': unit, 'complete': complete}})
                self.assertIn(label, html)
                if total is None:
                    self.assertNotIn('0,00', html)
                elif total == 0:
                    self.assertEqual(html.count('0,00'), 2)
