from copy import deepcopy
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from tender_selection.models import Tender
from .models import ComponentRoutePlan, TenderCommercialItem
from .calculation_v2_pipeline import ingest_source_items, build_commercial_items
from .test_calculation_v2_pipeline import notification, raw_item
from .product_editing import product_editor, save_products


class ProductEditingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('editor', password='test')
        self.tender = Tender.objects.create(purchase_number='edit-test', title='Поставка', notification_raw=notification([raw_item('Сумка', '100')], []))
        ingest_source_items(self.tender)
        self.good = build_commercial_items(tender=self.tender)[0]
        self.source = self.good.source_items.get()

    def save(self, state):
        return save_products(self.user, self.tender.pk, state)

    def test_edit_manual_price_preserves_sources_and_history(self):
        original = deepcopy(self.tender.notification_raw)
        state = product_editor(self.tender)
        row = state['items'][0]
        row.update(name='Сумка с печатью', quantity='200', mode='manual', cost_entry='parts', material='50', application='10', logistics='0')
        self.save(state)
        self.good.refresh_from_db()
        self.source.refresh_from_db()
        self.tender.refresh_from_db()
        self.assertEqual(self.good.quantity, 200)
        self.assertEqual(self.source.quantity, 100)
        self.assertEqual(self.tender.notification_raw, original)
        estimate = self.tender.estimates.latest('pk')
        self.assertEqual(estimate.lines.get().material_unit, 50)
        self.assertEqual(estimate.lines.get().commercial_item_id, self.good.pk)
        state = product_editor(self.tender)
        state['items'][0]['material'] = '70'
        self.save(state)
        self.assertEqual(estimate.lines.get().material_unit, 50)
        self.assertEqual(self.tender.estimates.latest('pk').supersedes_id, estimate.pk)

    def test_read_cancel_and_conflict_do_not_write(self):
        state = product_editor(self.tender)
        state['items'][0]['name'] = 'Черновик'
        self.assertEqual(product_editor(self.tender)['items'][0]['name'], 'Сумка')
        self.save(state)
        with self.assertRaisesMessage(ValueError, 'изменились'):
            self.save(state)

    def test_add_exclude_and_keep_route_history(self):
        component = self.good.components.get()
        plan = ComponentRoutePlan.objects.create(commercial_item=self.good, component=component)
        state = product_editor(self.tender)
        state['items'] = [dict(key='new-item', name='Коробка', quantity='2', unit='шт', mode='manual', cost_entry='parts', material='0', application='0', logistics='0', nmck='100', components=[])]
        self.save(state)
        self.good.refresh_from_db()
        self.assertEqual(self.good.status, 'excluded')
        self.assertTrue(ComponentRoutePlan.objects.filter(pk=plan.pk).exists())
        self.assertEqual(product_editor(self.tender)['items'][0]['name'], 'Коробка')
        build_commercial_items(tender=self.tender)
        self.good.refresh_from_db()
        self.assertEqual(self.good.status, 'excluded')

    def test_component_edit_and_manual_mode_roundtrip(self):
        state = product_editor(self.tender)
        state['items'][0]['components'] = [dict(key='new-component', name='Лента', quantity='2', unit='шт', mode='manual', cost_entry='parts', material='3', application='0', logistics='0', nmck='')]
        self.save(state)
        self.good.refresh_from_db()
        self.assertEqual(self.good.structure, TenderCommercialItem.Structure.COMPOSITE)
        component = self.good.components.get(status='active')
        self.assertEqual(component.effective_quantity, Decimal('200'))
        estimate = self.tender.estimates.latest('pk')
        self.assertEqual(estimate.lines.get().component_id, component.pk)
        self.client.force_login(self.user)
        from django.urls import reverse
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]),
                                   {'analysis_snapshot': '1', 'full_card': '1'}, secure=True)
        self.assertEqual(response.context['display_items'][0]['finance']['purchase_total'], Decimal('600'))
        state = product_editor(self.tender)
        row = state['items'][0]
        row.update(mode='manual', cost_entry='parts', material='12', application='0', logistics='0')
        self.save(state)
        state = product_editor(self.tender)
        state['items'][0]['mode'] = 'route'
        self.save(state)
        self.assertEqual(product_editor(self.tender)['items'][0]['material'], '12')

    def test_invalid_and_foreign_rows_rollback(self):
        state = product_editor(self.tender)
        state['items'][0]['quantity'] = 'NaN'
        with self.assertRaises(ValueError):
            self.save(state)
        self.assertFalse(self.tender.estimates.exists())
        state = product_editor(self.tender)
        state['items'][0]['key'] = 'good-99999'
        with self.assertRaises(ValueError):
            self.save(state)

    def test_real_card_and_save_endpoint_share_working_quantity_and_finances(self):
        from django.urls import reverse
        from unittest.mock import patch
        self.client.force_login(self.user)
        state = product_editor(self.tender)
        state['items'][0].update(quantity='200')
        state['confirmations'] = {'quantity': [state['items'][0]['key']], 'nmck': [], 'exclusions': []}
        response = self.client.post(reverse('tender_selection:save_products', args=[self.tender.pk]), state, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        with patch('tender_selection.views.notification_for', side_effect=AssertionError('Unexpected source request')):
            response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]), {'analysis_snapshot': '1', 'full_card': '1'}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-product-edit')
        self.assertEqual(response.context['display_items'][0]['quantity'], 200)
        self.assertIsNone(response.context['display_items'][0]['finance']['purchase_total'])
        self.assertIsNone(response.context['product_finance_totals']['purchase_total'])
        self.assertEqual(response.context['product_calculation']['input_status']['state'], 'current')

    def test_source_quantity_and_exclusion_need_explicit_confirmation(self):
        state = product_editor(self.tender)
        self.assertTrue(state['items'][0]['source_locked'])
        state['items'][0]['quantity'] = '200'
        with self.assertRaisesMessage(ValueError, 'Подтвердите изменение количества'):
            save_products(self.user, self.tender.pk, state, route_only=True)
        state = product_editor(self.tender)
        state['items'][0]['nmck'] = '999'
        with self.assertRaisesMessage(ValueError, 'Подтвердите изменение НМЦК'):
            save_products(self.user, self.tender.pk, state, route_only=True)
        state = product_editor(self.tender)
        key = state['items'][0]['key']
        state['items'] = []
        with self.assertRaisesMessage(ValueError, 'Подтвердите исключение'):
            save_products(self.user, self.tender.pk, state, route_only=True)
        state['confirmations'] = {'quantity': [], 'nmck': [], 'exclusions': [key]}
        save_products(self.user, self.tender.pk, state, route_only=True)
        self.good.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.good.status, 'excluded')
        self.assertTrue(self.source.is_active)

    def test_route_reads_working_quantity_and_preserves_evidence(self):
        from .calculation_v2_pipeline import routing_source_items
        state = product_editor(self.tender)
        state['items'][0]['quantity'] = '200'
        self.save(state)
        self.assertEqual(routing_source_items(self.tender)[0].quantity, 200)
        self.source.refresh_from_db()
        self.assertEqual(self.source.quantity, 100)

    def test_missing_price_remains_unknown_and_explicit_zero_is_known(self):
        state = product_editor(self.tender)
        self.save(state)
        self.assertFalse(self.tender.estimates.latest('pk').lines.get().requirements['card_edit']['known'])
        state = product_editor(self.tender)
        state['items'][0].update(mode='manual', cost_entry='parts', material='0', nmck='0')
        self.save(state)
        self.assertTrue(self.tender.estimates.latest('pk').lines.get().requirements['card_edit']['known'])

    def test_saved_route_price_survives_manual_switch(self):
        from .models import TenderEstimate, TenderLine
        original = TenderEstimate.objects.create(tender=self.tender, owner=self.user, tender_number='x', name='Original')
        TenderLine.objects.create(estimate=original, commercial_item=self.good, name='Сумка', quantity=100, nmck_unit=100, material_unit=30,
                                  requirements={'source_item_id': self.source.pk, 'production': {'stage': 'training_dialogue', 'route': {'processes': [{'id': 'goods'}]}, 'costs': [{'step_id': 'goods', 'amount_total': '3000'}]}})
        state = product_editor(self.tender)
        state['items'][0].update(mode='manual', cost_entry='parts', material='50')
        self.save(state)
        state = product_editor(self.tender)
        state['items'][0]['mode'] = 'route'
        self.save(state)
        line = self.tender.estimates.latest('pk').lines.get()
        self.assertEqual(line.material_unit, 30)
        self.assertTrue(line.requirements['card_edit']['known'])
        self.assertEqual(original.lines.get().material_unit, 30)

    def test_total_entry_and_route_result_do_not_overwrite_manual_price(self):
        import json
        from .product_editing import save_route_prices
        state = product_editor(self.tender)
        state['items'][0].update(mode='manual', cost_entry='total', total='42', nmck='100')
        self.save(state)
        estimate = self.tender.estimates.latest('pk')
        line = estimate.lines.get()
        production = {'stage': 'training_dialogue', 'route': {'processes': [{'id': 'supply'}]},
                      'costs': [{'step_id': 'supply', 'amount_total': '3000'}],
                      'totals': {'material_unit': '25', 'application_unit': '5', 'logistics_unit': '0'}}
        posted = {'id': line.pk, 'name': line.name, 'quantity': str(line.quantity), 'nmck_unit': str(line.nmck_unit),
                  'group_key': '', 'group_name': '', 'material_unit': '999', 'requirements': {'production': production}}
        updated = save_route_prices(self.user, estimate, {'lines_json': json.dumps([posted])})
        self.assertEqual(updated.lines.get().material_unit, 25)
        state = product_editor(self.tender)
        state['items'][0]['mode'] = 'route'
        self.save(state)
        line = self.tender.estimates.latest('pk').lines.get()
        self.assertEqual(line.material_unit + line.application_unit + line.logistics_unit, 30)
        self.assertTrue(line.requirements['card_edit']['known'])

    def test_unmaterialized_notice_can_be_edited_and_all_items_excluded(self):
        tender = Tender.objects.create(purchase_number='notice-only', title='Извещение', notification_raw=notification([raw_item('Блокнот', '10')], []))
        state = product_editor(tender)
        self.assertTrue(state['items'][0]['key'].startswith('notice-'))
        state['items'][0]['name'] = 'Рабочий блокнот'
        save_products(self.user, tender.pk, state)
        self.assertEqual(product_editor(tender)['items'][0]['name'], 'Рабочий блокнот')
        tender2 = Tender.objects.create(purchase_number='notice-exclude', title='Извещение', notification_raw=notification([raw_item('Ручка', '10')], []))
        state = product_editor(tender2)
        state['items'] = []
        save_products(self.user, tender2.pk, state)
        self.assertEqual(product_editor(tender2)['items'], [])
        self.assertEqual(tender2.v2_source_items.get().original_text, 'Ручка')

    def test_component_removal_does_not_repurpose_historical_component(self):
        state = product_editor(self.tender)
        state['items'][0]['components'] = [dict(key='new-part', name='Лента', quantity='2', unit='шт', mode='manual', cost_entry='total', total='3')]
        self.save(state)
        component = self.good.components.get(status='active')
        state = product_editor(self.tender)
        state['items'][0]['components'] = []
        self.save(state)
        component.refresh_from_db()
        self.assertEqual(component.name, 'Лента')
        self.assertEqual(component.status, 'superseded')
        self.assertNotEqual(self.good.components.get(status='active').pk, component.pk)

    def test_edited_route_uses_working_inputs_and_retains_plan(self):
        from .models import ProcessDefinition, TenderComputeJob
        from .calculation_v2_pipeline import RouteDecision, route_tender_batch, source_route_snapshots
        state = product_editor(self.tender)
        state['items'][0].update(name='Сумка с печатью', quantity='200')
        self.save(state)
        process = ProcessDefinition.objects.create(name='Поставка', role=ProcessDefinition.ROLE_SUPPLY)
        class Router:
            def route(router, *, items, **kwargs):
                self.assertEqual(items[0].name, 'Сумка с печатью')
                self.assertEqual(items[0].effective_quantity, 200)
                return [RouteDecision(item.pk, (process.pk,), Decimal('0.9'), {}) for item in items]
        job = TenderComputeJob.objects.create(tender=self.tender)
        route_tender_batch(job, Router())
        component = self.good.components.get(status='active')
        self.assertTrue(component.route_plans.exists())
        self.assertIn(source_route_snapshots(self.tender)[self.source.pk]['state'], ('ready', 'needs_review'))
        self.source.refresh_from_db()
        self.assertEqual(self.source.quantity, 100)
