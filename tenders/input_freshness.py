"""Read saved calculation inputs against accepted goods; never recompute prices."""
from decimal import Decimal, InvalidOperation

from .source_memory import signature

SPECIFICATION_METADATA = frozenset({'characteristics', 'reconciled', 'compiled_reconciliation', 'omitted_facts',
    'field_provenance', 'reconciliation_error', 'additive_sources', 'owner_answers'})

def conditions_fingerprint(conditions):
    return signature({key: sorted(value, key=signature) if isinstance(value, list) else value
                      for key, value in (conditions or {}).items() if value})


def specification(requirements):
    if not isinstance(requirements, dict):
        return None
    if isinstance(requirements.get('source_requirements'), dict):
        requirements = requirements['source_requirements']
    rows = requirements.get('characteristics')
    if not isinstance(rows, list):
        return None
    return {'characteristics': sorted([{'name': row.get('name'), 'value': row.get('value'), 'scope': row.get('scope', 'product'),
                    'conflict': bool(row.get('conflict'))} for row in rows if isinstance(row, dict)], key=signature),
            'other': {key: value for key, value in requirements.items() if key not in SPECIFICATION_METADATA}}


def estimate_input_status(estimate):
    labels = {'current': 'Товары и условия соответствуют расчёту', 'stale': 'Товары или условия изменились — проверьте расчёт',
              'unknown': 'Актуальность требований и условий не подтверждена'}
    rows = []
    if not getattr(estimate, 'tender_id', None) or not estimate.pk:
        return {'state': 'unknown', 'label': labels['unknown'], 'lines': rows}
    from django.db.models import Prefetch
    from .models import TenderSourceItem
    sources = TenderSourceItem.objects.only('pk', 'is_active')
    lines = estimate.lines.select_related('commercial_item', 'component__commercial_item').prefetch_related(
        Prefetch('commercial_item__source_items', queryset=sources), Prefetch('component__commercial_item__source_items', queryset=sources))
    for line in lines:
        component = line.component
        good = component.commercial_item if component else line.commercial_item
        state, reason = 'unknown', 'У строки нет подтверждённой связи с исходным товаром.'
        if good and good.tender_id == estimate.tender_id:
            requirements = line.requirements if isinstance(line.requirements, dict) else {}
            source_id = requirements.get('source_item_id')
            source_ids = {component.source_item_id} if component else {source.pk for source in good.source_items.all() if source.is_active}
            current_spec = component.requirements if component else good.requirements
            old_spec = specification(line.requirements)
            unit = component.unit if component else good.unit
            unit_changed = 'source_unit' in requirements and str(requirements['source_unit'] or '').strip().casefold().rstrip('.') != str(unit or '').strip().casefold().rstrip('.')
            try:
                quantity = good.quantity
                if component:
                    quantity = None if component.parent_id or good.quantity is None else component.effective_quantity
                quantity_changed = quantity is not None and Decimal(str(quantity)) != line.quantity
            except (InvalidOperation, TypeError, ValueError):
                quantity, quantity_changed = None, False
            if good.status != 'active' or component and component.status != 'active' or source_id and source_id not in source_ids:
                state, reason = 'stale', 'Изменилась принятая позиция или состав.'
            elif type(source_id) is int and (quantity_changed or unit_changed or old_spec is not None and old_spec != specification(current_spec)):
                state, reason = 'stale', 'Изменились количество или требования товара.'
            elif type(source_id) is int and quantity is not None and old_spec is not None and not (component and component.parent_id):
                state, reason = 'current', ''
        card_edit = (line.requirements or {}).get('card_edit')
        if card_edit and good and good.status == 'active':
            from .product_editing import input_basis
            state = 'current' if card_edit.get('basis') == input_basis(good) else 'stale'
            reason = '' if state == 'current' else 'Изменились рабочие параметры товара.'
        rows.append({'line_id': line.pk, 'name': line.name, 'state': state, 'reason': reason})
    state = 'unknown' if not rows or any(row['state'] == 'unknown' for row in rows) else 'current'
    if any(row['state'] == 'stale' for row in rows):
        state = 'stale'
    basis = (estimate.summary_snapshot or {}).get('conditions_fingerprint')
    if basis is not None:
        from tender_selection.models import Tender
        conditions = Tender.objects.using(estimate._state.db).filter(pk=estimate.tender_id).values_list('conditions', flat=True).first()
        if conditions is not None and basis != conditions_fingerprint(conditions):
            state = 'stale'
    elif state == 'current':
        state = 'unknown'
    return {'state': state, 'label': labels[state], 'lines': rows}
