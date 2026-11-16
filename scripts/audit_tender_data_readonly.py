"""Full TEST storage inventory. Only counts/IDs leave the server; no writes or AI."""
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from django.db import connection, transaction
from django.db.models import Count, F

from tender_selection.models import DocumentPreview, Tender, TenderHistory
from tender_selection.notification import parse_notification
from tenders.models import (
    CalculationComponent, ComponentOperationStep, ComponentRoutePlan, ProviderCalculationQuote,
    TenderCommercialItem, TenderComputeJob, TenderComputeLine, TenderComputeWorkUnit,
    TenderEstimate, TenderLine, TenderSourceItem,
)


def emit(section, value):
    print(json.dumps({section: value}, ensure_ascii=False), flush=True)


def select_only(execute, sql, params, many, context):
    if sql.lstrip().split(None, 1)[0].upper() not in {'SELECT', 'WITH', 'SHOW'}:
        raise RuntimeError('Audit attempted a non-read-only statement')
    return execute(sql, params, many, context)


def audit():
    version = Path('/app/.twin-version').read_text().splitlines()
    emit('environment', {'deployed_sha': next(line[4:] for line in version if line.startswith('sha=')), 'readonly': True})
    emit('counts', {model._meta.model_name: model.objects.count() for model in (
        Tender, TenderHistory, DocumentPreview, TenderSourceItem, TenderCommercialItem,
        CalculationComponent, ComponentRoutePlan, ComponentOperationStep, TenderComputeJob,
        TenderComputeLine, TenderComputeWorkUnit, TenderEstimate, TenderLine, ProviderCalculationQuote,
    )})
    emit('identity', {
        'history_duplicate_groups': TenderHistory.objects.values('source', 'law', 'purchase_number').annotate(n=Count('pk')).filter(n__gt=1).count(),
        'tenders_without_history': Tender.objects.filter(history_record__isnull=True).count(),
        'estimates_without_tender': TenderEstimate.objects.filter(tender__isnull=True).count(),
    })
    sources = {row['pk']: row for row in TenderSourceItem.objects.values('pk', 'tender_id', 'source_type', 'metadata')}
    members, component_sources = defaultdict(list), defaultdict(list)
    foreign_memberships = 0
    for row in TenderCommercialItem.objects.filter(status='active').values('pk', 'tender_id', 'source_items__pk'):
        source = sources.get(row['source_items__pk'])
        if source and source['tender_id'] == row['tender_id']:
            members[source['pk']].append(row['pk'])
        elif source:
            foreign_memberships += 1
    components = {row['pk']: row for row in CalculationComponent.objects.values('pk', 'commercial_item_id', 'source_item_id', 'parent_id', 'status')}
    for row in CalculationComponent.objects.filter(status='active', commercial_item__status='active').values('pk', 'source_item_id'):
        component_sources[row['source_item_id']].append(row['pk'])
    parent_errors, cycles = 0, 0
    for component in components.values():
        parent = components.get(component['parent_id'])
        if parent and parent['commercial_item_id'] != component['commercial_item_id']:
            parent_errors += 1
        seen, current = set(), component
        while current is not None:
            if current['pk'] in seen:
                cycles += 1
                break
            seen.add(current['pk'])
            current = components.get(current['parent_id'])
    emit('relations', {
        'foreign_active_source_memberships': foreign_memberships,
        'foreign_component_sources': CalculationComponent.objects.filter(source_item__isnull=False).exclude(source_item__tender_id=F('commercial_item__tender_id')).count(),
        'foreign_component_parents': parent_errors,
        'components_with_cyclic_ancestry': cycles,
        'foreign_route_components': ComponentRoutePlan.objects.filter(component__isnull=False).exclude(component__commercial_item_id=F('commercial_item_id')).count(),
        'foreign_compute_sources': TenderComputeLine.objects.exclude(source_item__tender_id=F('job__tender_id')).count(),
        'foreign_compute_components': TenderComputeLine.objects.filter(component__isnull=False).exclude(component__commercial_item__tender_id=F('job__tender_id')).count(),
    })
    line_status = Counter()
    for row in TenderLine.objects.values('pk', 'estimate__tender_id', 'requirements').iterator():
        info = row['requirements'] if isinstance(row['requirements'], dict) else {}
        source_id = info.get('source_item_id')
        source = sources.get(source_id) if type(source_id) is int else None
        if row['estimate__tender_id'] is None:
            line_status['without_tender'] += 1
        elif source_id is None:
            line_status['without_source_id'] += 1
        elif source is None:
            line_status['source_missing'] += 1
        elif source['tender_id'] != row['estimate__tender_id']:
            line_status['foreign_source'] += 1
        else:
            metadata = source['metadata'] if isinstance(source['metadata'], dict) else {}
            matches = component_sources[source_id] if source['source_type'] == 'document_component' or metadata.get('relationship') == 'component' else members[source_id]
            line_status['unique_existing_subject' if len(matches) == 1 else 'subject_ambiguous' if matches else 'subject_not_materialized'] += 1
    emit('estimate_line_links', dict(line_status))
    previews = {row['url']: row for row in DocumentPreview.objects.values('url').annotate(content_size=Count('pk'))}
    nonempty = set(DocumentPreview.objects.exclude(html='').values_list('url', flat=True))
    errors = set(DocumentPreview.objects.exclude(error='').values_list('url', flat=True))
    docs, referenced = Counter(), set()
    for row in Tender.objects.values('pk', 'notification_raw').iterator():
        try:
            documents = parse_notification(row['notification_raw'] or {}).get('documents', [])
        except (ValueError, TypeError, KeyError):
            docs['unparseable_notifications'] += 1
            continue
        urls = {doc['url'] for doc in documents if doc.get('url')}
        docs['tenders_with_documents'] += bool(urls)
        docs['tender_document_links'] += len(urls)
        docs['cached_nonempty_links'] += len(urls & nonempty)
        docs['missing_or_empty_links'] += len(urls - nonempty)
        referenced.update(urls)
    docs.update(unique_referenced_urls=len(referenced), missing_preview_urls=len(referenced - set(previews)),
                unreferenced_preview_urls=len(set(previews) - referenced), cached_error_urls=len(errors & referenced))
    emit('documents', dict(docs))
    readings, latest, latest_seen = Counter(), Counter(), set()
    for job in TenderComputeJob.objects.order_by('-pk').values('pk', 'tender_id', 'status', 'diagnostics').iterator():
        if job['tender_id'] not in latest_seen:
            latest_seen.add(job['tender_id'])
            latest[job['status']] += 1
        diagnostic = job['diagnostics'] if isinstance(job['diagnostics'], dict) else {}
        if diagnostic.get('tender_conditions'):
            readings['jobs_with_tender_conditions'] += 1
        state = diagnostic.get('source_reading')
        if not isinstance(state, dict):
            continue
        readings['jobs_with_saved_reading'] += 1
        readings['complete_reading_checkpoints'] += state.get('complete') is True
        records = state.get('records') or {}
        assignments = state.get('assignments') or {}
        if not isinstance(records, dict) or not isinstance(assignments, dict):
            readings['malformed_checkpoints'] += 1
            continue
        readings['reading_records_across_checkpoints'] += len(records)
        for assignment in assignments.values():
            if isinstance(assignment, dict):
                readings['unassigned_across_checkpoints'] += assignment.get('scope') == 'unassigned'
        for record in records.values():
            if not isinstance(record, dict):
                readings['malformed_records'] += 1
                continue
            refs = record.get('source_ids') or []
            if not isinstance(refs, list) or any(type(pk) is not int or pk not in sources or sources[pk]['tender_id'] != job['tender_id'] for pk in refs):
                readings['records_with_invalid_source_refs'] += 1
    emit('latest_job_statuses', dict(latest))
    emit('saved_reading', dict(readings))
    emit('legacy_analysis', {'tenders_with_risk': Tender.objects.exclude(risk_assessment={}).count(),
                             'tenders_with_profile': Tender.objects.exclude(profile_assessment={}).count()})
    emit('foundation_tables_present', sorted(set(connection.introspection.table_names()) & {'tenders_tenderanalysis', 'tender_selection_tenderdocument'}))
    emit('audit_result', 'COMPLETED_READ_ONLY; counts are not semantic acceptance or a transfer plan')


assert os.environ.get('GOSPLAN_CLIENT_ID') == 'test', 'TEST container required'
assert connection.vendor == 'postgresql', 'PostgreSQL required'
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cursor.execute('SHOW transaction_read_only')
        assert cursor.fetchone()[0] == 'on'
    with connection.execute_wrapper(select_only):
        audit()
