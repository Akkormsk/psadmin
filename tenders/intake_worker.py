"""Budgeted consumer for the existing durable intake jobs; no separate analyser."""
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from tender_selection.models import FilterSettings
from .models import TenderComputeJob
from .calculation_v2 import claim_next_job

ACTIVE = {'running', 'preparing_input', 'routing', 'preparing'}


def _held_cost(rows):
    total = Decimal('0')
    for row in rows:
        actual = Decimal(str(row['total_cost'] or 0))
        reserved = Decimal(str(row.get('diagnostics__auto_reservation_rub') or 0))
        total += max(actual, reserved) if row['status'] in ACTIVE or row.get('diagnostics__billing_uncertain') else actual
    return total


def _spending(queryset):
    return _held_cost(queryset.values('total_cost', 'status', 'diagnostics__auto_reservation_rub', 'diagnostics__billing_uncertain'))


def _flow_jobs(root):
    return TenderComputeJob.objects.filter(Q(pk=root) | Q(diagnostics__refresh_root_id=root))


@transaction.atomic
def claim_budgeted_job(daily_budget):
    daily_budget = Decimal(str(daily_budget))
    if not daily_budget.is_finite() or daily_budget <= 0:
        return None
    FilterSettings.load()
    FilterSettings.objects.select_for_update().get(pk=1)
    job = TenderComputeJob.objects.select_for_update(skip_locked=True).filter(status='queued').order_by('created_at','pk').first()
    if not job:
        return None
    root = job.diagnostics.get('refresh_root_id') or job.pk
    flow_limit = Decimal(str(job.diagnostics.get('refresh_flow_limit_rub', '30')))
    available_flow = flow_limit - _spending(_flow_jobs(root))
    cap = min(Decimal('10'), available_flow)
    if cap <= 0:
        job.status, job.error = 'partial', {'class': 'BudgetLimit', 'message': 'Исчерпан лимит обновления; полученные данные сохранены.', 'retryable': False}
        job.completed_at = timezone.now()
        job.save(update_fields=['status','error','completed_at','updated_at'])
        return None
    today = str(timezone.localdate())
    available_day = daily_budget - _spending(TenderComputeJob.objects.filter(diagnostics__auto_budget_date=today))
    if available_day < cap:
        return None
    claimed = claim_next_job()
    if not claimed or claimed.pk != job.pk:
        raise RuntimeError('Intake claim changed under the budget lock')
    claimed.diagnostics.update(refresh_root_id=root, refresh_flow_limit_rub=str(flow_limit),
        auto_budget_date=today, auto_reservation_rub=str(cap), max_total_cost_rub=str(cap),
        refresh_started_at=claimed.diagnostics.get('refresh_started_at') or claimed.created_at.isoformat())
    claimed.save(update_fields=['diagnostics','updated_at'])
    return claimed


def _coverage(job):
    covered = set()
    for name, stage in job.diagnostics.get('stages', {}).items():
        phase = name.split(':', 1)[0]
        if phase not in {'structure','national_regime','tz'} or stage.get('outcome') == 'source_only' or stage.get('state') not in {'complete','pending_validation'}:
            continue
        field, kind = ('source_id', int) if phase == 'structure' else ('key', str)
        rows = stage.get('result')
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and type(row.get(field)) is kind:
                covered.add((phase, row[field]))
    return covered


@transaction.atomic
def queue_continuation(job, *, interrupted=False):
    if job.attempt_count >= 3:
        return None
    stages = list(job.diagnostics.get('stages', {}).values()) + [job.diagnostics.get('routing', {})]
    budget_limited = any(stage.get('provider_call_happened') is False and stage.get('reserved_cost_rub') is not None for stage in stages)
    parent_id = job.diagnostics.get('continuation_of')
    parent = TenderComputeJob.objects.filter(pk=parent_id).first() if parent_id else None
    if not interrupted and (not budget_limited or not _coverage(job) - (_coverage(parent) if parent else set())):
        return None
    from tender_selection.models import Tender
    tender = Tender.objects.select_for_update().get(pk=job.tender_id)
    if job.trigger != 'manual_document_refresh' and tender.status == Tender.DISMISSED:
        return None
    if TenderComputeJob.objects.filter(tender_id=job.tender_id, status__in=ACTIVE | {'queued'}).exclude(pk=job.pk).exists():
        return None
    root = job.diagnostics.get('refresh_root_id') or job.pk
    allowed = {key: job.diagnostics[key] for key in ('requested_by', 'routes_only','acceptance_run','refresh_flow_limit_rub','refresh_started_at') if key in job.diagnostics}
    return TenderComputeJob.objects.create(tender_id=job.tender_id, version=job.version, trigger=job.trigger,
        attempt_count=job.attempt_count, diagnostics={**allowed, 'intake_refresh': True,
            'refresh_root_id': root, 'continuation_of': job.pk, 'refresh_started_at': allowed.get('refresh_started_at') or job.created_at.isoformat(),
            'refresh': {'stage': 'queued', 'label': 'Продолжаем обновление…'}})


@transaction.atomic
def recover_interrupted_jobs(*, now=None):
    from datetime import timedelta
    from .analysis_presentation import _job_analysis_cost
    now = now or timezone.now()
    count = 0
    for job in TenderComputeJob.objects.select_for_update(skip_locked=True).filter(status__in=ACTIVE, diagnostics__intake_refresh=True, updated_at__lt=now-timedelta(minutes=15)):
        job.diagnostics['billing_uncertain'] = any(stage.get('state') == 'running' for stage in job.diagnostics.get('stages', {}).values())
        job.total_cost = Decimal(str(_job_analysis_cost(job).get('known_rub') or job.total_cost or 0))
        job.status, job.completed_at = 'partial', now
        job.error = {'class': 'WorkerInterrupted', 'message': 'Обновление прервано. Полученные результаты сохранены.', 'retryable': True}
        job.save(update_fields=['status','completed_at','error','total_cost','diagnostics','updated_at'])
        count += bool(queue_continuation(job, interrupted=True))
    return count


def run_budgeted_job(daily_budget):
    if Decimal(str(daily_budget)) <= 0:
        return None
    recover_interrupted_jobs()
    job = claim_budgeted_job(daily_budget)
    if not job:
        return None
    from tender_selection.services import _run_document_analysis
    _run_document_analysis(job.pk)
    job.refresh_from_db()
    queue_continuation(job)
    return job
