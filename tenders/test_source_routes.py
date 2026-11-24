from decimal import Decimal
from unittest.mock import Mock, patch
from django.test import TestCase, override_settings
from tender_selection.models import Tender
from .models import ComponentRoutePlan, ProcessDefinition, TenderComputeJob, OwnerInteraction
from .calculation_v2_pipeline import RouteDecision, ingest_source_items, route_tender_batch
from .test_calculation_v2_pipeline import notification, raw_item


class SourceRouteTests(TestCase):
    def test_refresh_keeps_latest_accepted_revision_without_reactivating_its_ancestor(self):
        from .calculation_v2_pipeline import active_calculation_items
        intermediate = type(self.source).objects.create(tender=self.tender, parent=self.source,
            source_key='accepted-first', source_type='document_extraction', is_active=False,
            original_text='Первое уточнение', quantity=self.source.quantity, unit=self.source.unit)
        current = type(self.source).objects.create(tender=self.tender, parent=intermediate,
            source_key='accepted-second', source_type='document_extraction', is_active=True,
            original_text='Принятый товар', quantity=self.source.quantity, unit=self.source.unit)
        self.source.is_active = False
        self.source.save(update_fields=['is_active'])
        ingest_source_items(self.tender)
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_active)
        self.assertEqual([item.pk for item in active_calculation_items(self.tender)], [current.pk])
        # Also protect the canonical view of already inconsistent historical flags.
        type(self.source).objects.filter(pk=self.source.pk).update(is_active=True)
        self.assertEqual([item.pk for item in active_calculation_items(self.tender)], [current.pk])

    def test_bad_product_preparation_does_not_rollback_other_routes(self):
        other = type(self.source).objects.create(tender=self.tender, source_key='other', original_text='Набор', quantity=1)
        type(self.source).objects.create(tender=self.tender, parent=other, source_key='broken-component',
            source_type='document_component', original_text='Составляющая', quantity=1,
            metadata={'quantity_per_parent': 'не определено'})
        job = self.run_route()
        self.assertEqual(ComponentRoutePlan.objects.get().component.source_item_id, self.source.pk)
        self.assertTrue(any(gap.get('source_id') == other.pk for gap in job.diagnostics['analysis_gaps']))

    def test_error_saving_one_route_does_not_rollback_another_product(self):
        from .calculation_v2_pipeline import _component_conflicts
        other = type(self.source).objects.create(tender=self.tender, source_key='other', original_text='Другой товар', quantity=1)
        def conflicts(commercial):
            if commercial.source_items.filter(pk=other.pk).exists():
                raise ValueError('Ошибка сведений одного товара')
            return _component_conflicts(commercial)
        with patch('tenders.calculation_v2_pipeline._component_conflicts', side_effect=conflicts):
            job = self.run_route()
        self.assertEqual(ComponentRoutePlan.objects.get().component.source_item_id, self.source.pk)
        self.assertTrue(any(gap.get('source_id') == other.pk for gap in job.diagnostics['analysis_gaps']))

    def test_specification_change_rebuilds_only_affected_route_and_preserves_history(self):
        other = type(self.source).objects.create(tender=self.tender, source_key='other', original_text='Другой товар', quantity=1)
        self.run_route()
        original = dict(ComponentRoutePlan.objects.values_list('component__source_item_id', 'pk'))
        self.source.requirements = {'characteristics': [{'name': 'Материал', 'value': 'Бумага'}]}
        self.source.save(update_fields=['requirements'])
        self.run_route()
        requested = self.router.route.call_args.kwargs['items']
        self.assertEqual([item.source_item_id for item in requested], [self.source.pk])
        self.assertEqual(ComponentRoutePlan.objects.get(pk=original[other.pk]).status, 'planned')
        self.assertEqual(ComponentRoutePlan.objects.get(pk=original[self.source.pk]).status, 'superseded')
        self.assertEqual(ComponentRoutePlan.objects.count(), 3)

    def test_missing_new_route_keeps_previous_saved_steps_and_does_not_duplicate_empty_results(self):
        self.run_route()
        plan = ComponentRoutePlan.objects.get()
        previous_steps = list(plan.steps.values_list('pk', 'process_id'))
        self.source.requirements = {'characteristics': [{'name': 'Материал', 'value': 'Уточнён'}]}
        self.source.save(update_fields=['requirements'])
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (), Decimal('0'),
            {'reason': 'Новый маршрут не получен'}, True) for item in kwargs['items']]
        self.run_route()
        job = self.run_route()
        job.status = 'needs_review'
        job.save(update_fields=['status'])
        self.assertEqual(ComponentRoutePlan.objects.count(), 1)
        self.assertEqual(list(plan.steps.values_list('pk', 'process_id')), previous_steps)
        from .calculation_v2_pipeline import source_route_snapshots
        self.assertEqual(source_route_snapshots(self.tender)[self.source.pk]['state'], 'stale')

    @override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True,
        CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {
            'max_input_tokens': 200000, 'max_output_tokens': 32000}})
    def test_bad_route_response_is_isolated_and_only_missing_route_is_retried(self):
        from .calculation_v2_pipeline import GatewayBatchRouter
        other = type(self.source).objects.create(tender=self.tender, source_key='other', original_text='Другой товар', quantity=1)
        job = TenderComputeJob.objects.create(tender=self.tender)
        def response(prompt, **kwargs):
            import json
            items = json.loads(prompt.rsplit('\n', 1)[1])['items']
            return {'items': [{'component_id': item['component_id'],
                'process_ids': [self.stage.pk] if item['name'] == self.source.original_text else ['неизвестный этап'],
                'confidence': 1, 'reason': 'Сохранённый ответ'} for item in items]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=response):
            route_tender_batch(job, GatewayBatchRouter(max_cost_rub=10))
        good = ComponentRoutePlan.objects.get(component__source_item=self.source)
        self.assertTrue(good.steps.exists())
        self.assertFalse(ComponentRoutePlan.objects.get(component__source_item=other).steps.exists())
        self.run_route()
        self.assertEqual([item.source_item_id for item in self.router.route.call_args.kwargs['items']], [other.pk])
        self.assertEqual(ComponentRoutePlan.objects.get(pk=good.pk).status, 'planned')

    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="synthetic-route", notification_raw=notification([raw_item("Изделие", "12")]))
        self.source = ingest_source_items(self.tender)[0]
        self.stage = ProcessDefinition.objects.create(name="Исполнение", description="Подготовка готового изделия")
        self.router = Mock(cost_rub=0, diagnostics={})
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal(".9"), {"reason": "Подходит по ТЗ"}) for item in kwargs["items"]]

    def run_route(self):
        job = TenderComputeJob.objects.create(tender=self.tender)
        route_tender_batch(job, self.router)
        return job

    def test_cached_route_keeps_steps_and_avoids_second_model_call_without_estimate(self):
        self.run_route()
        plan = ComponentRoutePlan.objects.get()
        step_id = plan.steps.get().pk
        self.run_route()
        self.assertEqual(self.router.route.call_count, 1)
        self.assertEqual(plan.steps.get().pk, step_id)
        self.assertFalse(self.tender.estimates.exists())

    def test_source_or_stage_changes_invalidate_route(self):
        self.run_route()
        self.source.requirements = {"characteristics": [{"name": "Размер", "value": "А5"}]}
        self.source.save()
        self.run_route()
        self.stage.description = "Исполнение с упаковкой"; self.stage.save()
        self.run_route()
        self.assertEqual(self.router.route.call_count, 3)

    def test_edit_during_model_call_is_not_saved_as_current_route(self):
        def changed(**kwargs):
            self.source.quantity = 99; self.source.save()
            return [RouteDecision(item.pk, (self.stage.pk,), Decimal(".9"), {}) for item in kwargs["items"]]
        self.router.route.side_effect = changed
        job = self.run_route()
        self.assertTrue(any('изменились' in gap['reason'] for gap in job.diagnostics['analysis_gaps']))
        self.assertFalse(ComponentRoutePlan.objects.exists())

    def test_no_stages_needs_review_without_paid_call_or_duplicate_questions(self):
        ProcessDefinition.objects.update(is_active=False)
        self.run_route(); self.run_route()
        self.router.route.assert_not_called()
        self.assertEqual(ComponentRoutePlan.objects.get().status, "needs_review")
        self.assertEqual(OwnerInteraction.objects.count(), 0)

    def test_answer_to_route_question_invalidates_cache_and_reaches_router(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import answer_tender_owner_question
        user = get_user_model().objects.create_superuser('route-answer', password='test')
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.9'), {}, True, 'Рабочий цвет?', 'execution_choice', ('Цвет',), ('Красный', 'Синий')) for item in kwargs['items']]
        self.run_route()
        question = OwnerInteraction.objects.get(status='suggested')
        answer_tender_owner_question(user, self.tender.pk, question.pk, 'Красный')
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.9'), {}) for item in kwargs['items']]
        self.run_route()
        self.assertEqual(self.router.route.call_count, 2)
        answers = self.router.route.call_args.kwargs['items'][0].requirements['owner_answers']
        self.assertEqual(answers[0]['answer'], 'Красный')
        from .calculation_v2_pipeline import source_route_snapshots
        self.assertEqual(source_route_snapshots(self.tender)[self.source.pk]['state'], 'ready')
        self.assertFalse(OwnerInteraction.objects.filter(status='open').exists())

    def test_answered_source_conflict_is_not_reopened_or_written_into_customer_tz(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import answer_tender_owner_question
        from .calculation_v2_pipeline import source_route_snapshots
        user = get_user_model().objects.create_superuser('conflict-answer', password='test')
        requirements = {"characteristics": [{"name": "Покрытие", "value": "матовое"}, {"name": "Покрытие", "value": "глянцевое"}]}
        self.source.requirements = requirements
        self.source.save()
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.9'), {}, True, 'Уточните покрытие.', 'conflicting_information') for item in kwargs['items']]
        self.run_route()
        question = OwnerInteraction.objects.get(status='suggested')
        self.assertEqual(question.possible_answers['options'], ['матовое', 'глянцевое'])
        # Legacy stored interactions used conflicts, without missing_requirement_names.
        question.context['missing_requirement_names'] = []
        question.save()
        answer_tender_owner_question(user, self.tender.pk, question.pk, 'матовое')
        self.run_route()
        self.assertFalse(OwnerInteraction.objects.filter(status='open').exists())
        self.assertEqual(source_route_snapshots(self.tender)[self.source.pk]['state'], 'ready')
        self.source.refresh_from_db()
        self.assertEqual(self.source.requirements, requirements)
        self.run_route()
        self.assertEqual(self.router.route.call_count, 2)

    def test_legacy_source_clarification_is_not_selected_as_an_execution_method(self):
        from .models import OwnerFeedbackEvent
        from .calculation_v2_pipeline import source_route_snapshots
        question = OwnerInteraction.objects.create(tender=self.tender, source_item=self.source, status='resolved', question='Цвет?', context={'conflicts': ['Цвет']})
        OwnerFeedbackEvent.objects.create(interaction=question, raw_text='Серебристый', scope='current_tender')
        self.run_route()
        snapshot = source_route_snapshots(self.tender)[self.source.pk]
        self.assertIsNone(snapshot['interaction'])
        self.assertEqual(ComponentRoutePlan.objects.get().metadata['selected_choice'], '')
        self.assertEqual(self.router.route.call_args.kwargs['items'][0].requirements['owner_answers'][0]['answer'], 'Серебристый')

    def test_assumption_is_usable_without_answer_and_corrections_remain_editable(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import answer_tender_owner_question
        from .calculation_v2_pipeline import source_route_snapshots
        from .models import OwnerFeedbackEvent
        user = get_user_model().objects.create_superuser('route-override', password='test')
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.6'), {"reason": "Наиболее вероятен готовый результат"}, True, '', 'execution_choice', (), ('Готовое изделие', 'Отдельное нанесение'), 'Готовое изделие') for item in kwargs['items']]
        self.run_route()
        snapshot = source_route_snapshots(self.tender)[self.source.pk]
        self.assertEqual(snapshot['state'], 'ready')
        self.assertTrue(snapshot['assumption'])
        self.assertEqual(snapshot['interaction']['selected'], 'Готовое изделие')
        self.assertFalse(OwnerInteraction.objects.filter(status='open').exists())
        interaction = OwnerInteraction.objects.get(status='suggested')
        answer_tender_owner_question(user, self.tender.pk, interaction.pk, 'Своя технология с отдельной упаковкой')
        self.run_route()
        answer_tender_owner_question(user, self.tender.pk, interaction.pk, 'Отдельное нанесение')
        answer_tender_owner_question(user, self.tender.pk, interaction.pk, 'Отдельное нанесение')
        self.run_route()
        self.assertEqual(OwnerFeedbackEvent.objects.filter(interaction=interaction).count(), 2)
        inputs = self.router.route.call_args.kwargs['items'][0].requirements['owner_answers']
        self.assertEqual([x['answer'] for x in inputs], ['Отдельное нанесение'])
        self.assertEqual(source_route_snapshots(self.tender)[self.source.pk]['interaction']['selected'], 'Отдельное нанесение')
        self.run_route()
        self.assertEqual(self.router.route.call_count, 3)
        self.assertFalse(OwnerInteraction.objects.filter(status='open').exists())

    def test_route_budget_blocks_unpriced_call_before_gateway(self):
        from .calculation_v2_pipeline import GatewayBatchRouter, build_commercial_items
        from .models import CalculationComponent
        job = TenderComputeJob.objects.create(tender=self.tender)
        build_commercial_items(job)
        with patch('tenders.services._ai_gateway_json') as gateway, self.assertRaises(ValueError):
            GatewayBatchRouter(max_cost_rub=0).route(tender=self.tender, items=list(CalculationComponent.objects.all()), processes=[self.stage], knowledge=[])
        gateway.assert_not_called()

    def test_unresolved_route_does_not_plan_paid_cache_preparation(self):
        from .preparation import planned_tasks
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.5'), {}, True, 'Нужно уточнение') for item in kwargs['items']]
        job = self.run_route()
        engine = Mock()
        engine.supports_preparation.return_value = True
        engine.plan_tender_preparation.side_effect = lambda job, steps: [object() for step in steps]
        with patch('tenders.preparation._preparation_engines', return_value=[engine]):
            self.assertEqual(planned_tasks(job), [])
