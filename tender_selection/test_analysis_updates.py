from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from .models import Tender, FilterSettings
from tenders.models import TenderComputeJob, TenderSourceItem


class AnalysisUpdatesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('live-analysis', password='test')
        self.client.force_login(self.user)
        FilterSettings.load()
        self.tender = Tender.objects.create(purchase_number='analysis-live', title='Проверка', notification_raw={'source': {}}, extras_checked_at=timezone.now())
        self.job = TenderComputeJob.objects.create(tender=self.tender, version='v2-document-manual', status='preparing_input')
        self.url = reverse('tender_selection:analysis_status') + '?ids=' + str(self.tender.pk)

    def test_status_is_read_only_and_reports_running_then_failure_reason(self):
        with CaptureQueriesContext(connection) as queries, patch('tenders.services._ai_gateway_json') as gateway:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['statuses'][0]['processing'])
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        gateway.assert_not_called()
        self.job.status = 'partial'
        self.job.diagnostics = {'enrichment_state': 'validation_failure', 'enrichment': [{'outcome': 'validation_failure', 'final_failure_reason': 'Характеристика: не указан исходный факт'}]}
        self.job.save()
        status = self.client.get(self.url).json()['statuses'][0]
        self.assertFalse(status['processing'])
        self.assertEqual(status['code'], 'ANALYSIS_INCOMPLETE')
        self.assertIn('не указан исходный факт', status['reason'])
        self.assertNotEqual(status['revision'], response.json()['statuses'][0]['revision'])

    def test_proposals_ready_are_not_reported_as_no_changes(self):
        self.job.status = 'needs_review'; self.job.save()
        TenderSourceItem.objects.create(tender=self.tender, source_key='pending', source_type='document_proposal', original_text='Предложение', is_active=False)
        status = self.client.get(self.url).json()['statuses'][0]
        self.assertEqual(status['code'], 'NEEDS_OWNER_INPUT')
        self.assertEqual(status['pending_count'], 1)
        self.assertIn('проверить правки', status['label'])

    def test_result_fragment_does_not_fetch_documents_or_mark_opened(self):
        with CaptureQueriesContext(connection) as queries, patch('tender_selection.views.notification_for') as notification, patch('tenders.services._ai_gateway_json') as gateway:
            response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]) + '?analysis_snapshot=1&workspace=1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-analysis-result=')
        self.assertNotContains(response, '<html')
        notification.assert_not_called(); gateway.assert_not_called()
        self.assertFalse(any(query['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for query in queries))
        self.tender.refresh_from_db(); self.assertIsNone(self.tender.opened_at)

    def test_status_permissions_and_invalid_inputs(self):
        self.assertEqual(self.client.post(reverse('tender_selection:analysis_status')).status_code, 405)
        self.assertEqual(self.client.get(reverse('tender_selection:analysis_status') + '?ids=oops').status_code, 400)
        self.user.is_superuser = False; self.user.save(update_fields=['is_superuser'])
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_snapshot_retries_when_completion_changes_during_read(self):
        from tenders.analysis_presentation import resolve_tender_analysis_status
        calls = []
        def resolve(tender):
            calls.append(1)
            if len(calls) == 2:
                self.job.status = 'ready'; self.job.save()
            return resolve_tender_analysis_status(tender)
        with patch('tenders.analysis_presentation.resolve_tender_analysis_status', side_effect=resolve):
            response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]) + '?analysis_snapshot=1')
        self.assertEqual(response.status_code, 409)

    def test_saved_reference_failure_has_human_explanation(self):
        self.job.status = 'partial'
        self.job.diagnostics = {'enrichment_state': 'validation_failure', 'enrichment': [{'final_failure_reason': 'Решение не соответствует ссылкам на исходные факты'}]}
        self.job.save()
        status = self.client.get(self.url).json()['statuses'][0]
        self.assertIn('смешаны новые и исходные характеристики', status['reason'])
