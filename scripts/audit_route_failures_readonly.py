"""Summarize saved intake failures without document text, writes, network or model calls."""
import json
from collections import Counter, defaultdict


def summarize_binding_results(diagnostics):
    stages = []
    for name, metrics in (diagnostics.get('stages') or {}).items():
        if not name.startswith('binding:'):
            continue
        rows = metrics.get('result')
        if not isinstance(rows, list):
            continue
        shapes, scopes, purposes = Counter(), Counter(), Counter()
        for row in rows:
            if not isinstance(row, dict):
                shapes[type(row).__name__] += 1
                continue
            shapes[','.join(sorted(row))] += 1
            scopes[str(row.get('scope'))] += 1
            purposes[json.dumps(row.get('purposes'), ensure_ascii=False, sort_keys=True)] += 1
        stages.append({'stage': name, 'state': metrics.get('state'),
            'validation_error': metrics.get('validation_error'), 'reused_from': metrics.get('reused_from'),
            'rows': len(rows), 'shapes': dict(shapes), 'scopes': dict(scopes), 'purposes': dict(purposes)})
    return stages


def audit_saved_route_jobs():
    from tenders.models import TenderComputeJob
    from tender_selection.models import Tender
    jobs = TenderComputeJob.objects.filter(version__startswith='v2').exclude(tender__status=Tender.DISMISSED).order_by('pk')
    history = defaultdict(list)
    for row in jobs.values('pk', 'tender_id', 'tender__purchase_number', 'status', 'created_at', 'trigger',
            'diagnostics__stages__source_binding__state', 'diagnostics__stages__source_binding__warning',
            'diagnostics__stages__routing__state'):
        history[row['tender_id']].append(row)
    reports = []
    for tender_id, rows in history.items():
        latest = rows[-1]
        binding_failures = [row for row in rows if row['diagnostics__stages__source_binding__state'] == 'failed']
        routed = [row for row in rows if row['diagnostics__stages__routing__state'] == 'complete']
        diagnostics = jobs.only('diagnostics').get(pk=latest['pk']).diagnostics or {}
        reading = diagnostics.get('source_reading') or {}
        def point(row):
            return {'job': row['pk'], 'created_at': row['created_at'].isoformat(), 'status': row['status'],
                'binding': row['diagnostics__stages__source_binding__state'],
                'routing': row['diagnostics__stages__routing__state'],
                'warning': row['diagnostics__stages__source_binding__warning']}
        reports.append({'number': latest['tender__purchase_number'], 'job_count': len(rows),
            'latest': point(latest), 'first_binding_failure': point(binding_failures[0]) if binding_failures else None,
            'last_routed': point(routed[-1]) if routed else None,
            'job_status_counts': dict(Counter(row['status'] for row in rows)),
            'gaps': diagnostics.get('analysis_gaps'),
            'reading': {key: reading.get(key) for key in ('read', 'total', 'complete')},
            'saved_binding_results': summarize_binding_results(diagnostics)})
    return reports
