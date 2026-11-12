"""Read-only projection of the existing tender product graph."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .calculation_v2_pipeline import active_calculation_items, source_route_snapshots
from .document_enrichment import effective_item_requirements
from .models import ProviderCalculatorBinding, TenderCommercialItem, TenderComputeWorkUnit


def tender_working_items(tender):
    """Return stored working items and uncovered sources; never prepare or calculate."""
    sources = list(active_calculation_items(tender))
    snapshots = source_route_snapshots(tender, sources=sources)
    bindings = {}

    def source_data(source):
        return {'id': source.pk, 'name': source.original_text,
                'quantity': str(source.quantity) if source.quantity is not None else None, 'unit': source.unit,
                'requirements': deepcopy(effective_item_requirements(source)),
                'provenance': deepcopy(source.provenance),
                'evidence_links': deepcopy((source.metadata or {}).get('evidence_links', {}))}

    def calculations(lines, component=None):
        output = []
        for line in lines:
            result = line.result or {}
            if not result:
                continue
            snapshot = line.input_snapshot or {}
            route = snapshots.get(line.source_item_id, {})
            freshness = 'unknown'
            if component and all(key in snapshot for key in ('quantity', 'requirements')):
                try:
                    same_quantity = Decimal(str(snapshot['quantity'])) == component.effective_quantity
                except (InvalidOperation, TypeError, ValueError):
                    same_quantity = False
                if not same_quantity or snapshot['requirements'] != component.requirements:
                    freshness = 'stale'
            fingerprint = snapshot.get('input_fingerprint')
            if freshness != 'stale' and fingerprint:
                plan = next((p for p in component.route_plans.all() if p.pk == route.get('plan_id')), None) if component else None
                if route.get('state') == 'stale' or (plan and plan.metadata.get('input_fingerprint') != fingerprint):
                    freshness = 'stale'
                elif plan and route.get('state') == 'ready' and all(k in snapshot for k in ('quantity', 'requirements')):
                    freshness = 'current'
            binding_id = result.get('provider_binding_id')
            if binding_id not in bindings:
                bindings[binding_id] = ProviderCalculatorBinding.objects.select_related('link__counterparty', 'knowledge_version').filter(pk=binding_id).first() if type(binding_id) is int else None
            binding = bindings[binding_id]
            selection = None
            if binding and binding.knowledge_version_id and binding.knowledge_version.status == 'confirmed':
                selection = {'calculator_id': binding.pk, 'name': binding.name,
                             'counterparty_id': binding.link.counterparty_id,
                             'counterparty': binding.link.counterparty.name,
                             'process_id': binding.link.stage_id,
                             'available': binding.is_active and binding.link.is_active and binding.link.counterparty.is_active}
            if 'provider_quote' in result and freshness != 'stale':
                # The stored embedded quote has no versioned FK to its pricing inputs.
                freshness = 'unknown'
            output.append({'id': line.pk, 'job_id': line.job_id, 'status': line.status,
                           'freshness': freshness, 'usable': line.status == 'ready' and freshness == 'current',
                           'selection': selection, 'result': deepcopy(result)})
        return output

    def route_data(plan):
        route = snapshots.get(plan.component.source_item_id, {}) if plan.component_id else {}
        state = route.get('state') if route.get('plan_id') == plan.pk else 'unknown'
        return {'id': plan.pk, 'scope': plan.scope, 'status': plan.status, 'freshness': state,
                'steps': [{'id': step.pk, 'process_id': step.process_id, 'name': step.process.name,
                           'position': step.position, 'status': step.status, 'details': deepcopy(step.details)}
                          for step in plan.steps.all()]}

    items, covered = [], set()
    commercials = TenderCommercialItem.objects.filter(tender=tender, status='active').prefetch_related(
        'source_items', 'components__source_item', 'components__route_plans__steps__process',
        'components__compute_lines__job', 'route_plans__steps__process', 'compute_lines__job')
    for item in commercials.order_by('pk'):
        linked = list(item.source_items.all())
        covered.update(source.pk for source in linked)
        components = [c for c in item.components.all() if c.status == 'active']
        nodes = {c.pk: {'id': c.pk, 'name': c.name, 'quantity_per_parent': str(c.quantity_per_parent),
                        'quantity': str(c.effective_quantity), 'unit': c.unit,
                        'requirements': deepcopy(c.requirements), 'requirements_freshness': 'unknown', 'provenance': deepcopy(c.provenance),
                        'source_item_id': c.source_item_id, 'parent_id': c.parent_id,
                        'routes': [route_data(p) for p in c.route_plans.all() if p.scope == 'component'],
                        'calculations': calculations(sorted((line for line in c.compute_lines.all() if line.job.tender_id == tender.pk), key=lambda line: line.pk, reverse=True), c),
                        'components': []} for c in components}
        roots = []
        for c in components:
            ancestors, parent = {c.pk}, c.parent_id
            while parent in nodes and parent not in ancestors:
                ancestors.add(parent)
                parent = nodes[parent]['parent_id']
            if c.parent_id in nodes and parent not in ancestors:
                nodes[c.parent_id]['components'].append(nodes[c.pk])
            else:
                roots.append(nodes[c.pk])
        results = [node['calculations'][0] for node in nodes.values() if node['calculations']]
        items.append({'id': item.pk, 'kind': 'working', 'name': item.display_name,
                      'quantity': str(item.quantity) if item.quantity is not None else None, 'unit': item.unit,
                      'requirements': deepcopy(item.requirements), 'requirements_freshness': 'unknown', 'provenance': deepcopy(item.provenance),
                      'sources': [source_data(s) for s in linked], 'components': roots,
                      'shared_routes': [route_data(p) for p in item.route_plans.all() if p.scope == 'shared'],
                      'calculations': calculations(sorted((line for line in item.compute_lines.all() if line.component_id is None and line.job.tender_id == tender.pk), key=lambda line: line.pk, reverse=True)),
                      'state': 'stale' if any(r['freshness'] == 'stale' for r in results) else 'ready' if len(results) == len(nodes) and results and all(r['usable'] for r in results) else 'needs_review' if results else 'not_calculated'})
    for source in sources:
        if source.pk not in covered:
            items.append({'id': None, 'kind': 'source', 'name': source.original_text,
                          'quantity': str(source.quantity) if source.quantity is not None else None, 'unit': source.unit,
                          'requirements': deepcopy(effective_item_requirements(source)), 'provenance': deepcopy(source.provenance),
                          'sources': [source_data(source)], 'components': [], 'shared_routes': [],
                          'calculations': [], 'state': 'not_prepared'})
    units = TenderComputeWorkUnit.objects.filter(job__tender=tender).select_related('operation_step__route_plan').prefetch_related('operation_steps__route_plan', 'lines').order_by('-pk')
    for item in items:
        item['work_results'] = [
            {'id': unit.pk, 'engine_key': unit.engine_key, 'status': unit.status,
             'component_id': unit.component_id, 'operation_step_ids': sorted({*(step.pk for step in unit.operation_steps.all()), *([unit.operation_step_id] if unit.operation_step_id else [])}),
             'freshness': 'unknown', 'usable': False, 'input_fingerprint': unit.input_fingerprint, 'result': deepcopy(unit.result)}
            for unit in units if unit.result and item['id'] is not None and
            ((unit.operation_step_id and unit.operation_step.route_plan.commercial_item_id == item['id']) or
             any(step.route_plan.commercial_item_id == item['id'] for step in unit.operation_steps.all()) or
             any(line.commercial_item_id == item['id'] for line in unit.lines.all()) or
             (unit.component_id and any(c['id'] == unit.component_id for c in _flatten(item['components']))))]
        if item['state'] == 'not_calculated' and (item['work_results'] or item['calculations']):
            item['state'] = 'needs_review'
    return {'tender_id': tender.pk, 'items': items}


def _flatten(components):
    for component in components:
        yield component
        yield from _flatten(component['components'])
