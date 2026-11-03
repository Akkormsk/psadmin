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
    with patch('tender_selection.views.notification_for', side_effect=AssertionError('Read-only snapshot fetched notification')), patch('tender_selection.views.extras_for', return_value=(tender.clarifications_raw or [], tender.complaints_raw or [])), patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Read-only card called model')):
        response = read(reverse('tender_selection:detail', args=[tender.pk]), {'analysis_snapshot': '1', 'full_card': '1'})
        links = html.fromstring(response.content).xpath('//*[@data-source-route]')
        ids = [int(node.get('data-route-source')) for node in links]
        statuses = [read(node.get('data-source-route')).status_code for node in links]
    collections = {}
    for key, path, params in [('hidden', reverse('tender_selection:list'), {'view': 'list', 'all': '1'})]:
        try:
            print('COLLECTION_STARTED ' + key, flush=True)
            collection = read(path, params)
            collections[key] = {'status': collection.status_code, 'bytes': len(collection.content)}
        except Exception as exc:
            collections[key] = {'error_type': type(exc).__name__, 'error': str(exc)}
    from tender_selection.notification import parse_notification
    tree = html.fromstring(response.content)
    rendered_facts = tree.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," ts-chars ")]/li')
    numeric_rows = []
    for item in parse_notification(tender.notification_raw).get('items', []):
        for fact in item.get('characteristics', []):
            if not str(fact.get('value', '')).startswith(('не менее ', 'более ', 'не более ', 'менее ', 'от ', 'до ')):
                continue
            matching = [node for node in rendered_facts if ' '.join(node.itertext()).strip().startswith(fact['name'] + ':') and str(fact['value']) in ' '.join(node.itertext())]
            numeric_rows.append({'name': fact['name'], 'value': fact['value'], 'rendered': bool(matching), 'original_black': any(not node.xpath('.//*[contains(@class,"ts-fact-added")]') for node in matching)})
    return {'numeric_notification_rows': numeric_rows, 'collections': collections, 'card_http_status': response.status_code, 'route_link_count': len(ids), 'unique_route_links': len(set(ids)), 'route_endpoint_statuses': statuses}


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
    from tenders.calculation_v2_pipeline import active_calculation_items, source_route_snapshots
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
        if diagnostics.get('stages'):
            from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, document_analysis_sources, assess_quality
            from unittest.mock import patch
            engine = GatewayDocumentEnricher(max_cost_rub='10')
            sources = list(document_analysis_sources(tender)[0].values())
            with patch('tender_selection.services.documents_html'):
                prepared = engine.prepare_stages(tender, sources, assess_quality(tender, sources)['documents'])
            latest['staged_context'] = {'characters': len(engine.shared_context), 'utf8_bytes': len(engine.shared_context.encode('utf-8')), 'sections': {key: len(json.dumps(value, ensure_ascii=False)) for key, value in json.loads(engine.shared_context).items()}, 'documents': [{key: doc.get(key) for key in ('name', 'image_indexes')} | {'paragraph_chars': len(doc['text']), 'table_chars': len(json.dumps(doc['tables'], ensure_ascii=False)), 'evidence_chars': len(doc['evidence_text'])} for doc in engine.stage_documents]}
            normalize = lambda value: ' '.join(str(value).split()).casefold()
            latest['structure_evidence_check'] = [{'kind': row.get('kind'), 'doc_id': row.get('doc_id'), 'goods': row.get('goods'), 'reason': row.get('reason'), 'quote': row.get('quote'), 'exact_match': normalize(row.get('quote')) in normalize(engine.document_lookup.get(row.get('doc_id'), {}).get('evidence_text', ''))} for row in diagnostics['stages'].get('structure', {}).get('result', [])]
            latest['tz_evidence_check'] = []
            for row in diagnostics['stages'].get('tz', {}).get('result', []):
                for fact in row.get('characteristics', []):
                    doc = engine.document_lookup.get(fact.get('doc_id'), {})
                    quote = normalize(fact.get('quote'))
                    cells = [cell if isinstance(cell, str) else cell['text'] for table in doc.get('tables', []) for line in table['rows'] for cell in line]
                    if quote and quote not in normalize(doc.get('evidence_text', '')):
                        import difflib
                        text = doc.get('evidence_text', '')
                        match = difflib.SequenceMatcher(None, quote, normalize(text)).find_longest_match()
                        latest['tz_evidence_check'].append({'key': row.get('key'), 'name': fact.get('name'), 'quote': fact.get('quote'), 'doc_id': fact.get('doc_id'), 'exact_cell_match': any(quote in normalize(cell) for cell in cells), 'source_near_match': text[max(0,match.b-70):match.b+match.size+70]})
            latest['proposed_items'] = [{'name': item.original_text, 'quantity': item.quantity, 'per_parent': item.metadata.get('quantity_per_parent'), 'supply_kind': item.metadata.get('supply_kind'), 'incomplete': item.metadata.get('analysis_incomplete'), 'facts': effective_item_requirements(item).get('characteristics')} for item in pending]


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
            'document_input_profile': document_input_profile(tender), 'card_route_check': {'skipped': 'Data-only audit avoids GET refresh side effects'}, 'saved_routes': {key: {'state': value.get('state'), 'steps': len(value.get('steps', [])), 'plan_id': value.get('plan_id'), 'assumption': value.get('assumption'), 'working_choice': value.get('interaction')} for key, value in source_route_snapshots(tender).items()}, 'recent_jobs': [{key: row[key] for key in ('id', 'version', 'status', 'started_at', 'completed_at', 'total_cost', 'error')} | {'phase': row['diagnostics'].get('refresh'), 'stages': {key: {'state': value.get('state'), 'reused_from': value.get('reused_from')} for key, value in row['diagnostics'].get('stages', {}).items()}} for row in TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').values('id', 'version', 'status', 'started_at', 'completed_at', 'total_cost', 'error', 'diagnostics')[:8]], 'pending_count': sum(row['changes'] for row in pending_changes), 'pending_no_change_count': sum(not row['changes'] for row in pending_changes), 'pending_review_summary': pending_changes, 'active_items': items}


def collect_saved_status(number):
    from tender_selection.models import Tender
    from tenders.models import TenderComputeJob, TenderSourceItem
    from tenders.calculation_v2_pipeline import source_route_snapshots
    tender = Tender.objects.get(purchase_number=number)
    from django.urls import reverse
    from django.utils import timezone
    from tender_selection.models import FilterSettings
    from tender_selection.services import visible_found_tenders
    settings = FilterSettings.load()
    visible, hidden, expired = visible_found_tenders(Tender.objects.filter(pk=tender.pk), min_price=settings.min_price, include_words=settings.include_words, exclude_words=settings.exclude_words)
    print('TENDER_LOCATION ' + json.dumps({'pk': tender.pk, 'number': number, 'status': tender.status, 'review': tender.review, 'deadline': tender.collecting_finished_at, 'server_time': timezone.now(), 'archived_at': tender.archived_at, 'archived_from_stage': tender.archived_from_stage, 'filters_visible': bool(visible), 'filtered_count': hidden, 'expired_count': expired, 'detail_url': reverse('tender_selection:detail', args=[tender.pk]), 'estimates': list(tender.estimates.values('pk'))}, default=str), flush=True)
    from tenders.models import OwnerInteraction, OwnerFeedbackEvent
    interactions = list(OwnerInteraction.objects.filter(tender=tender).order_by('-pk')[:20])
    print('ROUTE_ANSWERS ' + json.dumps([{'id': q.pk, 'status': q.status, 'source_id': q.source_item_id, 'question': q.question, 'context': q.context, 'options': q.possible_answers, 'answers': list(OwnerFeedbackEvent.objects.filter(interaction=q).values_list('raw_text', flat=True))} for q in interactions], ensure_ascii=False, default=str), flush=True)
    jobs = []
    for job in TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk')[:10]:
        stages = job.diagnostics.get('stages', {})
        jobs.append({'id': job.pk, 'status': job.status, 'completed_at': job.completed_at, 'error': job.error,
            'cost_rub': job.total_cost, 'contract': job.diagnostics.get('intake_contract'),
            'stages': {name: {key: row.get(key) for key in ('state', 'cost_rub', 'reused_from', 'validation_version', 'reserved_cost_rub', 'remaining_budget_rub', 'input_token_bound', 'usage')} for name, row in stages.items()}})
    mismatches = []
    latest = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').first()
    if latest and latest.status == 'partial' and latest.diagnostics.get('stages', {}).get('tz', {}).get('result'):
        from unittest.mock import patch
        from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, document_analysis_sources, assess_quality
        from tenders.document_enrichment import project_source_fact
        engine = GatewayDocumentEnricher(max_cost_rub='0')
        sources = list(document_analysis_sources(tender)[0].values())
        with patch('tender_selection.services.documents_html'):
            engine.prepare_stages(tender, sources, assess_quality(tender, sources)['documents'])
        from tender_selection.notification import _dig, _as_list
        raw_items = _as_list(_dig(tender.notification_raw.get('source', {}), 'notificationInfo', 'purchaseObjectsInfo', 'notDrugPurchaseObjectsInfo', 'purchaseObject', default=[]))
        def named_source_nodes(node, name):
            if isinstance(node, dict):
                if node.get('name') == name:
                    yield node
                for child in node.values():
                    yield from named_source_nodes(child, name)
            elif isinstance(node, list):
                for child in node:
                    yield from named_source_nodes(child, name)
        for row in latest.diagnostics['stages']['tz']['result']:
            source_id = int(row['key'].split(':')[0])
            for fact in row.get('characteristics', []):
                if fact.get('resolution') in {'keep', 'omit'} and not fact.get('baseline_refs') and not fact.get('notification_refs'):
                    mismatches.append({'key': row['key'], 'missing_reference': fact, 'raw_source_facts': list(named_source_nodes(tender.notification_raw, fact.get('name'))),
                        'matching_source_facts': [source for source in engine.baselines[source_id] if source.get('name') == fact.get('name')]})
                grounded = bool((fact.get('doc_id') is not None and str(fact.get('quote') or '').strip()) or fact.get('document_facts') or fact.get('baseline_refs') or fact.get('notification_refs'))
                if not grounded and fact.get('resolution') not in {'keep', 'omit'}:
                    mismatches.append({'key': row['key'], 'unsupported_fact': fact, 'baseline': engine.baselines[source_id], 'raw_characteristics': [(raw_items[(engine.stage_sources[source_id].metadata or {}).get('notification_index', 0)].get('KTRU') or raw_items[(engine.stage_sources[source_id].metadata or {}).get('notification_index', 0)].get('OKPD2') or {}).get('characteristics', {})]})
                    if len(mismatches) >= 3: break
                    continue
                if fact.get('doc_id') is not None or fact.get('document_facts') or fact.get('resolution') in {'keep', 'omit'}:
                    continue
                refs = (fact.get('baseline_refs') or []) + (fact.get('notification_refs') or [])
                if not refs:
                    mismatches.append({'key': row['key'], 'unsupported_fact': fact, 'baseline': engine.baselines[source_id], 'raw_characteristics': [(raw_items[(engine.stage_sources[source_id].metadata or {}).get('notification_index', 0)].get('KTRU') or raw_items[(engine.stage_sources[source_id].metadata or {}).get('notification_index', 0)].get('OKPD2') or {}).get('characteristics', {})]})
                    if len(mismatches) >= 3: break
                original = [engine.baselines[source_id][ref] for ref in refs if type(ref) is int and 0 <= ref < len(engine.baselines[source_id])]
                norm = lambda value: ' '.join(str(value).casefold().split())
                if original and not all(project_source_fact(item, fact.get('name'), fact.get('value'), source_item_id=source_id, reference=item['id']) for item in original):
                    mismatches.append({'key': row['key'], 'name': fact.get('name'), 'value': fact.get('value'), 'resolution': fact.get('resolution'), 'original': original})
                if len(mismatches) >= 5:
                    break
            if len(mismatches) >= 5:
                break
    from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality
    diagnostic_engine = GatewayDocumentEnricher(max_cost_rub='0')
    _, document_states = diagnostic_engine._bounded_documents(assess_quality(tender, [])['documents'], compact=True)
    document_states = [{key: row.get(key) for key in ('name', 'reading_status', 'error', 'warnings', 'cache_kind', 'cached_text_chars', 'sent_text_chars', 'image_count')} for row in document_states]
    pending = list(TenderSourceItem.objects.filter(tender=tender, source_type='document_proposal', is_active=False).values('original_text', 'quantity', 'metadata'))
    return {'number': number, 'read_only': True, 'model_called': False, 'jobs': jobs, 'documents': document_states, 'source_mismatches': mismatches, 'rendered_card': inspect_card_routes(tender),
        'proposals': [{'name': row['original_text'], 'quantity': row['quantity'], 'incomplete': row['metadata'].get('analysis_incomplete')} for row in pending],
        'routes': {key: {'state': value.get('state'), 'steps': len(value.get('steps', [])), 'plan_id': value.get('plan_id'), 'assumption': value.get('assumption'), 'working_choice': value.get('interaction')} for key, value in source_route_snapshots(tender).items()}}


def main():
    print("INTAKE_AUDIT_STARTED", flush=True)
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
        print("INTAKE_AUDIT " + json.dumps(collect_saved_status(number), ensure_ascii=False, default=str), flush=True)


if __name__ == '__main__':
    main()
