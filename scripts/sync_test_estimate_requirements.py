"""One-off TEST repair. Preview first; apply only the exact reviewed digest."""
import hashlib
import json
import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path


def collect_plan():
    from tender_selection.models import Tender
    from tender_selection.notification import parse_notification
    from tenders.models import TenderEstimate
    from tenders.calculation_v2_pipeline import active_calculation_items
    from tenders.document_enrichment import effective_item_requirements, notification_origin, notification_quote_source, is_item_enrichment
    updates, skipped = [], []
    for estimate in TenderEstimate.objects.select_related('tender').filter(tender__status=Tender.PUSHED, tender__outcome_status=Tender.OUTCOME_DRAFT).order_by('pk'):
        tender = estimate.tender
        sources = active_calculation_items(tender)
        if not any(source.source_type in {'document_extraction', 'document_enrichment'} for source in sources):
            continue
        originals = (parse_notification(tender.notification_raw) if tender.notification_raw else {}).get('items', [])
        by_origin = {}
        for source in sources:
            origin = notification_origin(source, same_item=True)
            if origin:
                by_origin.setdefault((origin.metadata or {}).get('notification_index'), []).append(source)
        lines = list(estimate.lines.order_by('sort_order', 'pk'))
        reason, changes = '', []
        if len(lines) != len(sources):
            reason = 'changed_composition'
        for line in lines if not reason else []:
            info = deepcopy(line.requirements or {})
            source_id = info.get('source_item_id')
            matches = []
            if source_id:
                for source in sources:
                    ancestor, seen = source, set()
                    while ancestor and ancestor.pk not in seen:
                        seen.add(ancestor.pk)
                        if ancestor.pk == source_id:
                            matches.append(source); break
                        ancestor = ancestor.parent if is_item_enrichment(ancestor) and ancestor.parent_id else None
            else:
                matches = by_origin.get(line.sort_order, [])
            if len(matches) != 1:
                reason = 'ambiguous_source_or_components'; break
            source = matches[0]
            origin = notification_origin(source, same_item=True)
            index = (origin.metadata or {}).get('notification_index') if origin else None
            if type(index) is not int or not 0 <= index < len(originals):
                reason = 'missing_original'; break
            original = originals[index]
            if line.quantity != source.quantity or notification_quote_source(source) is None or line.nmck_unit != Decimal(str(original.get('price') or 0)):
                reason = 'changed_quantity_or_price'; break
            if line.name not in {(original.get('name') or 'Позиция')[:500], source.original_text[:500]}:
                reason = 'manual_name'; break
            if any(getattr(line, key) for key in ('material_unit', 'application_unit', 'logistics_unit', 'product_url')) or info.get('production'):
                reason = 'existing_cost_or_route'; break
            requirements = effective_item_requirements(source, strict=True)
            facts = requirements.get('characteristics', [])
            rows = [{**fact, 'label': fact.get('name') or 'Характеристика', 'value': fact.get('value', ''), 'source': 'из извещения' if fact.get('source', 'notification') == 'notification' else 'из документации'} for fact in facts]
            pair = lambda row: (str(row.get('label') or row.get('name') or ''), str(row.get('value', '')))
            original_pairs = {pair(row) for row in original.get('characteristics', []) if row.get('name') or row.get('value')}
            old_pairs = {pair(row) for row in info.get('requirements', [])}
            if old_pairs not in (original_pairs, {pair(row) for row in rows}) or info.get('missing') or info.get('questions'):
                reason = 'manual_or_unresolved_requirements'; break
            old_rows = {pair(row): row for row in info.get('requirements', [])}
            if any(row.get('selected') is False and pair(row) not in {pair(fact) for fact in rows} for row in info.get('requirements', [])):
                reason = 'manual_selection_changed_fact'; break
            rows = [{**old_rows.get(pair(row), {}), **row} for row in rows]
            updated = {**info, 'requirements': rows, 'missing': [], 'questions': [], 'source_name': source.original_text,
                       'source_unit': source.unit, 'source_item_id': source.pk, 'source_requirements': requirements,
                       'source_provenance': source.provenance, 'nmck_unit_unknown': original.get('price') is None}
            if line.name != source.original_text[:500] or info != updated:
                changes.append({'line_id': line.pk, 'estimate_id': estimate.pk, 'before': {'name': line.name, 'requirements': info}, 'after': {'name': source.original_text[:500], 'requirements': updated}})
        if reason:
            skipped.append({'estimate_id': estimate.pk, 'tender_number': tender.purchase_number, 'reason': reason})
        else:
            updates.extend(changes)
    return {'updates': updates, 'skipped': skipped}


def digest(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
    import django
    django.setup()
    from django.conf import settings
    from django.db import transaction
    from tenders.models import TenderEstimate, TenderLine
    mode = os.environ.get('REFRESH_ESTIMATES_MODE', 'preview')
    if mode not in {'preview', 'apply'}:
        raise ValueError('Invalid mode')
    with transaction.atomic():
        if mode == 'apply':
            list(TenderEstimate.objects.select_for_update().values_list('pk', flat=True))
            list(TenderLine.objects.select_for_update().values_list('pk', flat=True))
        plan = collect_plan()
        signature = digest(plan)
        backup = None
        if mode == 'apply':
            if signature != os.environ.get('REFRESH_ESTIMATES_DIGEST'):
                raise ValueError('Plan changed; repeat preview')
            backup = Path(settings.BASE_DIR) / 'private_maintenance_backups' / ('estimate-requirements-' + signature + '.json')
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                json.dump(plan, stream, ensure_ascii=False, default=str)
            for change in plan['updates']:
                TenderLine.objects.filter(pk=change['line_id']).update(**change['after'])
        print(json.dumps({'mode': mode, 'digest': signature, 'updated_lines': len(plan['updates']), 'estimate_ids': sorted({row['estimate_id'] for row in plan['updates']}), 'skipped': plan['skipped'], 'backup_saved': backup is not None, 'model_called': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
