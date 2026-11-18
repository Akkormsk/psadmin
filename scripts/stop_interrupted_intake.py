"""Explicit operator stop of one stale TEST intake; no model calls or continuation."""
import json
import os
from datetime import timedelta
from decimal import Decimal

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()

from django.db import connection, transaction
from django.utils import timezone
from tenders.analysis_presentation import _job_analysis_cost
from tenders.models import TenderComputeJob

number = os.environ['INSPECT_TENDER_NUMBER']
job_id = os.environ['STOP_INTERRUPTED_JOB_ID']
if len(number) != 19 or not number.isascii() or not number.isdigit() or not job_id.isascii() or not job_id.isdigit():
    raise ValueError('Expected purchase number and explicit job ID')
if connection.vendor != 'postgresql':
    raise RuntimeError('Operator stop requires PostgreSQL')

with transaction.atomic():
    job = TenderComputeJob.objects.select_for_update().get(pk=int(job_id), tender__purchase_number=number)
    if not job.version.startswith('v2') or not job.diagnostics.get('intake_refresh'):
        raise ValueError('Selected job is not an intake refresh')
    before = job.status
    if job.status in {'preparing_input', 'running', 'routing', 'preparing'}:
        now = timezone.now()
        timeout = timedelta(seconds=int(os.getenv('CALCULATION_V2_INTAKE_HEARTBEAT_TIMEOUT_SECONDS', '900')))
        if job.updated_at >= now - timeout:
            raise ValueError('Heartbeat is recent; refusing to stop a potentially live process')
        stages = job.diagnostics.get('stages') or {}
        job.diagnostics['billing_uncertain'] = bool(job.diagnostics.get('billing_uncertain')) or any(
            stage.get('state') == 'running' and stage.get('provider_call_happened') is not False
            for stage in stages.values())
        job.total_cost = Decimal(str(_job_analysis_cost(job).get('known_rub') or job.total_cost or 0))
        job.diagnostics['operator_stop'] = {'at': now.isoformat(), 'workflow_run_id': os.environ['STOP_RUN_ID'],
            'reason': 'Owner requested stop of interrupted intake', 'last_heartbeat': job.updated_at.isoformat()}
        job.diagnostics['refresh'] = {**job.diagnostics.get('refresh', {}), 'failed': True}
        job.status, job.completed_at = 'partial', now
        job.error = {'class': 'WorkerInterrupted', 'message': 'Обновление остановлено по запросу владельца. Полученные результаты сохранены.', 'retryable': True}
        job.save(update_fields=['status', 'completed_at', 'error', 'diagnostics', 'total_cost', 'updated_at'])
    job.refresh_from_db()
    print(json.dumps({'number': number, 'job_id': job.pk, 'before': before, 'status': job.status,
        'completed_at': job.completed_at, 'error': job.error, 'operator_stop': job.diagnostics.get('operator_stop'),
        'cost': _job_analysis_cost(job),
        'other_active_jobs': list(TenderComputeJob.objects.filter(tender_id=job.tender_id,
            status__in=['queued', 'preparing_input', 'running', 'routing', 'preparing']).exclude(pk=job.pk).values_list('pk', flat=True))},
        ensure_ascii=False, default=str), flush=True)
