"""Operator-only read-only inspection; no HTTP views, document fetches or model calls."""
import json
import os


def incomplete_response_summary(diagnostics):
    """Inspect saved output structure without publishing customer text in CI logs."""
    import re
    entries = diagnostics.get('enrichment') or []
    entries = [entries] if isinstance(entries, dict) else entries
    result = []
    for entry in entries:
        evidence = entry.get('incomplete_response') or {}
        if not evidence:
            continue
        text = evidence.get('content') or ''
        summary = {key: value for key, value in evidence.items() if key != 'content'}
        summary['captured_characters'] = len(text)
        summary['json_key_counts'] = {key: len(re.findall(r'"' + key + r'"\s*:', text)) for key in ('items', 'parent_source_item_id', 'characteristics', 'name', 'value', 'keep_refs', 'baseline_refs', 'document_facts', 'reason', 'evidence')}
        # Complete response rows can be decoded even when the final row is cut off.
        start = re.search(r'"items"\s*:\s*\[', text)
        rows, offset = [], start.end() if start else len(text)
        decoder = json.JSONDecoder()
        while offset < len(text):
            offset += len(text[offset:]) - len(text[offset:].lstrip(' \r\n\t,'))
            try:
                row, offset = decoder.raw_decode(text, offset)
            except ValueError:
                break
            if not isinstance(row, dict):
                break
            facts = (row.get('requirements') or {}).get('characteristics') or []
            rows.append({'characters': len(json.dumps(row, ensure_ascii=False)),
                'field_characters': {key: len(json.dumps(value, ensure_ascii=False)) for key, value in row.items()},
                'fact_count': len(facts),
                'unique_facts': len({json.dumps(fact, ensure_ascii=False, sort_keys=True) for fact in facts})})
        summary['complete_rows'] = rows
        result.append(summary)
    return result


def inspect_card_routes(tender):
    """Render the real read-only card and exercise its route endpoints; emit counts only."""
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory
    from django.urls import reverse, resolve
    from lxml import html
    from unittest.mock import patch
    user = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by('pk').first()
    if not user:
        return {'administrator_available': False}
    factory = RequestFactory()
    def request(path, data=None):
        value = factory.get(path, data=data or {}, secure=True, HTTP_HOST='test.admin.psodin.ru')
        value.user, value.session = user, {}
        return value
    def read(path, data=None):
        match = resolve(path)
        return match.func(request(path, data), *match.args, **match.kwargs)
    with patch('tender_selection.views.notification_for', side_effect=AssertionError('Read-only snapshot fetched notification')), patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Read-only card called model')):
        response = read(reverse('tender_selection:detail', args=[tender.pk]), {'analysis_snapshot': '1', 'full_card': '1'})
        links = html.fromstring(response.content).xpath('//*[@data-source-route]')
        ids = [int(node.get('data-route-source')) for node in links]
        statuses = [read(node.get('data-source-route')).status_code for node in links]
    return {'card_http_status': response.status_code, 'route_link_count': len(ids), 'unique_route_links': len(set(ids)), 'route_endpoint_statuses': statuses}


def document_input_profile(tender):
    """Measure the existing prompt from cached data; never fetch or call a model."""
    from unittest.mock import patch
    from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, active_calculation_items, assess_quality
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, default=str))
    report = {}
    def capture(prompt, **kwargs):
        offset = prompt.index('{"notification_items":')
        payload = json.loads(prompt[offset:])
        report.update(prompt_characters=len(prompt), prompt_utf8_bytes=len(prompt.encode('utf-8')),
            instruction_characters=offset, section_characters={key: size(value) for key, value in payload.items()})
        report['documents'] = []
        for doc in payload['documents']:
            cells = [cell for table in doc.get('tables', []) for row in table['rows'] for cell in row if cell]
            report['documents'].append({'text_characters': len(doc['text']), 'table_json_characters': size(doc.get('tables', [])),
                'table_cell_characters': sum(map(len, cells)), 'cell_characters_found_in_text': sum(len(cell) for cell in cells if cell in doc['text'])})
        raw = payload.get('notification_payload') or {}
        source = raw.get('source') or raw
        report['notification_sections'] = {key: size(value) for key, value in source.items()}
        return {'items': []}, {}
    sources = active_calculation_items(tender)
    with patch('tender_selection.services.documents_html'), patch('tenders.services._ai_gateway_json', side_effect=capture):
        GatewayDocumentEnricher().extract_tender(tender=tender, source_items=sources, documents=assess_quality(tender, sources)['documents'])
    return report


def collect_report(number):
    from tender_selection.models import Tender
    from tender_selection.notification import parse_notification
    from tenders.calculation_v2_pipeline import active_calculation_items
    from tenders.document_enrichment import effective_item_requirements, notification_origin, notification_quote_source
    from tenders.models import TenderComputeJob

    tender = Tender.objects.get(purchase_number=number)
    import re
    national_fields = []
    def inspect_fields(value, path="$", relevant=False):
        if len(national_fields) >= 80:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                hit = relevant or bool(re.search(r"national|origin|admission|restrict|prefer|prohib|foreign|advantage|нацреж|происхожд|огранич|запрет", key, re.I))
                inspect_fields(child, path + "." + key, hit)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_fields(child, path + f"[{index}]", relevant)
        elif relevant:
            national_fields.append({"path": path, "value": str(value)[:500]})
    inspect_fields(tender.notification_raw or {})
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
        latest['incomplete_response_summary'] = incomplete_response_summary(diagnostics)
        latest['enrichment_metrics'] = [{key: entry.get(key) for key in ('outcome', 'model', 'reasoning_effort', 'thinking', 'duration_ms', 'cost_rub')} | {
            'usage': entry.get('usage') or entry.get('extraction', {}).get('usage'),
            'accepted_count': entry.get('extraction', {}).get('accepted_count'),
        } for entry in diagnostics.get('enrichment', []) if isinstance(entry, dict)]
        latest['rejections'] = [entry.get('extraction', {}).get('rejections', []) for entry in diagnostics.get('enrichment', []) if isinstance(entry, dict)]
        latest['enrichment_state'] = diagnostics.get('enrichment_state')
        latest['steps'] = {name: {key: row.get(key) for key in ('state', 'model', 'usage', 'duration_ms', 'started_at', 'cost_rub', 'actual_cost_rub', 'billed_cache_read_tokens', 'billed_cache_write_tokens', 'reused_from', 'billing_request_id')} for name, row in diagnostics.get('stages', {}).items()}
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
    return {'failed_tenders': [{'number': row['tender__purchase_number'], 'title': row['tender__title'], 'issues': analysis_issues(SimpleNamespace(**row)), 'usage': [entry.get('usage') for entry in (row['diagnostics'].get('enrichment') or []) if isinstance(entry, dict)]} for row in recent if row['status'] in {'partial', 'failed'}], 'failure_issue_counts': dict(Counter(issue for row in recent if row['status'] in {'partial', 'failed'} for issue in analysis_issues(SimpleNamespace(**row)))), 'latest_job_status_counts': dict(Counter(row['status'] for row in recent)), 'rejection_counts': dict(failures), 'tender_number': number, 'national_regime_fields': national_fields, 'read_only': True, 'model_called': False, 'latest_job': latest,
            'document_input_profile': document_input_profile(tender), 'card_route_check': inspect_card_routes(tender), 'pending_count': sum(row['changes'] for row in pending_changes), 'pending_no_change_count': sum(not row['changes'] for row in pending_changes), 'pending_review_summary': pending_changes, 'active_items': items}


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
