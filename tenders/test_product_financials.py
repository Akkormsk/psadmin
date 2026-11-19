from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .product_financials import present_product_financials
from .services import calculate_tender


class ProductFinancialsTests(SimpleTestCase):
    def line(self, source, cost, **extra):
        return {'id': source, 'name': 'Товар', 'quantity': '10', 'nmck_unit': '100',
            'material_unit': str(cost), 'application_unit': '0', 'logistics_unit': '0',
            'group_key': '', 'requirements': {'source_item_id': source}, **extra}

    def workspace(self, lines, state='current'):
        return {'lines': lines, 'input_status': {'state': state}}

    def estimate(self):
        return SimpleNamespace(reduction_percent=Decimal('20'), russia_delivery=Decimal('90'), vat_rate_snapshot=Decimal('10'))

    def test_profit_is_before_tax_and_general_delivery_using_one_shared_calculation(self):
        item = {'source_id': 42, 'quantity': 10, 'price': 100, 'sum': 1000}
        with patch('tenders.product_financials.calculate_tender', wraps=calculate_tender) as executor:
            present_product_financials([item], [], self.workspace([self.line(42, 30)]), self.estimate())
        self.assertEqual(executor.call_count, 1)
        self.assertEqual(item['finance']['purchase_total'], Decimal('300'))
        self.assertEqual(item['finance']['rrp_total'], Decimal('800'))
        self.assertEqual(item['finance']['profit'], Decimal('500'))
        self.assertEqual(item['finance']['profit_unit'], Decimal('50'))

    def test_group_quote_is_not_distributed_to_its_components(self):
        children = [{'source_id': source, 'quantity': 10} for source in (42, 43)]
        quote = {'source_item_id': 11, 'member_source_ids': [42, 43], 'quantity': '10',
            'unit': 'набор', 'price': '100', 'sum': '1000', 'component_quantity': '10'}
        lines = [self.line(source, cost, nmck_unit='0', group_key='source-11',
            requirements={'source_item_id': source, 'source_group': quote}) for source, cost in ((42, 10), (43, 20))]
        group = {'key': 11, 'items': children, 'original': {'quantity': 10, 'price': 100, 'sum': 1000}}
        totals = present_product_financials(children, [group], self.workspace(lines), self.estimate())
        self.assertEqual(totals['purchase_total'], Decimal('300'))
        self.assertEqual(totals['profit'], Decimal('500'))
        self.assertEqual(group['finance']['profit'], Decimal('500'))
        self.assertEqual(group['finance']['purchase_total'], Decimal('300'))
        self.assertIsNone(children[0]['finance']['nmck_total'])
        self.assertIsNone(children[0]['finance']['profit'])

    def test_unpriced_operation_does_not_become_zero_or_profit(self):
        item = {'source_id': 42, 'quantity': 10}
        line = self.line(42, 20)
        line['requirements']['production'] = {'stage': 'training_dialogue',
            'route': {'processes': [{'id': 'goods'}, {'id': 'print'}]},
            'costs': [{'step_id': 'goods', 'amount_total': '200'}]}
        present_product_financials([item], [], self.workspace([line]), self.estimate())
        self.assertEqual(item['finance']['state'], 'partial')
        self.assertIsNone(item['finance']['profit'])
        self.assertEqual(item['finance']['purchase_total'], Decimal('200'))

    def test_stale_unknown_and_ambiguous_bindings_do_not_show_current_profit(self):
        for state in ('stale', 'unknown'):
            item = {'source_id': 42, 'quantity': 10, 'sum': 1000}
            present_product_financials([item], [], self.workspace([self.line(42, 20)], state), self.estimate())
            self.assertIsNone(item['finance']['profit'])
            self.assertIsNone(item['finance']['rrp_total'])
        item = {'source_id': 42, 'quantity': 10}
        present_product_financials([item], [], self.workspace([self.line(42, 20), self.line(42, 30)]), self.estimate())
        self.assertIsNone(item['finance']['purchase_total'])

    def test_no_estimate_keeps_only_real_notification_prices_and_negative_profit_is_visible(self):
        item = {'source_id': 42, 'quantity': 10, 'sum': 1000, 'price': 100}
        present_product_financials([item], [], None, None)
        self.assertEqual(item['finance']['nmck_total'], 1000)
        self.assertIsNone(item['finance']['rrp_total'])
        present_product_financials([item], [], self.workspace([self.line(42, 120)]), self.estimate())
        self.assertEqual(item['finance']['profit'], Decimal('-400'))

    def test_totals_do_not_double_count_the_group_or_omit_new_goods_as_complete(self):
        items = [{'source_id': 42, 'quantity': 10}, {'source_id': 43, 'quantity': 10}]
        totals = present_product_financials(items, [], self.workspace([self.line(42, 30)]), self.estimate())
        self.assertEqual(totals['purchase_total'], Decimal('300'))
        self.assertEqual(totals['state'], 'partial')
        self.assertIsNone(totals['profit'])
        self.assertIsNone(totals['nmck_total'])

    def test_unknown_notification_price_is_not_a_zero_revenue_loss(self):
        item = {'source_id': 42, 'quantity': 10}
        present_product_financials([item], [], self.workspace([self.line(42, 30, nmck_unit='0')]), self.estimate())
        self.assertIsNone(item['finance']['nmck_total'])
        self.assertIsNone(item['finance']['profit'])
        self.assertEqual(item['finance']['purchase_total'], Decimal('300'))

    def test_exact_commercial_subject_can_be_shown_without_a_single_source(self):
        item = {'commercial_id': 55, 'source_id': None, 'quantity': 10}
        line = self.line(42, 30, commercial_item_id=55, requirements={})
        present_product_financials([item], [], self.workspace([line]), self.estimate())
        self.assertEqual(item['finance']['purchase_total'], Decimal('300'))
        self.assertEqual(item['finance_key'], 'commercial-55')

    def test_shared_cells_and_footer_show_total_first_and_unit_price_second(self):
        from django.template.loader import render_to_string
        item = {'source_id': 42, 'quantity': 10}
        present_product_financials([item], [], self.workspace([self.line(42, 30)]), self.estimate())
        context = {'finance': item['finance'], 'finance_key': item['finance_key'], 'finance_view': 'row'}
        html = render_to_string('tender_selection/_product_financials.html', context)
        self.assertLess(html.index('300,00'), html.index('30,00'))
        self.assertIn('500,00', html)
        self.assertIn('50,00', html)
        context['finance_view'] = 'footer'
        html = render_to_string('tender_selection/_product_financials.html', context)
        self.assertIn('Себестоимость товара', html)
        self.assertLess(html.index('300,00'), html.index('30,00'))

    def test_legacy_notification_blanks_render_as_unknown_without_500(self):
        for value in ('', '   ', None):
            item = {'source_id': 42, 'quantity': value, 'sum': value, 'price': value}
            totals = present_product_financials([item], [], None, None, value)
            self.assertIsNone(item['finance']['nmck_total'])
            self.assertIsNone(item['finance']['nmck_unit'])
            if totals is not None:
                self.assertIsNone(totals['nmck_total'])
        item = {'source_id': 42, 'quantity': '10', 'sum': '', 'price': ''}
        present_product_financials([item], [], None, None)
        self.assertIsNone(item['finance']['nmck_total'])

    def test_source_numbers_keep_zero_and_accept_existing_document_number_formats(self):
        item = {'source_id': 42, 'quantity': '10', 'sum': '1 000,50', 'price': '100,05'}
        present_product_financials([item], [], None, None)
        self.assertEqual(item['finance']['nmck_total'], Decimal('1000.50'))
        self.assertEqual(item['finance']['nmck_unit'], Decimal('100.05'))
        item.update(sum='0', price='0', quantity='0')
        present_product_financials([item], [], None, None)
        self.assertEqual(item['finance']['nmck_total'], Decimal('0'))
