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
    from tenders.document_enrichment import document_proposal_changes
    pending = list(tender.v2_source_items.filter(source_type="document_proposal", is_active=False).select_related("parent"))
    pending_changes = [{"name": item.original_text, "changes": bool(document_proposal_changes(item))} for item in pending]
    latest = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').values('version', 'status', 'completed_at', 'error', 'diagnostics').first()
    if latest:
        from types import SimpleNamespace
        from tenders.analysis_presentation import analysis_issues
        latest['issues'] = analysis_issues(SimpleNamespace(**latest))
        diagnostics = latest.pop('diagnostics') or {}
        latest['rejections'] = [entry.get('extraction', {}).get('rejections', []) for entry in diagnostics.get('enrichment', []) if isinstance(entry, dict)]
        latest['enrichment_state'] = diagnostics.get('enrichment_state')
    from collections import Counter
    from types import SimpleNamespace
    from tenders.analysis_presentation import analysis_issues
    from django.db.models import Max
    latest_ids = TenderComputeJob.objects.filter(tender__status__in=[Tender.NEW, Tender.PUSHED], version__startswith='v2').values('tender_id').annotate(latest_id=Max('pk')).values_list('latest_id', flat=True)
    recent = list(TenderComputeJob.objects.filter(pk__in=latest_ids).values('status', 'diagnostics', 'error', 'tender__purchase_number', 'tender__title'))
    failures = Counter()
    for row in recent:
        details = row['diagnostics'] if isinstance(row['diagnostics'], dict) else {}
        entries = details.get('enrichment', [])
        entries = [entries] if isinstance(entries, dict) else entries
        for entry in entries:
            if isinstance(entry, dict):
                for rejection in (entry.get('extraction') or {}).get('rejections', []):
                    failures[str(rejection.get('reason'))] += 1
    return {'failed_tenders': [{'number': row['tender__purchase_number'], 'title': row['tender__title'], 'issues': analysis_issues(SimpleNamespace(**row)), 'usage': [entry.get('usage') for entry in (row['diagnostics'].get('enrichment') or []) if isinstance(entry, dict)]} for row in recent if row['status'] in {'partial', 'failed'}], 'failure_issue_counts': dict(Counter(issue for row in recent if row['status'] in {'partial', 'failed'} for issue in analysis_issues(SimpleNamespace(**row)))), 'latest_job_status_counts': dict(Counter(row['status'] for row in recent)), 'rejection_counts': dict(failures), 'tender_number': number, 'read_only': True, 'model_called': False, 'latest_job': latest,
            'pending_count': sum(row['changes'] for row in pending_changes), 'pending_no_change_count': sum(not row['changes'] for row in pending_changes), 'pending_review_summary': pending_changes, 'active_items': items}


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
