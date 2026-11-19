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
    jobs = TenderComputeJob.objects.filter(version__startswith='v2').order_by('pk')
    history = defaultdict(list)
    for row in jobs.values('pk', 'tender_id', 'tender__purchase_number', 'tender__status', 'status', 'created_at', 'trigger',
            'diagnostics__stages__source_binding__state', 'diagnostics__stages__source_binding__warning',
            'diagnostics__stages__routing__state'):
        history[row['tender_id']].append(row)
    from tenders.models import ComponentRoutePlan, TenderEstimate
    from tenders.calculation_v2_pipeline import source_route_snapshots
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
        tender = Tender.objects.get(pk=tender_id)
        routes = source_route_snapshots(tender)
        plans = list(ComponentRoutePlan.objects.filter(commercial_item__tender=tender).values(
            'pk', 'status', 'component__source_item_id', 'component__status', 'commercial_item__status'))
        estimate = TenderEstimate.objects.filter(tender=tender).order_by('-updated_at').first()
        legacy_routes = []
        if estimate:
            for line in estimate.lines.all():
                requirements = line.requirements or {}
                production = requirements.get('production') or {}
                route = production.get('route') or {}
                if isinstance(route, dict) and (route.get('processes') or route.get('steps')):
                    legacy_routes.append({'line_id': line.pk, 'source_id': requirements.get('source_item_id'),
                        'commercial_item_id': line.commercial_item_id, 'component_id': line.component_id,
                        'stage': production.get('stage'), 'steps': len(route.get('processes') or route.get('steps') or [])})
        reports.append({'number': latest['tender__purchase_number'], 'archived': latest['tender__status'] == Tender.DISMISSED, 'job_count': len(rows),
            'latest': point(latest), 'first_binding_failure': point(binding_failures[0]) if binding_failures else None,
            'last_routed': point(routed[-1]) if routed else None,
            'job_status_counts': dict(Counter(row['status'] for row in rows)),
            'gaps': diagnostics.get('analysis_gaps'),
            'reading': {key: reading.get(key) for key in ('read', 'total', 'complete')},
            'saved_binding_results': summarize_binding_results(diagnostics),
            'current_routes': [{'source_id': key, 'state': row['state'], 'plan_id': row['plan_id'], 'steps': len(row['steps'])} for key,row in routes.items()],
            'stored_plans': plans, 'legacy_routes': legacy_routes})
    return reports
