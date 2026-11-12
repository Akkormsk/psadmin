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
        self.assertEqual(tree['calculations'], [])
        self.assertEqual(tree['components'], [])
        self.assertEqual(TenderSourceItem.objects.count(), before)
        self.assertFalse(TenderCommercialItem.objects.exists())

    def test_missing_results_does_not_invent_cost(self):
        self.working()
        tree = self.read()['items'][0]
        self.assertEqual(tree['state'], 'not_calculated')
        self.assertEqual(tree['components'][0]['calculations'], [])

    def test_stale_result_remains_visible_but_is_not_usable(self):
        item, component = self.working()
        job = TenderComputeJob.objects.create(tender=self.tender)
        line = TenderComputeLine.objects.create(job=job, source_item=self.source, commercial_item=item, component=component, status='ready', input_snapshot={'quantity':'1','requirements':component.requirements}, result={'cost':'10'})
        tree = self.read()['items'][0]
        result = tree['components'][0]['calculations'][0]
        self.assertEqual(result['id'], line.pk)
        self.assertEqual(result['freshness'], 'stale')
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
        tree = self.read()['items'][0]
        self.assertEqual(tree['work_results'][0]['id'], unit.pk)
        self.assertEqual(tree['work_results'][0]['operation_step_ids'], [step.pk])
        self.assertEqual(tree['work_results'][0]['freshness'], 'unknown')

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
        result = self.read()['items'][0]['components'][0]['calculations'][0]
        self.assertEqual(result['selection']['counterparty_id'], provider.pk)
        self.assertEqual(result['selection']['calculator_id'], binding.pk)
        self.assertEqual(result['freshness'], 'unknown')
        self.assertFalse(result['usable'])
