from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase

from tender_selection.services import decide_document_proposals, push_to_estimate, risk_assessment_for, _save_risk_result
from . import test_document_components as fixtures
from .calculation_v2_pipeline import RouteDecision, route_tender_batch, source_route_snapshots
from .models import ComponentRoutePlan, ProcessDefinition, TenderAnalysis, TenderComputeJob, TenderComputeWorkUnit, TenderEstimate
from .input_freshness import estimate_input_status


class InputDependenciesSmokeTests(TestCase):
    setUp = fixtures.DocumentComponentAcceptanceTests.setUp
    row = fixtures.DocumentComponentAcceptanceTests.row
    stage = fixtures.DocumentComponentAcceptanceTests.stage

    def prepare(self):
        process = ProcessDefinition.objects.create(name='Изготовление')
        self.router = Mock(cost_rub=0, diagnostics={})
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (process.pk,), Decimal('.9'), {}) for item in kwargs['items']]
        job = TenderComputeJob.objects.create(tender=self.tender, status='ready')
        route_tender_batch(job, self.router)
        self.old_plan = ComponentRoutePlan.objects.filter(component__source_item=self.parents[0]).get()
        self.old_step = self.old_plan.steps.get()
        TenderComputeWorkUnit.objects.create(job=job, component=self.old_plan.component, operation_step=self.old_step,
            engine_key='saved', dedupe_key='saved', input_fingerprint='saved', result={'cost': '500'})
        self.estimate = TenderEstimate.objects.get(pk=push_to_estimate(self.tender, self.admin))
        import json
        from .estimate_actions import persist_estimate
        from .models import TenderSettings
        lines = [{'id': line.pk, 'name': line.name, 'quantity': str(line.quantity), 'nmck_unit': str(line.nmck_unit),
            'material_unit': str(line.nmck_unit / 2), 'application_unit': '0', 'logistics_unit': '0', 'requirements': line.requirements}
            for line in self.estimate.lines.all()]
        self.estimate, error, _ = persist_estimate(self.admin, {'tender_number': self.estimate.tender_number,
            'name': self.estimate.name, 'lines_json': json.dumps(lines)}, self.estimate, TenderSettings.objects.get(pk=1), pipeline=True)
        self.assertIsNone(error)
        self.old_summary = deepcopy(self.estimate.summary_snapshot)
        _save_risk_result(self.tender, {'risk_factors': [], 'risk_facts': {'documents_sufficient': True}, 'legal_risks': 'Прежняя оценка'}, ['ТЗ'])

    def test_accepted_changes_mark_inputs_without_rewriting_saved_prices(self):
        self.prepare()
        self.assertEqual(estimate_input_status(self.estimate)['state'], 'current')
        proposal = self.stage([self.row(self.parents[0], relationship='enrich', quantity=100)])['created'][0]
        decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'accept', schedule_routes=False)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.summary_snapshot, self.old_summary)
        self.assertEqual(estimate_input_status(self.estimate)['state'], 'stale')
        self.assertEqual(self.estimate.display_status(), 'Требует проверки')
        self.assertTrue(self.estimate.is_active_task())
        self.assertEqual(self.estimate.input_status_label(), estimate_input_status(self.estimate)['label'])
        self.tender.refresh_from_db()
        with patch('tender_selection.risk_assessment.assess', side_effect=AssertionError('Automatic paid assessment')):
            risk = risk_assessment_for(self.tender)
        self.assertTrue(risk['stale'])
        self.assertEqual(risk['legal_risks'], 'Прежняя оценка')
        self.assertFalse(TenderAnalysis.objects.filter(tender=self.tender, kind='risk', is_current=True).exists())
        self.assertEqual(source_route_snapshots(self.tender)[proposal.pk]['state'], 'stale')
        from django.urls import reverse
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid read')):
            response = self.client.get(reverse('tender_worklist_estimate', args=[self.estimate.pk]))
        self.assertContains(response, 'Исходные данные изменились')

    def test_route_revision_keeps_old_operations_and_saved_work_unit(self):
        self.prepare()
        self.stage([self.row(self.parents[0], relationship='enrich', quantity=100)])
        decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'accept', schedule_routes=False)
        job = TenderComputeJob.objects.create(tender=self.tender, status='ready')
        route_tender_batch(job, self.router)
        newest = ComponentRoutePlan.objects.filter(component_id=self.old_plan.component_id).latest('pk')
        self.assertEqual(newest.supersedes_id, self.old_plan.pk)
        self.assertEqual(self.old_plan.steps.get().pk, self.old_step.pk)
        self.assertTrue(TenderComputeWorkUnit.objects.filter(operation_step=self.old_step, result={'cost': '500'}).exists())

    def test_reject_does_not_invalidate_accepted_inputs(self):
        self.prepare()
        self.stage([self.row(self.parents[0], relationship='enrich', quantity=100)])
        decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'reject', schedule_routes=False)
        self.assertEqual(estimate_input_status(self.estimate)['state'], 'current')
        self.tender.refresh_from_db()
        self.assertFalse(self.tender.risk_assessment.get('stale'))

    def test_response_to_old_inputs_cannot_publish_as_current_risk(self):
        self.prepare()
        self.stage([self.row(self.parents[0], relationship='enrich', quantity=100)])
        previous = TenderAnalysis.objects.get(tender=self.tender, kind='risk', is_current=True)
        def answer(context):
            decide_document_proposals(self.admin, self.tender.pk, self.parents[0].pk, 'accept', schedule_routes=False)
            return {'data': {'risk_factors': [], 'risk_facts': {'documents_sufficient': True}, 'legal_risks': 'Ответ по старому ТЗ'}}
        url = 'https://zakupki.gov.ru/components'
        with patch('tender_selection.risk_assessment.select_documents', return_value=[{'url': url, 'name': 'ТЗ'}]), patch('tender_selection.services.documents_html', return_value={url: '<p>ТЗ</p>'}), patch('tender_selection.risk_assessment.assess', side_effect=answer) as ai:
            result = risk_assessment_for(self.tender, force=True)
        ai.assert_called_once()
        self.assertTrue(result['stale'])
        self.assertEqual(result['legal_risks'], 'Прежняя оценка')
        self.assertFalse(TenderAnalysis.objects.filter(tender=self.tender, kind='risk', is_current=True).exists())
        _save_risk_result(self.tender, {'risk_factors': [], 'risk_facts': {'documents_sufficient': True}}, ['ТЗ'])
        latest = TenderAnalysis.objects.get(tender=self.tender, kind='risk', is_current=True)
        self.assertEqual(latest.supersedes_id, previous.pk)

    def test_reading_freshness_is_read_only_and_preserves_archive(self):
        from django.db import connection
        self.prepare()
        def select_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(select_only):
            self.assertEqual(estimate_input_status(self.estimate)['state'], 'current')
        self.tender.status = self.tender.DISMISSED
        self.tender.save(update_fields=['status'])
        with connection.execute_wrapper(select_only):
            estimate_input_status(self.estimate)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.summary_snapshot, self.old_summary)

    def test_completed_job_inputs_are_not_overwritten_by_route_rebuild(self):
        self.prepare()
        line = self.old_plan.component.compute_lines.get()
        line.result = {'cost': '500'}
        line.status = 'ready'
        line.save(update_fields=['result', 'status'])
        saved_inputs = deepcopy(line.input_snapshot)
        self.parents[0].requirements = {'characteristics': [{'name': 'Материал', 'value': 'металл'}]}
        self.parents[0].save(update_fields=['requirements'])
        with self.assertRaisesRegex(ValueError, 'прежним требованиям'):
            route_tender_batch(line.job, self.router)
        line.refresh_from_db()
        self.assertEqual(line.input_snapshot, saved_inputs)
        self.assertEqual(line.result, {'cost': '500'})
