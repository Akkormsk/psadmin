"""Tender card edits use accepted goods; estimates retain immutable input snapshots."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from types import SimpleNamespace

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
    return signature({'name': good.display_name, 'quantity': str(good.quantity.normalize()) if good.quantity is not None else None, 'unit': good.unit,
                      'requirements': good.requirements,
                      'components': list(good.components.filter(status='active').order_by('pk').values(
                          'pk', 'name', 'quantity_per_parent', 'unit', 'requirements', 'parent_id'))})



def component_basis(component):
    return signature({'name': component.name,
                      'quantity': str(component.quantity_per_parent.normalize()),
                      'batch_quantity': str(component.commercial_item.quantity.normalize()) if component.commercial_item.quantity is not None else None,
                      'unit': component.unit, 'requirements': component.requirements, 'parent_id': component.parent_id})


def _row(obj, kind, saved_line=None):
    saved = deepcopy((obj.provenance or {}).get('card_edit', {}))
    cost = saved.get('manual', {})
    row = {'key': f'{kind}-{obj.pk}', 'name': obj.display_name if kind == 'good' else obj.name,
           'quantity': '' if kind == 'good' and obj.quantity is None else str(obj.quantity if kind == 'good' else obj.quantity_per_parent),
           'unit': obj.unit, 'mode': saved.get('mode', 'route'), 'nmck': saved.get('nmck', ''),
           'group': saved.get('group', saved_line.group_name if saved_line else ''), 'components': [],
           'cost_entry': saved.get('cost_entry', 'parts' if cost.get('application') or cost.get('logistics') else 'total'),
           'total': cost.get('total', cost.get('material', ''))}
    for field in COST_FIELDS:
        row[field] = cost.get(field, '')
    route = saved.get('route_price')
    row['route_label'] = 'Цена маршрута неизвестна.'
    if route and route.get('known'):
        basis = input_basis(obj) if kind == 'good' else component_basis(obj)
        row['route_label'] = 'Сохранённая цена маршрута.' if route.get('basis') == basis else 'Сохранённая цена требует пересчёта.'
    if saved_line and not saved:
        row['nmck'] = str(saved_line.nmck_unit)
        if not saved_line.requirements.get('production') and any(getattr(saved_line, field + '_unit') > 0 for field in COST_FIELDS):
            row['mode'] = 'manual'
            row['cost_entry'] = 'parts'
            row['total'] = ''
            row.update({field: str(getattr(saved_line, field + '_unit')) for field in COST_FIELDS})
    return row



def _matching_line(lines, *, good=None, component=None, source_id=None):
    if good is not None:
        matches = [line for line in lines if line.commercial_item_id == good.pk]
        if not matches and good.structure == TenderCommercialItem.Structure.SIMPLE:
            component_ids = {item.pk for item in good.components.all() if item.status == 'active'}
            matches = [line for line in lines if line.component_id in component_ids]
    elif component is not None:
        matches = [line for line in lines if line.component_id == component.pk]
    else:
        matches = []
    if not matches and source_id:
        matches = [line for line in lines if not line.commercial_item_id and not line.component_id and line.requirements.get('source_item_id') == source_id]
    if len(matches) > 1:
        raise ValueError('У товара несколько несопоставленных строк расчёта. Проверьте связи перед редактированием.')
    return matches[0] if matches else None


def _source_row(source, lines, notice, *, component=False):
    from .document_enrichment import notification_quote_source
    model = SimpleNamespace(pk=source.pk, display_name=source.original_text, name=source.original_text,
                            quantity=source.quantity, quantity_per_parent=source.metadata.get('quantity_per_parent', '1'),
                            unit=source.unit, provenance={})
    row = _row(model, 'component' if component else 'good', _matching_line(lines, source_id=source.pk))
    row['key'] = ('component-source-' if component else 'source-') + str(source.pk)
    row['source_ids'] = [source.pk]
    row['source_locked'] = not component and source.source_type != 'manual'
    row['source_label'] = 'Исходная позиция закупки' if row['source_locked'] else ('Добавлен вручную' if not component else '')
    quote = notification_quote_source(source)
    index = quote.metadata.get('notification_index') if quote else None
    if not component and row['nmck'] == '' and type(index) is int and 0 <= index < len(notice):
        price = notice[index].get('price')
        row['nmck'] = '' if price is None else str(price)
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
        previous = _matching_line(lines, good=good, source_id=linked[0].pk if len(linked) == 1 else None)
        row = _row(good, 'good', previous)
        row['commercial_id'] = good.pk
        row['source_ids'] = [source.pk for source in linked]
        row['source_locked'] = bool(linked) and any(source.source_type != 'manual' for source in linked)
        row['source_label'] = 'Исходная позиция закупки' if row['source_locked'] else 'Добавлен вручную'
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
                previous = _matching_line(lines, component=component, source_id=component.source_item_id)
                child = _row(component, 'component', previous)
                child['parent_key'] = f'component-{component.parent_id}' if component.parent_id else None
                child['source_ids'] = [component.source_item_id] if component.source_item_id else []
                row['components'].append(child)
        rows.append(row)
    # Excluded working goods still own their source evidence.
    represented.update(tender.v2_commercial_items.filter(status='excluded').values_list('source_items__pk', flat=True))
    from .calculation_v2_pipeline import active_calculation_items
    for source in active_calculation_items(tender):
        if source.pk not in represented:
            row = _source_row(source, lines, notice)
            row['components'] = [_source_row(child, lines, notice, component=True) for child in source.derived_items.filter(is_active=True, source_type='document_component').order_by('pk')]
            rows.append(row)
    if not rows and not tender.v2_commercial_items.exists() and not tender.v2_source_items.exists():
        from tender_selection.notification import parse_notification
        for index, source in enumerate(parse_notification(tender.notification_raw or {}).get('items', [])):
            rows.append({'key': f'notice-{index}', 'name': source.get('name', ''), 'quantity': str(source.get('quantity') or ''),
                         'unit': source.get('unit', ''), 'mode': 'route', 'nmck': str(source.get('price') or ''),
                         'material': '', 'application': '', 'logistics': '', 'group': '', 'components': [],
                         'source_locked': True, 'source_label': 'Исходная позиция закупки'})
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



def manual_values(row):
    if row.get('cost_entry', 'parts') == 'total':
        total = _number(row.get('total'), optional=True)
        return {'material': total, 'application': None, 'logistics': None}
    return {field: _number(row.get(field), optional=True) for field in COST_FIELDS}


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
        quantity = _number(row.get('quantity'), optional=not components, positive=True)
        quantity_field = CalculationComponent._meta.get_field('quantity_per_parent') if components else TenderCommercialItem._meta.get_field('quantity')
        if quantity is not None and quantity != quantity.quantize(Decimal(1).scaleb(-quantity_field.decimal_places)):
            raise ValueError('Количество содержит слишком много знаков после запятой.')
        price = _number(row.get('nmck'), optional=True)
        if price is not None and price != price.quantize(Decimal('0.01')):
            raise ValueError('Укажите цены с точностью до копейки.')
        if row.get('mode') not in ('route', 'manual'):
            raise ValueError('Выберите способ определения себестоимости.')
        if row.get('cost_entry', 'parts') not in ('total', 'parts'):
            raise ValueError('Выберите ввод общей суммы или детализации.')
        prices = list(manual_values(row).values())
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
                         'unit': obj.unit, 'previous': {key: edit[key] for key in ('mode', 'manual', 'nmck', 'group') if key in edit}}]
    previous['card_edit'] = {'mode': row['mode'], 'cost_entry': row.get('cost_entry', 'parts'), 'nmck': row.get('nmck', ''), 'group': row.get('group', ''),
                             'manual': {field: row.get(field, '') for field in (*COST_FIELDS, 'total')}, 'history': history}
    obj.provenance = previous


def _route_price(good, previous, unchanged):
    stored = (good.provenance or {}).get('card_edit', {}).get('route_price')
    if stored:
        return stored
    if previous:
        edit = previous.requirements.get('card_edit', {})
        if edit.get('route_price'):
            return edit['route_price']
        if edit.get('mode') == 'manual' or not previous.requirements.get('production'):
            return {'values': {}, 'known': False, 'basis': None, 'requirements': {}}
        from .product_financials import _cost_complete
        old_values = {field + '_unit': getattr(previous, field + '_unit') for field in COST_FIELDS}
        complete = _cost_complete({'name': previous.name, 'quantity': previous.quantity, 'requirements': previous.requirements, **old_values})
        return {'values': {field: str(getattr(previous, field + '_unit')) for field in COST_FIELDS},
                'known': complete,
                'basis': input_basis(good) if unchanged and previous.quantity == (good.quantity or 0) and previous.requirements.get('source_unit', good.unit) == good.unit else None,
                'requirements': deepcopy(previous.requirements)}
    return {'values': {}, 'known': False, 'basis': None, 'requirements': {}}


@transaction.atomic
def save_products(user, tender_id, state, *, route_updates=None, route_only=False):
    if not user.is_superuser:
        raise PermissionDenied
    tender = Tender.objects.select_for_update().get(pk=tender_id)
    if tender.status == Tender.DISMISSED:
        raise ValueError('Сначала верните тендер из архива.')
    list(tender.estimates.select_for_update().values_list('pk', flat=True))
    current = product_editor(tender)
    if not isinstance(state, dict) or state.get('revision') != current['revision']:
        raise ValueError('Товары или расчёт изменились. Откройте редактирование заново.')
    rows = deepcopy(state.get('items'))
    if route_only and isinstance(rows, list):
        confirmations = state.get('confirmations') if isinstance(state.get('confirmations'), dict) else {}
        confirmed_quantity = set(confirmations.get('quantity', []))
        confirmed_nmck = set(confirmations.get('nmck', []))
        confirmed_exclusions = set(confirmations.get('exclusions', []))
        old_rows = {row['key']: row for row in current['items']}
        submitted_keys = {row.get('key') for row in rows if isinstance(row, dict)}
        unconfirmed = [key for key, old in old_rows.items()
                       if old.get('source_locked') and key not in submitted_keys and key not in confirmed_exclusions]
        if unconfirmed:
            raise ValueError('Подтвердите исключение исходного товара.')
        def normalize(row, old):
            if not isinstance(row, dict):
                raise ValueError('Некорректная позиция.')
            if old.get('source_locked') and row.get('quantity', '') != old.get('quantity', '') and row.get('key') not in confirmed_quantity:
                raise ValueError('Подтвердите изменение количества из извещения.')
            if old.get('source_locked') and row.get('nmck', '') != old.get('nmck', '') and row.get('key') not in confirmed_nmck:
                raise ValueError('Подтвердите изменение НМЦК из извещения.')
            # Legacy manual values remain in provenance/history, but never drive a new version.
            row.update(mode='route', cost_entry=old.get('cost_entry', 'parts'))
            for field in (*COST_FIELDS, 'total'):
                row[field] = old.get(field, '')
            children = {child['key']: child for child in old.get('components', [])}
            for child in row.get('components', []):
                normalize(child, children.get(child.get('key'), {}))
        prior = old_rows
        for row in rows:
            normalize(row, prior.get(row.get('key'), {}))
    allowed = {row['key']: row for row in current['items']}
    _validate(rows, allowed)
    for row in rows:
        old = allowed.get(row['key'], {})
        _validate(row.get('components', []), {c['key'] for c in old.get('components', [])}, components=True)
    from .calculation_v2_pipeline import ingest_source_items, build_commercial_items, active_calculation_items
    if not tender.v2_source_items.exists() and any(row['key'].startswith('notice-') for row in current['items']):
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
        if good is None and not row['key'].startswith('new-'):
            raise ValueError('Не удалось сопоставить рабочую позицию с источником.')
        if good is None:
            good = TenderCommercialItem(tender=tender, source_key='manual:' + uuid4().hex)
        before = input_basis(good) if good.pk else None
        previous_structure = good.structure
        linked = set(good.source_items.values_list('pk', flat=True)) if good.pk else set()
        old_line = _matching_line(old_lines, good=good, source_id=next(iter(linked)) if len(linked) == 1 else None) if good.pk else None
        route = deepcopy((good.provenance or {}).get('card_edit', {}).get('route_price'))
        old_component_basis = {c.pk: component_basis(c) for c in good.components.all()} if good.pk else {}
        _remember(good, row, user)
        good.display_name, good.quantity, good.unit = row['name'].strip(), _number(row['quantity'], optional=True, positive=True), row['unit'].strip()
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
        existing_components.update({f'component-source-{c.source_item_id}': c for c in good.components.all() if c.source_item_id})
        for index, child in enumerate(row.get('components', [])):
            component = existing_components.get(child['key']) or CalculationComponent(commercial_item=good)
            stored_route = deepcopy((component.provenance or {}).get('card_edit', {}).get('route_price'))
            component_line = _matching_line(old_lines, component=component, source_id=component.source_item_id) if component.pk else None
            _remember(component, child, user)
            component.name, component.unit = child['name'].strip(), child['unit'].strip()
            component.quantity_per_parent = _number(child['quantity'], positive=True)
            component.sort_order, component.status = index, 'active'
            if component.source_item_id is None:
                component.source_item = TenderSourceItem.objects.create(tender=tender, source_type='document_component',
                    source_key='manual-component:' + uuid4().hex, parent=good.source_items.first(),
                    original_text=component.name, quantity=component.effective_quantity, unit=component.unit,
                    requirements=deepcopy(component.requirements), metadata={'quantity_per_parent': str(component.quantity_per_parent)},
                    provenance={'actor_id': user.pk, 'kind': 'manual_component'})
            component.save()
            basis = component_basis(component)
            if stored_route is None:
                old = component_line
                from .product_financials import _cost_complete
                stored_route = {'values': {field: str(getattr(old, field + '_unit')) for field in COST_FIELDS} if old else {},
                                'known': bool(old and _cost_complete({'name': old.name, 'quantity': old.quantity,
                                    'requirements': old.requirements, **{field + '_unit': getattr(old, field + '_unit') for field in COST_FIELDS}})),
                                'basis': basis if old_component_basis.get(component.pk) == basis else None,
                                'requirements': deepcopy(old.requirements) if old else {}}
            if route_updates and f'component-{component.pk}' in route_updates:
                stored_route = {**route_updates[f'component-{component.pk}'], 'basis': basis}
            component.provenance['card_edit']['route_price'] = stored_route
            component.save(update_fields=['provenance'])
            component_ids.append(component.pk)
        if any(c.parent_id and c.pk in component_ids and c.parent_id not in component_ids for c in good.components.all()):
            raise ValueError('Сначала исключите вложенные компоненты выбранного состава.')
        if row.get('components'):
            good.components.exclude(pk__in=component_ids).update(status='superseded')
        elif previous_structure != TenderCommercialItem.Structure.SIMPLE:
            good.components.filter(status='active').update(status='superseded')
            CalculationComponent.objects.create(commercial_item=good, source_item=good.source_items.first(), name=good.display_name, unit=good.unit, requirements=deepcopy(good.requirements))
        elif good.components.filter(status='active').exists():
            component = good.components.get(status='active')
            component.name, component.unit, component.requirements = good.display_name, good.unit, deepcopy(good.requirements)
            component.quantity_per_parent = Decimal('1')
            component.save()
        else:
            CalculationComponent.objects.create(commercial_item=good, source_item=good.source_items.first(), name=good.display_name, unit=good.unit, requirements=deepcopy(good.requirements))
        unchanged = before == input_basis(good)
        route = route or _route_price(good, old_line, unchanged)
        if route_updates and f'good-{good.pk}' in route_updates:
            route = route_updates[f'good-{good.pk}']
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
    if previous:
        estimate = previous
        estimate.name = tender.title[:300]
        estimate.tender_number = tender.purchase_number
        estimate.supersedes = None
        estimate.lines.all().delete()
        estimate.save(update_fields=['name', 'tender_number', 'supersedes', 'updated_at'])
    else:
        settings = TenderSettings.objects.first() or TenderSettings()
        estimate = TenderEstimate.objects.create(tender=tender, owner=user,
            name=tender.title[:300], tender_number=tender.purchase_number,
            reduction_percent=settings.default_reduction_percent,
            russia_delivery=0, vat_rate_snapshot=settings.vat_rate)
    lines = []
    for good, row, route, old_line in saved:
        nmck = _number(row.get('nmck'), optional=True)
        sources = list(good.source_items.all())
        if row['mode'] == 'route' and row.get('components'):
            components = list(good.components.filter(status='active').select_related('commercial_item'))
            source_id = sources[0].pk
            quote = {'source_item_id': source_id, 'member_source_ids': [c.source_item_id for c in components],
                     'quantity': str(good.quantity) if good.quantity is not None else None, 'unit': good.unit,
                     'price': str(nmck) if nmck is not None else None,
                     'sum': str(nmck * good.quantity) if nmck is not None and good.quantity is not None else None}
            for component in components:
                edit = component.provenance['card_edit']
                component_route = edit['route_price']
                manual = edit['mode'] == 'manual'
                values = manual_values({**edit['manual'], 'cost_entry': edit.get('cost_entry', 'parts')}) if manual else {
                    field: _number(component_route['values'].get(field), optional=True) for field in COST_FIELDS}
                known = any(value is not None for value in values.values()) if manual else bool(
                    component_route['known'] and component_route['basis'] == component_basis(component))
                known = known and good.quantity is not None and component.parent_id is None
                old = _matching_line(old_lines, component=component, source_id=component.source_item_id)
                requirements = deepcopy(component_route.get('requirements') or (old.requirements if old else {}))
                requirements.update(source_item_id=component.source_item_id, source_unit=component.unit,
                    source_requirements=deepcopy(component.requirements),
                    source_group={**quote, 'component_quantity': str(component.effective_quantity or 0)},
                    card_edit={'basis': input_basis(good), 'mode': edit['mode'], 'known': known,
                               'nmck_known': nmck is not None and good.quantity is not None,
                               'route_known': bool(component_route.get('known'))})
                TenderLine.objects.create(estimate=estimate, component=component, name=component.name,
                    quantity=component.effective_quantity or 0, nmck_unit=0,
                    requirements=requirements, sort_order=estimate.lines.count(),
                    group_key=f'source-{source_id}', group_name=good.display_name,
                    product_url=old.product_url if old else '', comment=old.comment if old else '',
                    **{field + '_unit': value or 0 for field, value in values.items()})
            continue
        manual = row['mode'] == 'manual'
        values = manual_values(row) if manual else {field: _number(route['values'].get(field), optional=True) for field in COST_FIELDS}
        known = any(value is not None for value in values.values()) if manual else route['known'] and route['basis'] == input_basis(good)
        known = known and good.quantity is not None
        requirements = deepcopy(old_line.requirements) if old_line else {}
        if len(sources) == 1:
            requirements['source_item_id'] = sources[0].pk
        if route.get('requirements', {}).get('production'):
            requirements['production'] = deepcopy(route['requirements']['production'])
        requirements.update(source_requirements=deepcopy(good.requirements), source_unit=good.unit,
                            card_edit={'basis': input_basis(good), 'mode': row['mode'], 'known': known,
                                       'nmck_known': nmck is not None and good.quantity is not None,
                                       'route_known': bool(route.get('known'))})
        line = TenderLine.objects.create(estimate=estimate, commercial_item=good, name=good.display_name,
            quantity=good.quantity or 0, nmck_unit=nmck or 0, requirements=requirements, sort_order=estimate.lines.count(),
            product_url=old_line.product_url if old_line else '', comment=old_line.comment if old_line else '',
            group_key=(old_line.group_key if old_line and old_line.group_name == row.get('group') else 'group-' + signature(row['group'])[:32]) if row.get('group') else '', group_name=row.get('group', ''),
            **{field + '_unit': value or 0 for field, value in values.items()})
    for line in estimate.lines.all():
        lines.append({'name': line.name, 'quantity': line.quantity, 'nmck_unit': line.nmck_unit,
                      'group_key': line.group_key, 'group_name': line.group_name, 'requirements': line.requirements,
                      **{field + '_unit': getattr(line, field + '_unit') for field in COST_FIELDS}})
    from .services import calculation_group_quotes
    group_quotes = calculation_group_quotes(lines)
    for line in estimate.lines.all():
        if line.group_key in group_quotes:
            line.requirements['card_edit']['nmck_known'] = True
            line.save(update_fields=['requirements'])
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
                child = {'source_id': component.source_item_id, 'component_id': component.pk, 'name': component.name,
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
        if line.requirements.get('production', {}).get('manual_plan_id'):
            raise ValueError('Изменяйте цену в действующем маршруте товара.')
        if requirements['production'] == line.requirements.get('production'):
            continue
        from .product_financials import _cost_complete
        production = requirements['production']
        totals = production.get('totals') or {}
        values = {field: _number(totals.get(field + '_unit'), optional=True) for field in COST_FIELDS}
        calculated = {'name': line.name, 'quantity': line.quantity, 'requirements': {'production': production},
                      **{field + '_unit': value or Decimal('0') for field, value in values.items()}}
        complete = all(value is not None for value in values.values()) and _cost_complete(calculated)
        key = f'component-{line.component_id}' if line.component_id else f'good-{line.commercial_item_id}'
        updates[key] = {'values': {field: str(value) if value is not None else None for field, value in values.items()},
                                           'known': complete, 'requirements': {'production': deepcopy(production)}}
    if not updates:
        return estimate
    save_products(user, estimate.tender_id, product_editor(estimate.tender), route_updates=updates, route_only=True)
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


def component_route_source(component):
    source = deepcopy(component.source_item)
    if source is not None and component.commercial_item.provenance.get('card_edit'):
        source.original_text, source.quantity, source.unit = component.name, component.effective_quantity, component.unit
        source.requirements = deepcopy(component.requirements)
    return source


def working_route_snapshot_sources(tender, sources):
    result = {source.pk: source for source in working_route_sources(tender, sources)}
    components = CalculationComponent.objects.filter(commercial_item__tender=tender, commercial_item__status='active',
        commercial_item__provenance__card_edit__isnull=False, status='active').select_related('commercial_item', 'source_item')
    for component in components:
        source = component_route_source(component)
        if source is not None:
            result[source.pk] = source
    return list(result.values())


def manual_step_prices(plan, subject, quantity):
    """Read the existing route price snapshot, never guess links to older steps."""
    edit = (subject.provenance or {}).get('card_edit', {})
    route = edit.get('route_price') or {}
    production = (route.get('requirements') or {}).get('production') or {}
    basis = component_basis(subject) if isinstance(subject, CalculationComponent) else input_basis(subject)
    if production.get('manual_plan_id') != plan.pk or route.get('basis') != basis:
        return {}
    if str(production.get('quantity')) != str(quantity.normalize()):
        return {}
    return {str(cost['step_id']): cost for cost in production.get('costs', [])}


@transaction.atomic
def save_manual_step(user, tender_id, source_id, plan_id, step_id, mode, amount):
    if not user.is_superuser:
        raise PermissionDenied
    tender = Tender.objects.select_for_update().get(pk=tender_id)
    from .calculation_v2_pipeline import source_route_snapshots
    from .models import ComponentRoutePlan
    snapshot = source_route_snapshots(tender).get(source_id) or {}
    if snapshot.get('state') != 'ready' or snapshot.get('plan_id') != plan_id or snapshot.get('pending_changes'):
        raise ValueError('Маршрут изменился или не согласован. Обновите карточку.')
    plan = ComponentRoutePlan.objects.select_for_update().select_related('commercial_item', 'component').get(pk=plan_id)
    good = plan.commercial_item
    if good.status != 'active' or not plan.component_id or plan.component.status != 'active':
        raise ValueError('Товар или компонент не принят.')
    subject = good if good.structure == TenderCommercialItem.Structure.SIMPLE else plan.component
    quantity = good.quantity if subject == good else subject.effective_quantity
    if quantity is None or quantity <= 0:
        raise ValueError('Неизвестно количество товара.')
    steps = snapshot['steps']
    if step_id not in {step['id'] for step in steps} or mode not in ('unit', 'total'):
        raise ValueError('Некорректный этап или способ ввода.')
    value = _number(amount, optional=True)
    if value is not None and value != value.quantize(Decimal('0.01')):
        raise ValueError('Укажите цену с точностью до копейки.')
    from .services import _money
    from .routes import _totals_from_costs
    costs = manual_step_prices(plan, subject, quantity)
    if value is None:
        costs.pop(str(step_id), None)
    else:
        total = _money(value * quantity if mode == 'unit' else value)
        costs[str(step_id)] = {'step_id': step_id, 'amount_total': str(total), 'source': 'Ручной ввод',
            'entry_mode': mode, 'entry_amount': str(value), 'actor_id': user.pk, 'at': timezone.now().isoformat()}
    ordered = [dict(costs[str(step['id'])], process_name=step['name']) for step in steps if str(step['id']) in costs]
    totals = _totals_from_costs(ordered, quantity)
    production = {'stage': 'training_dialogue', 'manual_plan_id': plan.pk, 'quantity': str(quantity.normalize()),
        'route': {'processes': steps}, 'costs': ordered, 'totals': totals}
    key = f'good-{good.pk}' if subject == good else f'component-{subject.pk}'
    update = {'values': {'material': totals['material_unit'], 'application': '0', 'logistics': '0'},
        'known': len(ordered) == len(steps), 'requirements': {'production': production}}
    return save_products(user, tender_id, product_editor(tender), route_updates={key: update}, route_only=True)
