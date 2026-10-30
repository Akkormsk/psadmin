from unittest.mock import Mock, patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from tenders import test_source_routes as route_fixtures
from tenders.models import TenderComputeJob
from .services import _run_document_analysis, refresh_document_analysis, source_route_for


class IntakeRefreshTests(TestCase):
    def setUp(self):
        route_fixtures.SourceRouteTests.setUp(self)
        self.user = get_user_model().objects.create_superuser('intake-owner', password='test')
        self.client.force_login(self.user)

    def create_job(self):
        with patch('threading.Thread'), self.captureOnCommitCallbacks(execute=True):
            result = refresh_document_analysis(self.user, self.tender.pk)
        return TenderComputeJob.objects.get(pk=result['job_id'])

    def run_job(self, job):
        with patch('tender_selection.services.notification_for', return_value=self.tender.notification_raw) as notification, patch('tender_selection.services.extras_for'), patch('tenders.calculation_v2_pipeline.enrich_suspicious_tender', return_value={'created': [], 'state': 'no_change', 'diagnostics': []}) as enrich:
            _run_document_analysis(job.pk, enricher=Mock(cost_rub=0, diagnostics={}), router=self.router)
        job.refresh_from_db()
        return notification, enrich

    def test_one_refresh_loads_notification_then_routes_without_estimate_or_provider_calculators(self):
        job = self.create_job()
        with patch('tenders.provider_execution.calculate_routed_provider_lines') as calculate:
            notification, enrich = self.run_job(job)
        self.assertEqual(job.status, 'ready')
        notification.assert_called_once_with(job.tender, force=True)
        self.assertEqual(enrich.call_count, 1)
        calculate.assert_not_called()
        self.assertFalse(self.tender.estimates.exists())
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.completed_at)
        self.assertEqual(source_route_for(self.user, self.tender.pk, self.source.pk)['state'], 'ready')

    def test_second_refresh_reuses_document_analysis_and_saved_route(self):
        self.run_job(self.create_job())
        notification, enrich = self.run_job(self.create_job())
        notification.assert_called_once()
        enrich.assert_not_called()
        self.assertEqual(self.router.route.call_count, 1)

    def test_routing_job_blocks_duplicate_click_and_progress_is_read_only(self):
        job = self.create_job(); job.status = 'routing'; job.diagnostics['refresh'] = {'stage': 'routing', 'label': 'Строим маршруты…'}; job.save()
        with patch('threading.Thread') as thread, self.captureOnCommitCallbacks(execute=True):
            self.assertFalse(refresh_document_analysis(self.user, self.tender.pk)['started'])
        thread.assert_not_called()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('tender_selection:analysis_status'), {'ids': self.tender.pk, 'details': self.tender.pk})
        state = response.json()['statuses'][0]
        self.assertEqual(state['label'], 'Строим маршруты…')
        self.assertEqual(state['progress']['stage'], 'routing')
        self.assertIn('elapsed_seconds', state['progress'])
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))

    def test_saved_route_read_uses_same_service_and_never_runs_model(self):
        self.run_job(self.create_job())
        url = reverse('tender_selection:item_route', args=[self.tender.pk, self.source.pk])
        with patch('tenders.services._ai_gateway_json') as gateway:
            response = self.client.get(url)
        gateway.assert_not_called()
        self.assertEqual(response.json(), source_route_for(self.user, self.tender.pk, self.source.pk))
        from tenders.assistant_tools import execute_tool
        self.assertEqual(execute_tool('tender.item_route', self.user, {'tender_id': self.tender.pk, 'source_item_id': self.source.pk}), response.json())
        stranger = get_user_model().objects.create_user('stranger')
        with self.assertRaises(ValueError):
            source_route_for(stranger, self.tender.pk, self.source.pk)

    def test_failed_notification_without_cache_does_not_build_route(self):
        self.tender.notification_raw = {}; self.tender.save()
        job = self.create_job()
        with patch('tender_selection.services.notification_for', return_value=None), patch('tender_selection.services.extras_for'):
            _run_document_analysis(job.pk, router=self.router)
        job.refresh_from_db()
        self.assertEqual(job.status, 'partial')
        self.assertIn('извещение', job.error['message'])
        self.router.route.assert_not_called()

    def test_incomplete_notification_response_does_not_replace_saved_full_data(self):
        from .services import notification_for
        before = self.tender.notification_raw
        with patch('tender_selection.services.gosplan.fetch_notification', return_value={'source': {}}):
            self.assertIsNone(notification_for(self.tender, force=True))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.notification_raw, before)
        self.assertIn('неполные', self.tender.notification_error)

    def test_complete_cached_notification_is_not_refetched_on_every_click(self):
        self.tender.notification_raw['source']['purchaseResponsibleInfo'] = {'responsibleOrgInfo': {'fullName': 'Заказчик'}}
        self.tender.notification_raw['source']['attachmentsInfo']['attachmentInfo'] = [{'url': 'https://zakupki.gov.ru/synthetic-spec', 'fileName': 'ТЗ.docx'}]
        self.tender.save()
        notification, enrich = self.run_job(self.create_job())
        notification.assert_not_called()

    def test_route_question_remains_visible_after_commercial_projection_is_created(self):
        from decimal import Decimal
        from tenders.calculation_v2_pipeline import RouteDecision
        self.router.route.side_effect = lambda **kwargs: [RouteDecision(item.pk, (self.stage.pk,), Decimal('.5'), {}, True, 'Укажите цвет изделия.', 'missing_information', ('Цвет',)) for item in kwargs['items']]
        self.run_job(self.create_job())
        response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]), {'analysis_snapshot': '1', 'workspace': '1'})
        self.assertContains(response, 'Укажите цвет изделия.')
        self.assertContains(response, 'Маршрут · нужно уточнение')

    def test_full_card_snapshot_is_read_only_and_does_not_call_ai(self):
        self.run_job(self.create_job())
        with CaptureQueriesContext(connection) as queries, patch('tenders.services._ai_gateway_json') as gateway:
            response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]), {'analysis_snapshot': '1', 'full_card': '1', 'workspace': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-card-refresh="notification"')
        self.assertContains(response, 'data-source-route=')
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        gateway.assert_not_called()
