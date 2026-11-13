from django.db import connection
from django.test import TestCase
from tender_selection.models import Tender
from .models import (TenderSourceItem, TenderCommercialItem, CalculationComponent,
                     ComponentRoutePlan, ComponentOperationStep, ProcessDefinition,
                     TenderComputeJob, TenderComputeLine)
from .working_items import tender_working_items


class WorkingItemsTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number='working-tree')
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key='1', original_text='Кружка', quantity=2, unit='шт', requirements={'characteristics': [{'name':'Цвет','value':'Белый'}]})

    def working(self):
        item = TenderCommercialItem.objects.create(tender=self.tender, source_key='1', display_name='Кружка', quantity=2, unit='шт', requirements=self.source.requirements)
        item.source_items.add(self.source)
        component = CalculationComponent.objects.create(commercial_item=item, source_item=self.source, name='Кружка', unit='шт', requirements=self.source.requirements)
        return item, component

    def read(self):
        def select_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(select_only):
            return tender_working_items(self.tender)

    def test_simple_item_preserves_requirements_and_source(self):
        item, component = self.working()
        tree = self.read()['items'][0]
        self.assertEqual(tree['id'], item.pk)
        self.assertEqual(tree['sources'][0]['id'], self.source.pk)
        self.assertEqual(tree['requirements'], item.requirements)
        self.assertEqual(tree['components'][0]['quantity'], '2.000000')

    def test_composite_keeps_nested_components(self):
        item, parent = self.working()
        child = CalculationComponent.objects.create(commercial_item=item, parent=parent, name='Лента', quantity_per_parent=3)
        tree = self.read()['items'][0]
        self.assertEqual(tree['components'][0]['components'][0]['id'], child.pk)

    def test_shared_stage_is_attached_once_at_item_level(self):
        item, component = self.working()
        plan = ComponentRoutePlan.objects.create(commercial_item=item, scope='shared')
        process = ProcessDefinition.objects.create(name='Упаковка')
        step = ComponentOperationStep.objects.create(route_plan=plan, process=process, position=1)
        tree = self.read()['items'][0]
        self.assertEqual(tree['shared_routes'][0]['steps'][0]['id'], step.pk)
        self.assertEqual(tree['components'][0]['routes'], [])

    def test_missing_working_items_returns_source_without_writes(self):
        before = TenderSourceItem.objects.count()
        tree = self.read()['items'][0]
        self.assertEqual(tree['kind'], 'source')
        self.assertTrue(all(not refs for refs in tree['calculations'].values()))
        self.assertEqual(tree['components'], [])
        self.assertEqual(TenderSourceItem.objects.count(), before)
        self.assertFalse(TenderCommercialItem.objects.exists())

    def test_missing_results_does_not_invent_cost(self):
        self.working()
        tree = self.read()['items'][0]
        self.assertEqual(tree['state'], 'not_calculated')
        self.assertTrue(all(not refs for refs in tree['components'][0]['calculations'].values()))

    def test_stale_result_remains_visible_but_is_not_usable(self):
        item, component = self.working()
        job = TenderComputeJob.objects.create(tender=self.tender)
        line = TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=component, status='ready', input_snapshot={'quantity':'1','requirements':component.requirements}, result={'cost':'10'})
        projection = self.read()
        tree = projection['items'][0]
        result = projection['calculation_results'][tree['components'][0]['calculations']['stale'][0]]
        self.assertEqual(result['id'], line.pk)
        self.assertEqual(result['freshness'], 'stale')
        self.assertEqual(tree['state'], 'stale')
        self.assertFalse(result['usable'])
        line.refresh_from_db()
        self.assertEqual(line.result, {'cost':'10'})

    def test_shared_work_result_uses_explicit_operation_link(self):
        from .models import TenderComputeWorkUnit
        item, component = self.working()
        plan = ComponentRoutePlan.objects.create(commercial_item=item, scope='shared')
        process = ProcessDefinition.objects.create(name='Общий этап')
        step = ComponentOperationStep.objects.create(route_plan=plan, process=process, position=1)
        job = TenderComputeJob.objects.create(tender=self.tender)
        unit = TenderComputeWorkUnit.objects.create(job=job, operation_step=step, engine_key='manual', dedupe_key='shared', input_fingerprint='saved', status='ready', result={'cost':'25'})
        projection = self.read()
        tree = projection['items'][0]
        result = projection['calculation_results'][tree['shared_routes'][0]['steps'][0]['calculations']['unverified'][0]]
        self.assertEqual(result['id'], unit.pk)
        self.assertEqual(result['operation_step_ids'], [step.pk])
        self.assertEqual(result['freshness'], 'unknown')

    def test_explicit_confirmed_provider_selection_is_returned(self):
        from django.contrib.auth import get_user_model
        from .models import Counterparty, CounterpartyKnowledgeVersion, StageCounterpartyLink, ProviderCalculatorBinding
        item, component = self.working()
        user = get_user_model().objects.create(username='working-tree')
        process = ProcessDefinition.objects.create(name='Изготовление')
        provider = Counterparty.objects.create(name='Исполнитель', created_by=user)
        link = StageCounterpartyLink.objects.create(stage=process, counterparty=provider)
        version = CounterpartyKnowledgeVersion.objects.create(counterparty=provider, status='confirmed', created_by=user)
        binding = ProviderCalculatorBinding.objects.create(link=link, knowledge_version=version, calculator_type='manual_quote', name='Прайс')
        job = TenderComputeJob.objects.create(tender=self.tender)
        TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=component, status='ready', result={'provider_binding_id':binding.pk, 'provider_quote':{'status':'ready','total':'20'}})
        projection = self.read()
        result = projection['calculation_results'][projection['items'][0]['components'][0]['calculations']['unverified'][0]]
        self.assertEqual(result['selection']['counterparty_id'], provider.pk)
        self.assertEqual(result['selection']['calculator_id'], binding.pk)
        self.assertEqual(result['freshness'], 'unknown')
        self.assertFalse(result['usable'])

    def test_set_notice_price_is_not_distributed_to_components(self):
        from .test_calculation_v2_pipeline import notification, raw_item
        row = raw_item('Набор', '2')
        row.update(price='100.00', sum='200.00')
        self.tender.notification_raw = notification([row])
        self.tender.save(update_fields=['notification_raw'])
        self.source.metadata = {'notification_index': 0}
        self.source.save(update_fields=['metadata'])
        item, component = self.working()
        CalculationComponent.objects.create(commercial_item=item, name='Коробка')
        tree = self.read()['items'][0]
        self.assertEqual(tree['notice_prices'][0]['total'], '200.00')
        self.assertEqual(tree['notice_prices'][0]['unit_price'], '100.00')
        self.assertTrue(all('notice_prices' not in c for c in tree['components']))

    def test_new_pending_line_does_not_expose_old_price_as_current(self):
        item, component = self.working()
        old_job = TenderComputeJob.objects.create(tender=self.tender)
        old = TenderComputeLine.objects.create(job=old_job, source_item=self.source, commercial_item=item, component=component, status='ready', result={'cost':'10'})
        new_job = TenderComputeJob.objects.create(tender=self.tender)
        new = TenderComputeLine.objects.create(job=new_job, source_item=self.source, commercial_item=item, component=component, status='queued')
        projection = self.read()
        buckets = projection['items'][0]['components'][0]['calculations']
        self.assertEqual(buckets['historical'], [f'line:{old.pk}'])
        self.assertEqual(buckets['pending'], [f'line:{new.pk}'])
        self.assertEqual(buckets['current'], [])
        self.assertEqual(projection['calculation_results'][f'line:{old.pk}']['result']['cost'], '10')
        self.assertNotEqual(projection['items'][0]['state'], 'ready')

    def test_shared_cost_referenced_by_two_steps_exists_once(self):
        from .models import TenderComputeWorkUnit
        item, component = self.working()
        plan = ComponentRoutePlan.objects.create(commercial_item=item, scope='shared')
        process = ProcessDefinition.objects.create(name='Общая доставка')
        steps = [ComponentOperationStep.objects.create(route_plan=plan, process=process, position=i) for i in (1, 2)]
        job = TenderComputeJob.objects.create(tender=self.tender)
        unit = TenderComputeWorkUnit.objects.create(job=job, operation_step=steps[0], engine_key='shared', dedupe_key='delivery', input_fingerprint='saved', status='ready', result={'cost':'25'})
        unit.operation_steps.add(*steps)
        projection = self.read()
        self.assertEqual(list(projection['calculation_results']), [f'work_unit:{unit.pk}'])
        for step in projection['items'][0]['shared_routes'][0]['steps']:
            self.assertEqual(step['calculations']['unverified'], [f'work_unit:{unit.pk}'])
        self.assertEqual(projection['items'][0]['components'][0]['calculations']['unverified'], [])
        self.assertNotEqual(projection['items'][0]['state'], 'ready')

    def test_complex_set_preserves_all_nested_objects_and_defers_nested_quantity(self):
        item, parent = self.working()
        inner = CalculationComponent.objects.create(commercial_item=item, parent=parent, name='Вложенный набор', quantity_per_parent=3)
        leaf = CalculationComponent.objects.create(commercial_item=item, parent=inner, name='Лента', quantity_per_parent=4)
        sibling = CalculationComponent.objects.create(commercial_item=item, name='Коробка', quantity_per_parent=1)
        projection = self.read()
        roots = projection['items'][0]['components']
        self.assertEqual({c['id'] for c in roots}, {parent.pk, sibling.pk})
        child = roots[0]['components'][0]
        self.assertEqual(child['id'], inner.pk)
        self.assertEqual(child['components'][0]['id'], leaf.pk)
        self.assertIsNone(child['quantity'])
        self.assertEqual(child['quantity_per_parent'], '3.0000')
        self.assertNotEqual(projection['items'][0]['state'], 'ready')

    def test_component_result_cannot_cover_uncomputed_required_operations(self):
        from unittest.mock import patch
        item, component = self.working()
        plan = ComponentRoutePlan.objects.create(commercial_item=item, component=component, metadata={'input_fingerprint':'current'})
        process = ProcessDefinition.objects.create(name='Обязательная операция')
        ComponentOperationStep.objects.create(route_plan=plan, process=process, position=1)
        job = TenderComputeJob.objects.create(tender=self.tender, status='ready')
        TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=component, status='ready', input_snapshot={'quantity':str(component.effective_quantity), 'requirements':component.requirements, 'input_fingerprint':'current'}, result={'cost':'10'})
        with patch('tenders.working_items.source_route_snapshots', return_value={self.source.pk:{'plan_id':plan.pk,'state':'ready'}}):
            projection = self.read()
        component_data = projection['items'][0]['components'][0]
        self.assertTrue(component_data['calculations']['current'])
        self.assertEqual(component_data['routes'][0]['steps'][0]['state'], 'not_calculated')
        self.assertNotEqual(projection['items'][0]['state'], 'ready')

    def test_unprepared_source_retains_existing_line_result(self):
        job = TenderComputeJob.objects.create(tender=self.tender)
        line = TenderComputeLine.objects.create(job=job, source_item=self.source, status='ready', result={'cost':'7'})
        projection = self.read()
        self.assertEqual(projection['items'][0]['calculations']['unverified'], [f'line:{line.pk}'])
        self.assertEqual(projection['calculation_results'][f'line:{line.pk}']['result'], {'cost':'7'})

    def test_historical_component_and_its_result_are_not_lost(self):
        item, component = self.working()
        archived = CalculationComponent.objects.create(commercial_item=item, source_item=self.source, name='Старая коробка', status='inactive')
        job = TenderComputeJob.objects.create(tender=self.tender)
        line = TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=archived, status='ready', result={'cost':'8'})
        projection = self.read()
        history = projection['items'][0]['historical_components']
        self.assertEqual([c['id'] for c in history], [archived.pk])
        self.assertEqual(history[0]['calculations']['historical'], [f'line:{line.pk}'])
        self.assertFalse(projection['calculation_results'][f'line:{line.pk}']['usable'])

    def test_distinct_components_with_same_source_keep_their_routes(self):
        from unittest.mock import patch
        item, first = self.working()
        second = CalculationComponent.objects.create(commercial_item=item, source_item=self.source, name='Второй компонент')
        one = ComponentRoutePlan.objects.create(commercial_item=item, component=first)
        two = ComponentRoutePlan.objects.create(commercial_item=item, component=second)
        with patch('tenders.working_items.source_route_snapshots', return_value={self.source.pk:{'plan_id':two.pk,'state':'ready'}}):
            tree = self.read()['items'][0]
        routes = {c['id']:c['routes'][0] for c in tree['components']}
        self.assertEqual(routes[first.pk]['id'], one.pk)
        self.assertFalse(routes[first.pk]['historical'])
        self.assertFalse(routes[second.pk]['historical'])

    def test_component_cannot_inherit_notice_price_of_its_set(self):
        from .test_calculation_v2_pipeline import notification, raw_item
        row = raw_item('Набор', '2')
        row.update(price='100.00', sum='200.00')
        self.tender.notification_raw = notification([row])
        self.tender.save(update_fields=['notification_raw'])
        self.source.metadata = {'notification_index':0}
        self.source.save(update_fields=['metadata'])
        derived = TenderSourceItem.objects.create(tender=self.tender, parent=self.source, source_key='part', source_type='document_component', original_text='Часть набора', metadata={'relationship':'component'})
        item = TenderCommercialItem.objects.create(tender=self.tender, source_key='part', display_name='Часть набора', quantity=2)
        item.source_items.add(derived)
        node = next(n for n in self.read()['items'] if n['id'] == item.pk)
        self.assertEqual(node['notice_prices'], [])

    def test_pending_shared_result_supersedes_old_result_without_losing_it(self):
        from .models import TenderComputeWorkUnit
        item, component = self.working()
        plan = ComponentRoutePlan.objects.create(commercial_item=item, scope='shared')
        process = ProcessDefinition.objects.create(name='Доставка')
        step = ComponentOperationStep.objects.create(route_plan=plan, process=process, position=1)
        old_job = TenderComputeJob.objects.create(tender=self.tender)
        new_job = TenderComputeJob.objects.create(tender=self.tender)
        old = TenderComputeWorkUnit.objects.create(job=old_job, operation_step=step, engine_key='delivery', dedupe_key='same', input_fingerprint='old', status='ready', result={'cost':'25'})
        new = TenderComputeWorkUnit.objects.create(job=new_job, operation_step=step, engine_key='delivery', dedupe_key='same', input_fingerprint='new', status='queued')
        projection = self.read()
        buckets = projection['items'][0]['shared_routes'][0]['steps'][0]['calculations']
        self.assertEqual(buckets['historical'], [f'work_unit:{old.pk}'])
        self.assertEqual(buckets['pending'], [f'work_unit:{new.pk}'])
        self.assertEqual(len(projection['calculation_results']), 2)
        self.assertNotEqual(projection['items'][0]['state'], 'ready')

    def test_result_without_an_active_working_owner_is_preserved_unattached(self):
        item, component = self.working()
        item.status = 'inactive'
        item.save(update_fields=['status'])
        job = TenderComputeJob.objects.create(tender=self.tender)
        line = TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=component, status='ready', result={'cost':'9'})
        projection = self.read()
        self.assertEqual(projection['unattached_calculations'], [f'line:{line.pk}'])
        self.assertEqual(projection['calculation_results'][f'line:{line.pk}']['result'], {'cost':'9'})
        self.assertFalse(projection['calculation_results'][f'line:{line.pk}']['usable'])
        self.assertEqual(projection['items'][0]['kind'], 'source')
