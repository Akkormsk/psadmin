"""SELECT-only projection. Results are stored once and referenced by their owners."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from tender_selection.notification import parse_notification
from .calculation_v2_pipeline import active_calculation_items, source_route_snapshots
from .document_enrichment import effective_item_requirements, notification_origin
from .models import ProviderCalculatorBinding, TenderCommercialItem, TenderComputeLine, TenderComputeWorkUnit


def _buckets():
    return {key: [] for key in ('current', 'historical', 'stale', 'unverified', 'pending')}


def tender_working_items(tender):
    """Read existing objects; never calculate prices, select providers or create data."""
    sources = list(active_calculation_items(tender))
    snapshots = source_route_snapshots(tender, sources=sources)
    notice = parse_notification(tender.notification_raw or {}).get('items', [])
    items, covered, results, binding_cache = [], set(), {}, {}
    unattached = []
    commercials = list(TenderCommercialItem.objects.filter(tender=tender, status='active').prefetch_related(
        'source_items__parent', 'components__source_item', 'route_plans__steps__process').order_by('pk'))
    component_nodes, item_nodes, step_nodes, plan_nodes = {}, {}, {}, {}
    stored_plans = {p.pk: p for item in commercials for p in item.route_plans.all()}
    source_nodes = {}

    def source_data(source):
        return {'id': source.pk, 'name': source.original_text,
                'quantity': str(source.quantity) if source.quantity is not None else None, 'unit': source.unit,
                'requirements': deepcopy(effective_item_requirements(source)), 'provenance': deepcopy(source.provenance),
                'evidence_links': deepcopy((source.metadata or {}).get('evidence_links', {}))}

    def notice_prices(linked):
        prices, seen = [], set()
        for source in linked:
            origin = notification_origin(source, same_item=True)
            index = (origin.metadata or {}).get('notification_index') if origin else None
            if type(index) is int and 0 <= index < len(notice) and index not in seen:
                seen.add(index)
                row = notice[index]
                prices.append({'source_item_id': origin.pk, 'position': index + 1,
                               'unit_price': row.get('price'), 'total': row.get('sum')})
        return prices

    for item in commercials:
        linked = list(item.source_items.all())
        covered.update(s.pk for s in linked)
        node = {'id': item.pk, 'kind': 'working', 'name': item.display_name, 'structure': item.structure,
                'quantity': str(item.quantity) if item.quantity is not None else None, 'unit': item.unit,
                'notice_prices': notice_prices(linked), 'requirements': deepcopy(item.requirements),
                'requirements_freshness': 'unknown', 'provenance': deepcopy(item.provenance),
                'sources': [source_data(s) for s in linked], 'components': [], 'historical_components': [], 'shared_routes': [],
                'calculations': _buckets(), 'issues': []}
        item_nodes[item.pk] = node
        components = list(item.components.all())
        local = {}
        for component in components:
            local[component.pk] = {'id': component.pk, 'name': component.name, 'parent_id': component.parent_id,
                'source_item_id': component.source_item_id, 'status': component.status, 'historical': component.status != 'active', 'quantity_per_parent': str(component.quantity_per_parent),
                # Existing effective_quantity uses commercial quantity, even for nested components.
                'quantity': str(component.effective_quantity) if component.parent_id is None else None,
                'stored_effective_quantity': str(component.effective_quantity), 'unit': component.unit,
                'requirements': deepcopy(component.requirements), 'requirements_freshness': 'unknown',
                'provenance': deepcopy(component.provenance), 'routes': [], 'components': [],
                'calculations': _buckets(), 'issues': [], 'state': 'historical' if component.status != 'active' else 'not_calculated'}
            component_nodes[component.pk] = (component, local[component.pk], node)
        for component in components:
            child = local[component.pk]
            ancestors, parent = {component.pk}, component.parent_id
            while parent in local and parent not in ancestors:
                ancestors.add(parent)
                parent = local[parent]['parent_id']
            if component.parent_id in local and parent not in ancestors and local[component.parent_id]['historical'] == child['historical']:
                local[component.parent_id]['components'].append(child)
            else:
                node['historical_components' if child['historical'] else 'components'].append(child)
                if component.parent_id:
                    child['issues'].append('invalid_parent')
            if component.parent_id:
                child['issues'].append('nested_quantity_not_supported_by_existing_calculator')
        for plan in item.route_plans.all():
            if plan.scope == 'shared':
                owner = node['shared_routes']
                route = {}
            elif plan.component_id in local:
                owner = local[plan.component_id]['routes']
                route = snapshots.get(component_nodes[plan.component_id][0].source_item_id, {})
            else:
                node['issues'].append('route_component_not_active')
                continue
            current_plan = route.get('plan_id') == plan.pk
            comparable = stored_plans.get(route.get('plan_id'))
            comparable = comparable is not None and comparable.component_id == plan.component_id
            projected = {'id': plan.pk, 'scope': plan.scope, 'status': plan.status,
                         'freshness': route.get('state') if current_plan else 'unknown',
                         'historical': plan.status in {'cancelled', 'superseded', 'inactive'} or bool(comparable and not current_plan) or bool(plan.component_id and local.get(plan.component_id, {}).get('historical', False)),
                         'steps': []}
            plan_nodes[plan.pk] = projected
            for step in plan.steps.all():
                step_node = {'id': step.pk, 'route_id': plan.pk, 'process_id': step.process_id, 'name': step.process.name,
                             'position': step.position, 'status': step.status, 'details': deepcopy(step.details),
                             'calculations': _buckets()}
                projected['steps'].append(step_node)
                step_nodes[step.pk] = step_node
            owner.append(projected)
        items.append(node)

    for source in sources:
        if source.pk not in covered:
            node = {'id': None, 'kind': 'source', 'name': source.original_text,
                'quantity': str(source.quantity) if source.quantity is not None else None, 'unit': source.unit,
                'notice_prices': notice_prices([source]), 'requirements': deepcopy(effective_item_requirements(source)),
                'provenance': deepcopy(source.provenance), 'sources': [source_data(source)],
                'components': [], 'historical_components': [], 'shared_routes': [], 'calculations': _buckets(), 'state': 'not_prepared'}
            source_nodes[source.pk] = node
            items.append(node)

    def selection(binding_id):
        if type(binding_id) is not int:
            return None
        if binding_id not in binding_cache:
            binding_cache[binding_id] = ProviderCalculatorBinding.objects.select_related('link__counterparty', 'knowledge_version').filter(pk=binding_id).first()
        binding = binding_cache[binding_id]
        if not binding or not binding.knowledge_version_id or binding.knowledge_version.status != 'confirmed':
            return None
        return {'calculator_id': binding.pk, 'name': binding.name, 'counterparty_id': binding.link.counterparty_id,
                'counterparty': binding.link.counterparty.name, 'process_id': binding.link.stage_id,
                'available': binding.is_active and binding.link.is_active and binding.link.counterparty.is_active}

    def attach(ref, record, owners, historical):
        bucket = ('historical' if historical else 'pending' if not record['result'] else
                  'current' if record['usable'] else 'stale' if record['freshness'] == 'stale' else 'unverified')
        record['historical'] = historical
        if historical:
            record['usable'] = False
        results[ref] = record
        for owner in owners:
            if ref not in owner['calculations'][bucket]:
                owner['calculations'][bucket].append(ref)

    def retain_unattached(ref, obj, kind):
        results[ref] = {'id': obj.pk, 'kind': kind, 'job_id': obj.job_id, 'status': obj.status,
                        'freshness': 'unknown', 'usable': False, 'historical': False,
                        'association': 'not_in_active_tree', 'component_id': obj.component_id,
                        'result': deepcopy(obj.result)}
        unattached.append(ref)

    seen = set()
    lines = TenderComputeLine.objects.filter(job__tender=tender).select_related('job').order_by('-job_id', '-pk')
    for line in lines:
        if line.component_id in component_nodes:
            component, owner, item_node = component_nodes[line.component_id]
            if line.commercial_item_id not in (None, item_node['id']):
                retain_unattached(f'line:{line.pk}', line, 'line')
                continue
        elif line.component_id is None and line.commercial_item_id in item_nodes:
            component, owner = None, item_nodes[line.commercial_item_id]
        elif line.component_id is None and line.commercial_item_id is None and line.source_item_id in source_nodes:
            component, owner = None, source_nodes[line.source_item_id]
        else:
            retain_unattached(f'line:{line.pk}', line, 'line')
            continue
        key = ('component', line.component_id) if component else ('item', line.commercial_item_id) if line.commercial_item_id else ('source', line.source_item_id)
        historical = key in seen or bool(component and component.status != 'active')
        seen.add(key)
        data, snapshot = line.result or {}, line.input_snapshot or {}
        route = snapshots.get(line.source_item_id, {})
        freshness = 'unknown'
        if component and all(k in snapshot for k in ('quantity', 'requirements')):
            try:
                quantity_matches = Decimal(str(snapshot['quantity'])) == component.effective_quantity
            except (InvalidOperation, TypeError, ValueError):
                quantity_matches = False
            if not quantity_matches or snapshot['requirements'] != component.requirements:
                freshness = 'stale'
            elif snapshot.get('input_fingerprint') and route.get('plan_id'):
                route_plan = stored_plans.get(route['plan_id'])
                comparable = route_plan is not None and route_plan.component_id == component.pk
                if comparable and route.get('state') == 'stale':
                    freshness = 'stale'
                elif comparable and route.get('state') == 'ready':
                    # Compare against the actual selected route fingerprint, not another plan.
                    freshness = 'current' if route_plan and route_plan.metadata.get('input_fingerprint') == snapshot['input_fingerprint'] else 'stale'
        if 'provider_quote' in data and freshness != 'stale':
            freshness = 'unknown'
        if component and component.parent_id and freshness != 'stale':
            freshness = 'unknown'
        attach(f'line:{line.pk}', {'id': line.pk, 'kind': 'line', 'job_id': line.job_id,
            'status': line.status, 'freshness': freshness,
            'usable': bool(data) and line.status == 'ready' and line.job.status not in {'failed', 'cancelled'} and freshness == 'current',
            'selection': selection(data.get('provider_binding_id')), 'result': deepcopy(data)}, [owner], historical)

    seen = set()
    units = TenderComputeWorkUnit.objects.filter(job__tender=tender).prefetch_related('operation_steps', 'lines').order_by('-job_id', '-pk')
    for unit in units:
        step_ids = {step.pk for step in unit.operation_steps.all()}
        if unit.operation_step_id:
            step_ids.add(unit.operation_step_id)
        owners = [step_nodes[pk] for pk in sorted(step_ids) if pk in step_nodes]
        if not step_ids:
            if unit.component_id in component_nodes:
                owners = [component_nodes[unit.component_id][1]]
            else:
                owners = []
                for line in unit.lines.all():
                    target = component_nodes[line.component_id][1] if line.component_id in component_nodes else item_nodes.get(line.commercial_item_id) or source_nodes.get(line.source_item_id)
                    if target is not None and all(target is not old for old in owners):
                        owners.append(target)
        if not owners:
            retain_unattached(f'work_unit:{unit.pk}', unit, 'work_unit')
            continue
        key = (unit.engine_key, unit.dedupe_key, unit.component_id, tuple(sorted(step_ids)))
        historical = key in seen or bool(owners and all(owner.get('route_id') and plan_nodes[owner['route_id']]['historical'] for owner in owners))
        seen.add(key)
        attach(f'work_unit:{unit.pk}', {'id': unit.pk, 'kind': 'work_unit', 'job_id': unit.job_id,
            'status': unit.status, 'freshness': 'unknown', 'usable': False,
            'input_fingerprint': unit.input_fingerprint, 'operation_step_ids': sorted(step_ids),
            'selection': selection((unit.result or {}).get('provider_binding_id')),
            'result': deepcopy(unit.result)}, owners, historical)

    def ready_calculations(node):
        return bool(node['calculations']['current']) and not any(node['calculations'][k] for k in ('pending', 'stale', 'unverified'))

    def assess(node, routes):
        children = node.get('components', [])
        for child in children:
            assess(child, child['routes'])
        active_routes = [r for r in routes if not r['historical']]
        steps = [step for route in active_routes for step in route['steps']]
        for step in steps:
            step['state'] = 'ready' if ready_calculations(step) else 'stale' if step['calculations']['stale'] else 'needs_review' if step['calculations']['unverified'] else 'not_calculated'
        complete = (not node['issues'] and not any(node['calculations'][k] for k in ('pending', 'stale', 'unverified')) and all(c['state'] == 'ready' for c in children) and
                    all(step['state'] == 'ready' for step in steps) and
                    bool(children or steps or ready_calculations(node)))
        if routes and not active_routes:
            complete = False
        states = [child['state'] for child in children] + [step['state'] for step in steps]
        if any(route['status'] == 'needs_review' for route in active_routes):
            complete = False
            states.append('needs_review')
        node['state'] = ('ready' if complete else 'needs_review' if node['issues'] or node['calculations']['unverified'] or 'needs_review' in states else
                         'stale' if node['calculations']['stale'] or 'stale' in states or any(r['freshness'] == 'stale' for r in active_routes) else 'not_calculated')

    for item in items:
        if item['kind'] == 'working':
            assess(item, item['shared_routes'])
    return {'tender_id': tender.pk, 'items': items, 'calculation_results': results, 'unattached_calculations': unattached}
