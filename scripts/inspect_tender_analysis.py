"""Operator-only read-only inspection; no HTTP views, document fetches or model calls."""
import json
import os


def collect_report(number):
    from tender_selection.models import Tender
    from tender_selection.notification import parse_notification
    from tenders.calculation_v2_pipeline import active_calculation_items
    from tenders.document_enrichment import effective_item_requirements, notification_origin, notification_quote_source
    from tenders.models import TenderComputeJob

    tender = Tender.objects.get(purchase_number=number)
    notification = parse_notification(tender.notification_raw or {}).get('items', [])
    items = []
    for item in active_calculation_items(tender):
        origin = notification_origin(item)
        position = (origin.metadata or {}).get('notification_index') if origin else None
        original = notification[position] if isinstance(position, int) and 0 <= position < len(notification) else {}
        facts = effective_item_requirements(item).get('characteristics', [])
        items.append({
            'name': item.original_text, 'type': item.source_type, 'relationship': item.metadata.get('relationship'),
            'quantity': item.quantity, 'unit': item.unit, 'notification_position': position + 1 if position is not None else None,
            'parent_name': item.parent.original_text if item.parent_id else None,
            'parent_type': item.parent.source_type if item.parent_id else None,
            'stored_fact_count': len((item.requirements or {}).get('characteristics', [])),
            'effective_fact_count': len(facts), 'effective_facts': facts,
            'same_item_quote_eligible': notification_quote_source(item) is not None,
            'original_name': original.get('name'), 'original_quantity': original.get('quantity'),
            'original_unit_price': original.get('price'), 'original_total': original.get('sum'),
        })
    latest = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').values('version', 'status', 'completed_at', 'error', 'diagnostics').first()
    if latest:
        diagnostics = latest.pop('diagnostics') or {}
        latest['enrichment_state'] = diagnostics.get('enrichment_state')
    return {'tender_number': number, 'read_only': True, 'model_called': False, 'latest_job': latest,
            'pending_count': tender.v2_source_items.filter(source_type='document_proposal', is_active=False).count(), 'active_items': items}


def main():
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
    django.setup()
    from django.db import connection, transaction
    number = os.environ['INSPECT_TENDER_NUMBER']
    if len(number) != 19 or not number.isascii() or not number.isdigit():
        raise ValueError('Expected one purchase number')
    if connection.vendor != 'postgresql':
        raise RuntimeError('Live inspection requires PostgreSQL read-only transaction')
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
        print(json.dumps(collect_report(number), ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
