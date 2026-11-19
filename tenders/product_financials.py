"""Presentation of saved product finances through the existing tender calculator."""
from collections import defaultdict
from decimal import Decimal

from .services import _parse_document_decimal, calculate_tender

COST_FIELDS = ('material_unit', 'application_unit', 'logistics_unit')
COLUMN_DEFINITIONS = (
    ('НМЦК', 'nmck_total', 'nmck_unit'),
    ('Себестоимость', 'purchase_total', 'purchase_unit'),
    ('РРЦ', 'rrp_total', 'rrp_unit'),
    ('Прибыль*', 'profit', 'profit_unit'),
)
STATE_LABELS = {
    'current': '', 'partial': 'Рассчитана часть', 'stale': 'Прежний расчёт',
    'unknown': 'Актуальность не подтверждена', 'unavailable': 'Нет расчёта',
}


def _source_decimal(value):
    number = _parse_document_decimal(value)
    return number if number is not None and number.is_finite() else None


def _finish(finance, quantity):
    for _, total_key, unit_key in COLUMN_DEFINITIONS:
        total = finance[total_key]
        if finance.get(unit_key) is None and total is not None and quantity and quantity > 0:
            finance[unit_key] = Decimal(str(total)) / quantity
    finance['label'] = STATE_LABELS[finance['state']]
    finance['cells'] = []
    for label, total, unit in COLUMN_DEFINITIONS:
        if total == 'purchase_total' and not finance['complete'] and finance[total] is not None:
            label = 'Себестоимость (часть)'
        finance['cells'].append({'label': label, 'total': finance[total],
            'unit': finance.get(unit), 'profit': total == 'profit'})
    return finance


def _state(freshness, complete):
    if freshness != 'current':
        return freshness
    if complete:
        return 'current'
    return 'partial'


def _original(item):
    return {'nmck_total': _source_decimal(item.get('sum')), 'nmck_unit': _source_decimal(item.get('price')),
        'purchase_total': None, 'purchase_unit': None, 'rrp_total': None, 'rrp_unit': None,
        'profit': None, 'profit_unit': None, 'state': 'unavailable', 'complete': False}


def _cost_complete(line):
    if not line['name'] or line['quantity'] <= 0 or not any(line[key] > 0 for key in COST_FIELDS):
        return False
    production = line.get('requirements', {}).get('production') or {}
    if production.get('stage') != 'training_dialogue':
        return True
    steps = production.get('route', {}).get('processes') or []
    required = {step.get('id') for step in steps}
    priced = {cost.get('step_id') for cost in production.get('costs', []) if cost.get('amount_total') is not None}
    return bool(required) and None not in required and required.issubset(priced)


def present_product_financials(items, groups, workspace, estimate, notification_total=None):
    """Annotate existing display objects only; never create or save calculation data."""
    for index, item in enumerate(items):
        if item.get('source_id') is not None:
            item['finance_key'] = f"source-{item['source_id']}"
        elif item.get('commercial_id'):
            item['finance_key'] = f"commercial-{item['commercial_id']}"
        else:
            item['finance_key'] = f'position-{index}'
        item['finance'] = _finish(_original(item), _source_decimal(item.get('quantity')))
    for group in groups:
        group['finance_key'] = f"group-{group['key']}"
        original = group.get('original') or {}
        group['finance'] = _finish(_original(original), _source_decimal(original.get('quantity')))
    if workspace is None or estimate is None:
        return _finish(_original({'sum': notification_total}), None) if notification_total is not None else None
    lines = []
    for saved in workspace['lines']:
        line = {**saved}
        for key in ('quantity', 'nmck_unit', *COST_FIELDS):
            line[key] = Decimal(str(saved[key]))
        lines.append(line)
    calculated, summary = calculate_tender(lines, estimate.reduction_percent,
        estimate.russia_delivery, estimate.vat_rate_snapshot)
    by_source, by_commercial, by_group = defaultdict(list), defaultdict(list), defaultdict(list)
    for line, values in zip(lines, calculated):
        pair = (line, values)
        source_id = (line.get('requirements') or {}).get('source_item_id')
        if source_id is not None:
            by_source[source_id].append(pair)
        if line.get('commercial_item_id'):
            by_commercial[line['commercial_item_id']].append(pair)
        if line.get('group_key'):
            by_group[line['group_key']].append(pair)
    freshness = workspace['input_status']['state']
    covered = set()
    for item in items:
        matches = by_source[item['source_id']] if item.get('source_id') is not None else by_commercial[item.get('commercial_id')]
        if len(matches) != 1:
            continue
        line, values = matches[0]
        complete = _cost_complete(line)
        covered.add(line['id'])
        finance = _original(item)
        finance['complete'] = complete
        finance['state'] = _state(freshness, complete)
        if values['purchase_total'] > 0:
            finance.update(purchase_total=values['purchase_total'], purchase_unit=values['purchase_unit'])
        if freshness == 'current':
            grouped_quote = values['nmck_total'] is None
            if grouped_quote or line['nmck_unit'] > 0:
                finance.update(nmck_total=values['nmck_total'], nmck_unit=None if grouped_quote else line['nmck_unit'],
                    rrp_total=values['rrp_total'], rrp_unit=values['rrp_unit'], profit=values['profit'] if complete else None)
            else:
                finance['state'] = 'partial'
        item['finance'] = _finish(finance, line['quantity'])
    for group in groups:
        source_ids = [item.get('source_id') for item in group['items']]
        if not source_ids or None in source_ids or len(set(source_ids)) != len(source_ids):
            continue
        matches = []
        for key, members in by_group.items():
            ids = [(line.get('requirements') or {}).get('source_item_id') for line, _ in members]
            quote = members[0][0].get('requirements', {}).get('source_group') or {}
            if len(ids) == len(source_ids) and set(ids) == set(source_ids) and quote.get('source_item_id') == group['key']:
                matches.append((key, members, quote))
        if len(matches) != 1:
            continue
        key, members, quote = matches[0]
        values = summary.get('groups', {}).get(key)
        if values is None:
            continue
        complete = all(_cost_complete(line) for line, _ in members) and not values['is_incomplete']
        finance = _original(group.get('original') or {})
        finance['complete'] = complete
        finance['state'] = _state(freshness, complete)
        if Decimal(values['purchase_total']) > 0:
            finance['purchase_total'] = Decimal(values['purchase_total'])
        if freshness == 'current':
            finance.update(nmck_total=_source_decimal(values['nmck_total']) if values['nmck_total'] is not None else finance['nmck_total'], rrp_total=_source_decimal(values['rrp_total']),
                profit=_source_decimal(values['gross_profit']) if complete else None)
        group['finance'] = _finish(finance, _source_decimal(quote.get('quantity')))

    if not lines or covered != {line['id'] for line in lines}:
        return None
    missing_items = any(item['finance']['state'] == 'unavailable' for item in items)
    complete = not missing_items and all(_cost_complete(line) for line in lines)
    finance = _original({'sum': notification_total})
    finance['complete'] = complete
    finance['state'] = _state(freshness, complete)
    if summary['purchase_total'] > 0:
        finance['purchase_total'] = summary['purchase_total']
    if freshness == 'current':
        known_prices = not missing_items and all(values['nmck_total'] is None or values['nmck_total'] > 0 for values in calculated)
        if not known_prices:
            finance['state'] = 'partial'
        if known_prices:
            finance.update(nmck_total=summary['nmck_total'], rrp_total=summary['rrp_total'],
                profit=summary['gross_profit'] if complete else None)
    return _finish(finance, None)
