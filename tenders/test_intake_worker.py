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

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_manual_update_uses_durable_worker_when_enabled(self):
        from django.contrib.auth import get_user_model
        from tender_selection.services import refresh_document_analysis
        owner = get_user_model().objects.create_superuser('worker-owner', password='test')
        with patch('threading.Thread') as thread, self.captureOnCommitCallbacks(execute=True):
            result = refresh_document_analysis(owner, self.tender.pk)
        thread.assert_not_called()
        self.assertEqual(TenderComputeJob.objects.get(pk=result['job_id']).status, 'queued')
