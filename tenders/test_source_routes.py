from decimal import Decimal
from unittest.mock import Mock, patch
from django.test import TestCase
from tender_selection.models import Tender
from .models import ComponentRoutePlan, ProcessDefinition, TenderComputeJob, OwnerInteraction
from .calculation_v2_pipeline import RouteDecision, ingest_source_items, route_tender_batch
from .test_calculation_v2_pipeline import notification, raw_item


class SourceRouteTests(TestCase):
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
        with self.assertRaisesMessage(ValueError, "изменились"):
            self.run_route()
        self.assertFalse(ComponentRoutePlan.objects.exists())

    def test_no_stages_needs_review_without_paid_call_or_duplicate_questions(self):
        ProcessDefinition.objects.update(is_active=False)
        self.run_route(); self.run_route()
        self.router.route.assert_not_called()
        self.assertEqual(ComponentRoutePlan.objects.get().status, "needs_review")
        self.assertEqual(OwnerInteraction.objects.count(), 1)

    def test_answer_to_route_question_invalidates_cache_and_reaches_router(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import answer_tender_owner_question
        user = get_user_model().objects.create_superuser('route-answer', password='test')
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.9'), {}, True, 'Укажите цвет.', 'missing_information', ('Цвет',)) for item in kwargs['items']]
        self.run_route()
        question = OwnerInteraction.objects.get(status='open')
        answer_tender_owner_question(user, self.tender.pk, question.pk, 'Красный')
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.9'), {}) for item in kwargs['items']]
        self.run_route()
        self.assertEqual(self.router.route.call_count, 2)
        answers = self.router.route.call_args.kwargs['items'][0].requirements['owner_answers']
        self.assertEqual(answers[0]['answer'], 'Красный')
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
