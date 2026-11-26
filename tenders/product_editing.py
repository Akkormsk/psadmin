"""Tender card edits use accepted goods; estimates retain immutable input snapshots."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tender_selection.models import Tender
from .models import CalculationComponent, TenderCommercialItem, TenderEstimate, TenderLine, TenderSettings, TenderSourceItem
from .source_memory import signature
from .input_freshness import conditions_fingerprint
from .services import calculate_tender

COST_FIELDS = ('material', 'application', 'logistics')


def input_basis(good):
    return signature({'name': good.display_name, 'quantity': str(good.quantity), 'unit': good.unit,
                      'requirements': good.requirements,
                      'components': list(good.components.filter(status='active').order_by('pk').values(
                          'pk', 'name', 'quantity_per_parent', 'unit', 'requirements', 'parent_id'))})


def _row(obj, kind, saved_line=None):
    saved = deepcopy((obj.provenance or {}).get('card_edit', {}))
    cost = saved.get('manual', {})
    row = {'key': f'{kind}-{obj.pk}', 'name': obj.display_name if kind == 'good' else obj.name,
           'quantity': str(obj.quantity if kind == 'good' else obj.quantity_per_parent),
           'unit': obj.unit, 'mode': saved.get('mode', 'route'), 'nmck': saved.get('nmck', ''),
           'group': saved.get('group', saved_line.group_name if saved_line else ''), 'components': []}
    for field in COST_FIELDS:
        row[field] = cost.get(field, '')
    if saved_line and not saved:
        row['nmck'] = str(saved_line.nmck_unit)
    return row


def product_editor(tender):
    goods = list(tender.v2_commercial_items.filter(status='active').filter(Q(provenance__draft=False) | Q(provenance__draft__isnull=True)).prefetch_related('components', 'source_items').order_by('pk'))
    estimate = tender.estimates.order_by('-updated_at', '-pk').first()
    lines = list(estimate.lines.all()) if estimate else []
    from tender_selection.notification import parse_notification
    notice = parse_notification(tender.notification_raw or {}).get('items', [])
    rows, represented = [], set()
    for good in goods:
        linked = list(good.source_items.all())
        represented.update(source.pk for source in linked)
        matches = [line for line in lines if line.commercial_item_id == good.pk or not line.component_id and len(linked) == 1 and line.requirements.get('source_item_id') == linked[0].pk]
        row = _row(good, 'good', matches[0] if len(matches) == 1 else None)
        if row['nmck'] == '' and not good.provenance.get('card_edit') and len(linked) == 1:
            from .document_enrichment import notification_quote_source
            quote_source = notification_quote_source(linked[0])
            notice_index = quote_source.metadata.get('notification_index') if quote_source else None
            if type(notice_index) is int and 0 <= notice_index < len(notice):
                value = notice[notice_index].get('price')
                row['nmck'] = '' if value is None else str(value)
        if good.structure != TenderCommercialItem.Structure.SIMPLE:
            row['components'] = []
            for component in good.components.all():
                if component.status != 'active':
                    continue
                matches = [line for line in lines if line.component_id == component.pk or
                           not line.commercial_item_id and component.source_item_id and line.requirements.get('source_item_id') == component.source_item_id]
                child = _row(component, 'component', matches[0] if len(matches) == 1 else None)
                child['parent_key'] = f'component-{component.parent_id}' if component.parent_id else None
                row['components'].append(child)
        rows.append(row)
    # Excluded working goods still own their source evidence.
    represented.update(tender.v2_commercial_items.filter(status='excluded').values_list('source_items__pk', flat=True))
    from .calculation_v2_pipeline import active_calculation_items
    for source in active_calculation_items(tender):
        if source.pk not in represented:
            rows.append({'key': f'source-{source.pk}', 'name': source.original_text, 'quantity': str(source.quantity or ''),
                         'unit': source.unit, 'mode': 'route', 'nmck': '', 'material': '', 'application': '', 'logistics': '', 'group': '', 'components': []})
    if not rows and not tender.v2_commercial_items.exists() and not tender.v2_source_items.exists():
        from tender_selection.notification import parse_notification
        for index, source in enumerate(parse_notification(tender.notification_raw or {}).get('items', [])):
            rows.append({'key': f'notice-{index}', 'name': source.get('name', ''), 'quantity': str(source.get('quantity') or ''),
                         'unit': source.get('unit', ''), 'mode': 'route', 'nmck': str(source.get('price') or ''),
                         'material': '', 'application': '', 'logistics': '', 'group': '', 'components': []})
    revision = signature({'items': rows, 'goods': [(g.pk, g.updated_at, input_basis(g)) for g in goods],
                          'sources': list(tender.v2_source_items.values('pk', 'created_at', 'is_active', 'requirements', 'metadata', 'quantity', 'unit')),
                          'notice': tender.notification_raw, 'conditions': tender.conditions,
                          'estimate': (estimate.pk, estimate.updated_at) if estimate else None})
    return {'items': rows, 'revision': revision}


def _number(value, *, optional=False, positive=False):
    if optional and value in (None, ''):
        return None
    try:
        number = Decimal(str(value).replace(',', '.'))
        if not number.is_finite() or number < 0 or positive and number <= 0:
            raise ValueError
        # Use the existing model's decimal precision and storage limit.
        field = TenderCommercialItem._meta.get_field('quantity')
        if number >= Decimal(10) ** (field.max_digits - field.decimal_places):
            raise ValueError
        return number
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('Укажите корректные количество и цены.') from None


def _validate(rows, allowed, *, components=False):
    if not isinstance(rows, list):
        raise ValueError('Некорректный перечень товаров.')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Некорректная позиция.')
        key = row.get('key')
        if not isinstance(key, str) or key in seen or key not in allowed and not key.startswith('new-'):
            raise ValueError('Товар отсутствует или указан повторно.')
        seen.add(key)
        for field in ('name', 'unit'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError('Заполните название и единицу измерения.')
        if len(row['unit']) > CalculationComponent._meta.get_field('unit').max_length or len(row['name']) > TenderLine._meta.get_field('name').max_length:
            raise ValueError('Сократите название или единицу измерения.')
        quantity = _number(row.get('quantity'), positive=True)
        quantity_field = CalculationComponent._meta.get_field('quantity_per_parent') if components else TenderCommercialItem._meta.get_field('quantity')
        if quantity != quantity.quantize(Decimal(1).scaleb(-quantity_field.decimal_places)):
            raise ValueError('Количество содержит слишком много знаков после запятой.')
        _number(row.get('nmck'), optional=True)
        if row.get('mode') not in ('route', 'manual'):
            raise ValueError('Выберите способ определения себестоимости.')
        prices = [_number(row.get(field), optional=True) for field in COST_FIELDS]
        if any(price is not None and price != price.quantize(Decimal('0.01')) for price in prices):
            raise ValueError('Укажите цены с точностью до копейки.')
        if row['mode'] == 'manual' and all(price is None for price in prices):
            raise ValueError('Укажите ручную себестоимость.')
        if components and row.get('components'):
            raise ValueError('Вложенный состав требует отдельного согласования.')
        if not isinstance(row.get('group', ''), str) or len(row.get('group', '')) > TenderLine._meta.get_field('group_name').max_length:
            raise ValueError('Сократите название группы.')


def _remember(obj, row, user):
    previous = deepcopy(obj.provenance or {})
    edit = previous.get('card_edit', {})
    history = edit.get('history', [])
    history = [*history, {'at': timezone.now().isoformat(), 'actor_id': user.pk,
                         'name': getattr(obj, 'display_name', getattr(obj, 'name', '')),
                         'quantity': str(getattr(obj, 'quantity', getattr(obj, 'quantity_per_parent', ''))),
                         'unit': obj.unit, 'previous': {key: value for key, value in edit.items() if key != 'history'}}]
    previous['card_edit'] = {'mode': row['mode'], 'nmck': row.get('nmck', ''), 'group': row.get('group', ''),
                             'manual': {field: row.get(field, '') for field in COST_FIELDS}, 'history': history}
    obj.provenance = previous


def _route_price(good, previous, unchanged):
    stored = (good.provenance or {}).get('card_edit', {}).get('route_price')
    if stored:
        return stored
    if previous:
        edit = previous.requirements.get('card_edit', {})
        if edit.get('route_price'):
            return edit['route_price']
        return {'values': {field: str(getattr(previous, field + '_unit')) for field in COST_FIELDS},
                'known': any(getattr(previous, field + '_unit') > 0 for field in COST_FIELDS),
                'basis': input_basis(good) if unchanged else None,
                'requirements': deepcopy(previous.requirements)}
    return {'values': {}, 'known': False, 'basis': None, 'requirements': {}}


@transaction.atomic
def save_products(user, tender_id, state, *, route_updates=None):
    if not user.is_superuser:
        raise PermissionDenied
    tender = Tender.objects.select_for_update().get(pk=tender_id)
    if tender.status == Tender.DISMISSED:
        raise ValueError('Сначала верните тендер из архива.')
    current = product_editor(tender)
    if not isinstance(state, dict) or state.get('revision') != current['revision']:
        raise ValueError('Товары или расчёт изменились. Откройте редактирование заново.')
    rows = state.get('items')
    allowed = {row['key']: row for row in current['items']}
    _validate(rows, allowed)
    for row in rows:
        old = allowed.get(row['key'], {})
        _validate(row.get('components', []), {c['key'] for c in old.get('components', [])}, components=True)
    from .calculation_v2_pipeline import ingest_source_items, build_commercial_items, active_calculation_items
    if not tender.v2_source_items.exists() and any(row['key'].startswith('notice-') for row in rows):
        ingest_source_items(tender)
    goods = list(tender.v2_commercial_items.exclude(status='draft').prefetch_related('source_items', 'components'))
    represented = {s.pk for good in goods for s in good.source_items.all()}
    missing = [s for s in active_calculation_items(tender) if s.pk not in represented]
    if missing:
        goods += build_commercial_items(tender=tender, sources=missing)
    by_key = {f'good-{good.pk}': good for good in goods}
    for good in goods:
        for source in good.source_items.all():
            by_key[f'source-{source.pk}'] = good
            if source.source_type == 'notification' and 'notification_index' in source.metadata:
                by_key[f"notice-{source.metadata['notification_index']}"] = good
    previous = tender.estimates.order_by('-updated_at', '-pk').first()
    old_lines = list(previous.lines.all()) if previous else []
    if previous and previous.status == TenderEstimate.Status.FINALIZED:
        raise ValueError('Расчёт зафиксирован. Изменение рабочего состава требует отдельного решения.')
    kept, changed, saved = set(), [], []
    for row in rows:
        good = by_key.get(row['key'])
        if good is None:
            good = TenderCommercialItem(tender=tender, source_key='manual:' + uuid4().hex)
        before = input_basis(good) if good.pk else None
        linked = set(good.source_items.values_list('pk', flat=True)) if good.pk else set()
        candidates = [line for line in old_lines if line.commercial_item_id == good.pk and good.pk or not line.component_id and line.requirements.get('source_item_id') in linked]
        if len(candidates) > 1:
            raise ValueError('У товара несколько строк расчёта. Требуется уточнить их связь перед редактированием.')
        old_line = candidates[0] if candidates else None
        route = deepcopy((good.provenance or {}).get('card_edit', {}).get('route_price'))
        old_component_basis = {c.pk: signature({'name': c.name, 'quantity': str(c.quantity_per_parent), 'unit': c.unit, 'requirements': c.requirements}) for c in good.components.all()} if good.pk else {}
        _remember(good, row, user)
        good.display_name, good.quantity, good.unit = row['name'].strip(), _number(row['quantity'], positive=True), row['unit'].strip()
        good.status = 'active'
        good.structure = TenderCommercialItem.Structure.COMPOSITE if row.get('components') else TenderCommercialItem.Structure.SIMPLE
        good.save()
        if not good.source_items.exists():
            source = TenderSourceItem.objects.create(tender=tender, source_type='manual', source_key=good.source_key,
                original_text=good.display_name, quantity=good.quantity, unit=good.unit,
                requirements=deepcopy(good.requirements), provenance={'actor_id': user.pk, 'kind': 'manual_product'})
            good.source_items.add(source)
        kept.add(good.pk)
        component_ids = []
        existing_components = {f'component-{c.pk}': c for c in good.components.all()}
        for index, child in enumerate(row.get('components', [])):
            component = existing_components.get(child['key']) or CalculationComponent(commercial_item=good)
            stored_route = deepcopy((component.provenance or {}).get('card_edit', {}).get('route_price'))
            component_lines = [line for line in old_lines if line.component_id == component.pk and component.pk or
                               not line.commercial_item_id and component.source_item_id and line.requirements.get('source_item_id') == component.source_item_id]
            if len(component_lines) > 1:
                raise ValueError('У компонента несколько строк расчёта. Уточните их связь.')
            _remember(component, child, user)
            component.name, component.unit = child['name'].strip(), child['unit'].strip()
            component.quantity_per_parent = _number(child['quantity'], positive=True)
            component.sort_order, component.status = index, 'active'
            component.save()
            basis = signature({'name': component.name, 'quantity': str(component.quantity_per_parent), 'unit': component.unit, 'requirements': component.requirements})
            if stored_route is None:
                old = component_lines[0] if component_lines else None
                stored_route = {'values': {field: str(getattr(old, field + '_unit')) for field in COST_FIELDS} if old else {},
                                'known': bool(old and any(getattr(old, field + '_unit') > 0 for field in COST_FIELDS)),
                                'basis': basis if old_component_basis.get(component.pk) == basis else None}
            component.provenance['card_edit']['route_price'] = stored_route
            component.save(update_fields=['provenance'])
            component_ids.append(component.pk)
        if any(c.parent_id and c.pk in component_ids and c.parent_id not in component_ids for c in good.components.all()):
            raise ValueError('Сначала исключите вложенные компоненты выбранного состава.')
        if row.get('components'):
            good.components.exclude(pk__in=component_ids).update(status='superseded')
        elif good.components.filter(status='active').count() > 1:
            good.components.filter(status='active').update(status='superseded')
        elif good.components.filter(status='active').exists():
            component = good.components.get(status='active')
            component.name, component.unit, component.requirements = good.display_name, good.unit, deepcopy(good.requirements)
            component.quantity_per_parent = Decimal('1')
            component.save()
        else:
            CalculationComponent.objects.create(commercial_item=good, source_item=good.source_items.first(), name=good.display_name, unit=good.unit, requirements=deepcopy(good.requirements))
        unchanged = before == input_basis(good)
        route = route or _route_price(good, old_line, unchanged)
        if route_updates and good.pk in route_updates:
            route = route_updates[good.pk]
            route['basis'] = input_basis(good)
        good.provenance['card_edit']['route_price'] = route
        good.save(update_fields=['provenance'])
        if not unchanged:
            changed.append(good.pk)
        saved.append((good, row, route, old_line))
    excluded = [good.pk for good in goods if good.pk not in kept and good.status == 'active']
    for good in goods:
        if good.pk in excluded:
            good.status = 'excluded'
            good.provenance = {**good.provenance, 'card_edit': {**good.provenance.get('card_edit', {}), 'excluded_by': user.pk}}
            good.save(update_fields=['status', 'provenance', 'updated_at'])
    from .tender_data import mark_tender_inputs_changed
    if changed or excluded:
        mark_tender_inputs_changed(tender, commercial_ids=changed + excluded)
    settings = TenderSettings.objects.first() or TenderSettings()
    estimate = TenderEstimate.objects.create(tender=tender, owner=user, supersedes=previous,
        name=tender.title[:300], tender_number=tender.purchase_number,
        reduction_percent=previous.reduction_percent if previous else settings.default_reduction_percent,
        russia_delivery=previous.russia_delivery if previous else 0,
        vat_rate_snapshot=previous.vat_rate_snapshot if previous else settings.vat_rate,
        document_analysis=deepcopy(previous.document_analysis) if previous else {})
    lines = []
    for index, (good, row, route, old_line) in enumerate(saved):
        manual = row['mode'] == 'manual'
        values = {field: _number(row.get(field), optional=True) for field in COST_FIELDS} if manual else {field: _number(route['values'].get(field), optional=True) for field in COST_FIELDS}
        known = any(value is not None for value in values.values()) if manual else route['known'] and route['basis'] == input_basis(good)
        if not manual and row.get('components'):
            component_values = {field: Decimal('0') for field in COST_FIELDS}
            known = True
            for component in good.components.filter(status='active'):
                edit = component.provenance['card_edit']
                component_basis = signature({'name': component.name, 'quantity': str(component.quantity_per_parent), 'unit': component.unit, 'requirements': component.requirements})
                if edit['mode'] == 'manual':
                    prices = edit['manual']
                else:
                    prices = edit['route_price']['values']
                    known = known and edit['route_price']['known'] and edit['route_price']['basis'] == component_basis
                # Existing routing supports direct components; retain nested input as unverified.
                known = known and component.parent_id is None
                for field in COST_FIELDS:
                    component_values[field] += (_number(prices.get(field), optional=True) or 0) * component.quantity_per_parent
            values = component_values
        nmck = _number(row.get('nmck'), optional=True)
        requirements = deepcopy(old_line.requirements) if old_line else {}
        sources = list(good.source_items.all())
        if len(sources) == 1:
            requirements['source_item_id'] = sources[0].pk
        if route_updates and good.pk in route_updates:
            requirements['production'] = deepcopy(route['requirements'].get('production', {}))
        requirements.update(source_requirements=deepcopy(good.requirements), source_unit=good.unit,
                            card_edit={'basis': input_basis(good), 'mode': row['mode'], 'known': known,
                                       'nmck_known': nmck is not None, 'route_price': route})
        line = TenderLine.objects.create(estimate=estimate, commercial_item=good, name=good.display_name,
            quantity=good.quantity, nmck_unit=nmck or 0, requirements=requirements, sort_order=index,
            product_url=old_line.product_url if old_line else '', comment=old_line.comment if old_line else '',
            group_key=(old_line.group_key if old_line and old_line.group_name == row.get('group') else 'group-' + signature(row['group'])[:32]) if row.get('group') else '', group_name=row.get('group', ''),
            **{field + '_unit': value or 0 for field, value in values.items()})
        lines.append({'name': line.name, 'quantity': line.quantity, 'nmck_unit': line.nmck_unit,
                      'group_key': line.group_key, 'group_name': line.group_name, 'requirements': line.requirements,
                      **{field + '_unit': getattr(line, field + '_unit') for field in COST_FIELDS}})
    _, summary = calculate_tender(lines, estimate.reduction_percent, estimate.russia_delivery, estimate.vat_rate_snapshot)
    estimate.summary_snapshot = {key: value if key == 'groups' else str(value) for key, value in summary.items()}
    estimate.summary_snapshot.update(is_incomplete=not saved or any(not l.requirements['card_edit']['known'] or not l.requirements['card_edit']['nmck_known'] for l in estimate.lines.all()), conditions_fingerprint=conditions_fingerprint(tender.conditions))
    estimate.save(update_fields=['summary_snapshot'])
    return product_editor(tender)


def apply_working_products(tender, items, routes):
    """Overlay card-managed goods on existing source presentation without changing evidence."""
    from .document_enrichment import characteristic_display
    from tender_selection.national_regime import annotation
    managed = list(tender.v2_commercial_items.filter(provenance__card_edit__isnull=False).prefetch_related('source_items', 'components'))
    if not managed:
        return items
    source_ids = {s.pk for good in managed for s in good.source_items.all()}
    by_source = {item.get('source_id'): item for item in items if item.get('source_id')}
    by_good = {item.get('commercial_id'): item for item in items if item.get('commercial_id')}
    result = [item for item in items if item.get('source_id') not in source_ids and item.get('commercial_id') not in {good.pk for good in managed}]
    for good in managed:
        if good.status != 'active':
            continue
        linked = list(good.source_items.all())
        source = linked[0] if len(linked) == 1 else None
        item = deepcopy(by_good.get(good.pk) or by_source.get(source.pk if source else None) or {})
        item.update(commercial_id=good.pk, name=good.display_name, name_parts=[], quantity=good.quantity, unit=good.unit,
                    group_key=None, parent_display_name='', components=[], components_detail=[])
        item['source_id'] = source.pk if source else None
        item.setdefault('national_regime', annotation())
        item['characteristics'] = [characteristic_display(row) for row in good.requirements.get('characteristics', [])]
        item['production_route'] = routes.get(source.pk) if source else None
        if good.structure != TenderCommercialItem.Structure.SIMPLE:
            for component in good.components.all():
                if component.status != 'active':
                    continue
                child = {'source_id': component.source_item_id, 'name': component.name,
                         'quantity': component.effective_quantity, 'unit': component.unit,
                         'parent_display_name': good.display_name, 'national_regime': annotation(),
                         'tz_rows': [characteristic_display(row) for row in component.requirements.get('characteristics', [])],
                         'production_route': routes.get(component.source_item_id), 'finance_key': f'component-{component.pk}'}
                item['components_detail'].append(child)
        group_name = good.provenance['card_edit'].get('group', '')
        if group_name:
            item.update(group_key='edit-' + signature(group_name)[:32], parent_display_name=group_name,
                        original_item={'name': group_name}, parent_group_edit=True)
        result.append(item)
    return result


def save_route_prices(user, estimate, data):
    """Existing route controls may update route results, never manual or product inputs."""
    import json
    from .input_freshness import estimate_input_status
    latest = estimate.tender.estimates.order_by('-updated_at', '-pk').first()
    if latest.pk != estimate.pk or estimate_input_status(estimate)['state'] != 'current':
        raise ValueError('Расчёт изменился. Откройте карточку заново.')
    posted = json.loads(data.get('lines_json', '[]'))
    stored = {line.pk: line for line in estimate.lines.all()}
    if not isinstance(posted, list) or len(posted) != len(stored) or {row.get('id') for row in posted} != set(stored):
        raise ValueError('Редактируйте перечень во вкладке «Товары».')
    updates = {}
    for row in posted:
        line = stored[row['id']]
        if row.get('group_key', '') != line.group_key or row.get('group_name', '') != line.group_name:
            raise ValueError('Редактируйте группы во вкладке «Товары».')
        if row.get('name') != line.name or _number(row.get('quantity')) != line.quantity or _number(row.get('nmck_unit')) != line.nmck_unit:
            raise ValueError('Редактируйте перечень во вкладке «Товары».')
        requirements = row.get('requirements')
        if not isinstance(requirements, dict) or not isinstance(requirements.get('production'), dict):
            continue
        if requirements['production'] == line.requirements.get('production'):
            continue
        updates[line.commercial_item_id] = {'values': {field: str(_number(row.get(field + '_unit'))) for field in COST_FIELDS},
                                           'known': bool(requirements['production'].get('costs')),
                                           'requirements': deepcopy(requirements)}
    if not updates:
        return estimate
    save_products(user, estimate.tender_id, product_editor(estimate.tender), route_updates=updates)
    return estimate.tender.estimates.order_by('-updated_at', '-pk').first()


def working_route_sources(tender, sources):
    """Present current working inputs to the existing router; source rows remain evidence."""
    owners = {}
    for good in tender.v2_commercial_items.filter(provenance__card_edit__isnull=False).prefetch_related('source_items'):
        for source in good.source_items.all():
            owners.setdefault(source.pk, []).append(good)
    result = []
    for source in sources:
        goods = owners.get(source.pk, [])
        if not goods:
            result.append(source)
            continue
        active = [good for good in goods if good.status == 'active']
        if len(active) != 1:
            continue
        good = active[0]
        projected = deepcopy(source)
        projected.original_text, projected.quantity, projected.unit = good.display_name, good.quantity, good.unit
        projected.requirements = deepcopy(good.requirements)
        result.append(projected)
    return result
