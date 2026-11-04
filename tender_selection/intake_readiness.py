"""Read-only, evidence-based acceptance of current incoming tenders."""
from django.conf import settings
from django.urls import reverse

from tenders.calculation_v2_pipeline import source_route_snapshots
from tenders.models import TenderComputeJob
from .profile_triage import profile_disposition

PHASES = ('notification', 'documents', 'structure', 'national_regime', 'tz', 'routing')
ACTIVE = {'queued', 'running', 'preparing_input', 'routing', 'preparing'}


def summarize(tenders):
    tenders = list(tenders)
    latest = {}
    for job in TenderComputeJob.objects.filter(tender_id__in=[tender.pk for tender in tenders]).order_by('tender_id', '-pk'):
        latest.setdefault(job.tender_id, job)
    rows = []
    for tender in tenders:
        job = latest.get(tender.pk)
        stages = job.diagnostics.get('stages', {}) if job else {}
        routes = source_route_snapshots(tender)
        ready = sum(route['state'] == 'ready' and bool(route['steps']) for route in routes.values())
        missing = [name for name in PHASES if stages.get(name, {}).get('state') != 'complete' or stages.get(name, {}).get('outcome') == 'source_only']
        profile = profile_disposition(tender)
        complete = bool(job and job.status not in ACTIVE and not job.error and not missing and profile == 'fit' and routes and ready == len(routes))
        if complete:
            reason = ''
        elif job and job.status in ACTIVE:
            reason = 'Обрабатывается'
        elif not job:
            reason = 'Ещё не разобран'
        elif job.error:
            reason = str(job.error.get('message') or 'Этап анализа не завершён')
        elif missing:
            reason = 'Не все этапы анализа завершены'
        elif profile != 'fit':
            reason = 'Профиль закупки не подтверждён'
        else:
            reason = f'Нет готового маршрута у {len(routes) - ready} позиций' if routes else 'Не получены товарные позиции'
        rows.append({'tender_id': tender.pk, 'number': tender.purchase_number,
            'url': reverse('tender_selection:detail', args=[tender.pk]), 'complete': complete,
            'positions': len(routes), 'routes_ready': ready, 'reason': reason, 'profile': profile})
    return {'total': len(rows), 'complete': sum(row['complete'] for row in rows),
        'positions': sum(row['positions'] for row in rows), 'unresolved_lists': sum(row['positions'] == 0 for row in rows), 'routes_ready': sum(row['routes_ready'] for row in rows),
        'all_complete': bool(rows) and all(row['complete'] for row in rows),
        'background_enabled': bool(settings.CALCULATION_V2_ENABLED),
        'pending': [row for row in rows if not row['complete']]}
