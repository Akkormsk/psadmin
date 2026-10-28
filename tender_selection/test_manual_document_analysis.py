from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tender_selection.models import Tender
from tender_selection.services import refresh_document_analysis, _run_document_analysis
from tenders.models import TenderComputeJob


class ManualDocumentAnalysisTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('manual-owner', password='test')
        self.tender = Tender.objects.create(purchase_number='manual-example', title='Проверка запуска', notification_raw={'source': {}})
        self.client.force_login(self.user)

    def test_single_run_and_duplicate_click(self):
        with patch('threading.Thread') as thread, self.captureOnCommitCallbacks(execute=True):
            first = refresh_document_analysis(self.user, self.tender.pk)
            second = refresh_document_analysis(self.user, self.tender.pk)
            self.assertTrue(first['started'])
            self.assertFalse(second['started'])
        thread.return_value.start.assert_called_once()
        self.assertEqual(TenderComputeJob.objects.count(), 1)

    def test_post_keeps_workspace_and_rejects_external_redirect(self):
        url = reverse('tender_selection:refresh_analysis', args=[self.tender.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url + '?workspace=1', {'next': 'https://evil.test/'})
        self.assertEqual(response['Location'], reverse('tender_selection:detail', args=[self.tender.pk]) + '?workspace=1')
        self.assertEqual(TenderComputeJob.objects.count(), 1)

    def test_service_requires_admin(self):
        viewer = get_user_model().objects.create_user('ordinary-viewer')
        with self.assertRaises(ValueError):
            refresh_document_analysis(viewer, self.tender.pk)
        self.assertFalse(TenderComputeJob.objects.exists())

    def test_worker_does_not_run_route_or_accept_data(self):
        job = TenderComputeJob.objects.create(tender=self.tender, version='v2-document-manual', status='preparing_input')
        with patch('tenders.calculation_v2_pipeline.GatewayDocumentEnricher') as gateway, patch('tenders.calculation_v2_pipeline.enrich_suspicious_tender', return_value={'created': [], 'state': 'no_data', 'diagnostics': []}), patch('tenders.calculation_v2_pipeline.route_tender_batch') as route:
            gateway.return_value.cost_rub = 0
            _run_document_analysis(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, 'ready')
        self.assertIsNotNone(job.completed_at)
        route.assert_not_called()
        self.assertEqual(gateway.call_args.kwargs['max_cost_rub'], Decimal('10'))
