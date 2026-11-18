from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from tender_selection.models import FilterSettings, Tender
from .models import TenderComputeJob
from .intake_worker import claim_budgeted_job, queue_continuation, recover_interrupted_jobs


class IntakeWorkerTests(TestCase):
    def setUp(self):
        FilterSettings.load()
        self.tender = Tender.objects.create(purchase_number='worker-source', title='Работа')

    def job(self, **kwargs):
        return TenderComputeJob.objects.create(tender=self.tender, diagnostics={'intake_refresh': True}, **kwargs)

    def test_zero_budget_does_not_claim_or_spend(self):
        job = self.job()
        self.assertIsNone(claim_budgeted_job(0))
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.attempt_count, 0)

    def test_intake_consumer_leaves_other_queued_calculation_jobs_untouched(self):
        other = TenderComputeJob.objects.create(tender=self.tender)
        intake = self.job()
        self.assertEqual(claim_budgeted_job(10).pk, intake.pk)
        other.refresh_from_db()
        self.assertEqual(other.status, 'queued')
        self.assertEqual(other.attempt_count, 0)

    def test_active_reservations_prevent_daily_budget_race(self):
        first = self.job()
        first = claim_budgeted_job(Decimal('15'))
        self.assertEqual(first.diagnostics['auto_reservation_rub'], '10')
        other = self.job()
        self.assertIsNone(claim_budgeted_job(Decimal('15')))
        first.status, first.total_cost = 'ready', Decimal('3')
        first.save()
        self.assertEqual(claim_budgeted_job(Decimal('15')).pk, other.pk)

    def test_budget_continuation_preserves_costs_and_requires_new_progress(self):
        job = self.job(status='needs_review', attempt_count=1, total_cost=Decimal('6'))
        job.diagnostics['stages'] = {'structure:1': {'state': 'pending_validation', 'result': [{'source_id': 1}]},
            'structure:2': {'state': 'failed', 'provider_call_happened': False, 'reserved_cost_rub': '3'}}
        job.save()
        continuation = queue_continuation(job)
        self.assertNotEqual(continuation.pk, job.pk)
        self.assertEqual(continuation.diagnostics['continuation_of'], job.pk)
        self.assertEqual(continuation.total_cost, 0)
        from .analysis_presentation import analysis_cost
        self.assertEqual(Decimal(analysis_cost(continuation)['known_rub']), Decimal('6'))
        self.assertEqual(continuation.diagnostics['refresh_started_at'], job.created_at.isoformat())
        job.refresh_from_db()
        self.assertEqual(job.total_cost, Decimal('6'))
        continuation.status = 'needs_review'
        continuation.diagnostics['stages'] = job.diagnostics['stages']
        continuation.save()
        self.assertIsNone(queue_continuation(continuation))

    def test_heartbeat_protects_a_long_running_job_and_recovery_keeps_history(self):
        job = self.job(status='running', attempt_count=1, total_cost=Decimal('2'))
        job.started_at = timezone.now()-timedelta(minutes=40)
        job.save()
        self.assertEqual(recover_interrupted_jobs(), 0)
        TenderComputeJob.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(minutes=16))
        self.assertEqual(recover_interrupted_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'partial')
        self.assertEqual(job.total_cost, Decimal('2'))
        self.assertEqual(TenderComputeJob.objects.filter(diagnostics__continuation_of=job.pk, status='queued').count(), 1)

    def test_truncated_response_continues_only_when_closed_records_made_progress(self):
        job = self.job(status='needs_review', attempt_count=1)
        job.diagnostics['stages'] = {'tz:1': {'state': 'failed', 'incomplete_response': {
            'finish_reason': 'length', 'content': '{"items":[{"key":"1:0","characteristics":[]}, {"key":'}}}
        job.save()
        child = queue_continuation(job)
        self.assertIsNotNone(child)
        child.diagnostics['stages'] = job.diagnostics['stages']
        child.status = 'needs_review'
        child.save()
        self.assertIsNone(queue_continuation(child))

    def test_interrupted_manual_refresh_keeps_evidence_without_automatic_continuation(self):
        job = self.job(status='preparing_input', trigger='manual_document_refresh')
        job.diagnostics.update(stages={'tz': {'state': 'running'},
            'structure': {'state': 'complete', 'result': [{'source_id': 1}]}},
            source_reading={'complete': True, 'read': 392})
        job.save()
        TenderComputeJob.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(minutes=16))
        with patch('tenders.services._ai_gateway_json') as gateway:
            self.assertEqual(recover_interrupted_jobs(), 0)
            self.assertEqual(recover_interrupted_jobs(), 0)
        gateway.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'partial')
        self.assertEqual(job.error['class'], 'WorkerInterrupted')
        self.assertIsNotNone(job.completed_at)
        self.assertTrue(job.diagnostics['billing_uncertain'])
        self.assertEqual(job.diagnostics['source_reading']['read'], 392)
        self.assertEqual(job.diagnostics['stages']['structure']['result'], [{'source_id': 1}])
        self.assertEqual(TenderComputeJob.objects.count(), 1)

    def test_old_queued_job_is_waiting_not_interrupted(self):
        from .intake_worker import interrupted_job_snapshot
        job = self.job()
        TenderComputeJob.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(hours=1))
        job.refresh_from_db()
        self.assertIs(interrupted_job_snapshot(job), job)

    @override_settings(CALCULATION_V2_ENABLED=False)
    def test_explicit_retry_records_interruption_and_starts_only_one_new_job(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import refresh_document_analysis
        owner = get_user_model().objects.create_superuser('retry-owner', password='test')
        job = self.job(status='preparing_input', trigger='manual_document_refresh')
        job.diagnostics['stages'] = {'tz': {'state': 'running'}}
        job.save()
        TenderComputeJob.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(minutes=16))
        with patch('threading.Thread') as thread, self.captureOnCommitCallbacks(execute=True):
            first = refresh_document_analysis(owner, self.tender.pk)
            duplicate = refresh_document_analysis(owner, self.tender.pk)
        self.assertTrue(first['started'])
        self.assertFalse(duplicate['started'])
        thread.return_value.start.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, 'partial')
        self.assertEqual(job.error['class'], 'WorkerInterrupted')
        self.assertTrue(job.diagnostics['billing_uncertain'])
        self.assertEqual(TenderComputeJob.objects.count(), 2)

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_manual_update_uses_durable_worker_when_enabled(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import refresh_document_analysis
        owner = get_user_model().objects.create_superuser('worker-owner', password='test')
        with patch('threading.Thread') as thread, self.captureOnCommitCallbacks(execute=True):
            result = refresh_document_analysis(owner, self.tender.pk)
        thread.assert_not_called()
        self.assertEqual(TenderComputeJob.objects.get(pk=result['job_id']).status, 'queued')
