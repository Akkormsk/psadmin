"""Operator trial on a quiescent isolated database; no fetch, AI or runtime activation."""
from collections import Counter, defaultdict
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction

from tender_selection.models import DocumentPreview, Tender, TenderDocument
from tender_selection.notification import parse_notification
from .calculation_v2_pipeline import GatewayDocumentEnricher
from .models import (
    CalculationComponent, TenderAnalysis, TenderCommercialItem, TenderComputeJob,
    TenderComputeLine, TenderComputeWorkUnit, TenderLine, TenderSourceItem,
)
from .source_memory import VERSION, SourceMemory, document_blocks, signature, validate_extracted_facts, validate_reading
from .tender_data import document_preview_map


def _fields(obj):
    return {field.attname: getattr(obj, field.attname) for field in obj._meta.concrete_fields}


def _subject(source_id, sources, items_by_source, components_by_source):
    source = sources.get(source_id)
    if source is None or not isinstance(source.metadata, dict):
        return None
    if source.source_type == 'document_component' or (source.metadata or {}).get('relationship') == 'component':
        matches = components_by_source.get(source_id, [])
        return ('component', matches[0]) if len(matches) == 1 else None
    matches = items_by_source.get(source_id, [])
    return ('item', matches[0]) if len(matches) == 1 else None


def _assignments(row, fact_keys, resolve):
    scope = row.get('scope')
    if scope == 'irrelevant':
        return []
    if scope == 'global' and not row.get('source_ids'):
        targets = [{'scope': 'tender'}]
    elif scope == 'item' and row.get('source_ids'):
        subjects = [resolve(pk) for pk in row['source_ids']]
        if all(subjects):
            targets = [{'scope': kind, 'target_id': pk} for kind, pk in dict.fromkeys(subjects)]
        else:
            targets = [{'scope': 'unassigned', 'reason': 'ambiguous_subject'}]
    else:
        targets = [{'scope': 'unassigned', 'reason': row.get('reason') or 'legacy_assignment_unverified'}]
    return [{'fact_key': key, **target} for key in fact_keys for target in targets]


def collect_transfer_plan(*, using, tender_ids=None):
    """SELECT-only plan; private payload contains evidence and must not be logged."""
    query = Tender.objects.using(using).order_by('pk')
    if tender_ids is not None:
        query = query.filter(pk__in=tender_ids)
    plan = {'database': using, 'tender_ids': [], 'previews': [], 'documents': [], 'analyses': [], 'lines': [], 'issues': []}
    observed, preview_changes, original_previews = [], {}, {}

    def issue(code, tender_id, **ids):
        entry = {'code': code, 'tender_id': tender_id, **ids}
        if entry not in plan['issues']:
            plan['issues'].append(entry)

    for tender in query.iterator():
        plan['tender_ids'].append(tender.pk)
        sources = {obj.pk: obj for obj in TenderSourceItem.objects.using(using).filter(tender=tender).order_by('pk')}
        items = list(TenderCommercialItem.objects.using(using).filter(tender=tender).prefetch_related('source_items').order_by('pk'))
        components = list(CalculationComponent.objects.using(using).filter(commercial_item__tender=tender).order_by('pk'))
        by_source, component_sources = defaultdict(list), defaultdict(list)
        memberships = []
        for item in items:
            source_ids = sorted(source.pk for source in item.source_items.all())
            memberships.append([item.pk, source_ids])
            if item.status == 'active':
                for source_id in source_ids:
                    by_source[source_id].append(item.pk)
        active_items = {item.pk for item in items if item.status == 'active'}
        for component in components:
            if component.status == 'active' and component.commercial_item_id in active_items:
                component_sources[component.source_item_id].append(component.pk)

        def resolve(source_id):
            return _subject(source_id, sources, by_source, component_sources)

        documents = parse_notification(tender.notification_raw or {}).get('documents', [])
        previews = document_preview_map(tender, [doc['url'] for doc in documents if doc.get('url')], using=using)
        original_previews.update((preview.pk, preview) for preview in previews.values())
        links = list(TenderDocument.objects.using(using).filter(tender=tender).order_by('pk'))
        analyses = list(TenderAnalysis.objects.using(using).filter(tender=tender).order_by('pk'))
        jobs = TenderComputeJob.objects.using(using).filter(tender=tender).order_by('-pk').iterator()
        lines = list(TenderLine.objects.using(using).filter(estimate__tender=tender).order_by('pk'))
        observed.append(signature([_fields(tender), [_fields(obj) for obj in sources.values()],
                         [_fields(obj) for obj in items], memberships, [_fields(obj) for obj in components],
                         [_fields(obj) for obj in previews.values()], [_fields(obj) for obj in links],
                         [_fields(obj) for obj in analyses], [_fields(obj) for obj in lines]]))
        desired = {}
        for doc in documents:
            url = doc.get('url')
            if not url or url in desired:
                continue
            preview = previews.get(url)
            if preview is None or not preview.html:
                issue('missing_preview', tender.pk, document_index=doc.get('index'))
                continue
            current = [link for link in links if link.source_key == url and link.is_current]
            if current and current[0].preview_id != preview.pk:
                issue('document_target_conflict', tender.pk, preview_id=preview.pk)
                continue
            desired[url] = {'tender_id': tender.pk, 'preview_id': preview.pk, 'source_key': url, 'assignments': []}

        engine = GatewayDocumentEnricher()
        bounded, considered = engine._bounded_documents(documents, compact=True, using=using, tender=tender)
        readable = {doc['url'] for doc in considered if doc['reading_status'] != 'unreadable'}
        lookup = {index: doc for index, doc in enumerate((doc for doc in bounded if doc['url'] in readable), 1)}
        memory = SourceMemory(document_blocks(lookup, engine.document_images), [], 'trial-verification')
        seen = set()
        for job in jobs:
            observed.append(signature(_fields(job)))
            state = (job.diagnostics or {}).get('source_reading') or {}
            if not state:
                continue
            if not isinstance(state, dict) or state.get('version') != VERSION:
                issue('reading_policy_unverified', tender.pk, job_id=job.pk)
                continue
            saved_blocks, source_map = state.get('fragments') or {}, state.get('source_map') or {}
            records, saved_bindings = state.get('records') or {}, state.get('assignments') or {}
            if not all(isinstance(value, dict) for value in (saved_blocks, source_map, records, saved_bindings)):
                issue('reading_validation_failed', tender.pk, job_id=job.pk)
                continue
            rows, bindings = list(records.values()), list(saved_bindings.values())
            counts = Counter(row.get('id') for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str))
            for row in rows:
                identity = row.get('id') if isinstance(row, dict) else None
                if not isinstance(identity, str) or identity in seen:
                    continue
                block = saved_blocks.get(identity)
                if counts[identity] != 1 or not isinstance(block, dict) or memory.blocks.get(identity) != block:
                    issue('stale_reading', tender.pk, job_id=job.pk, fragment_id=identity)
                    continue
                if block.get('image_index'):
                    issue('visual_reading_needs_verification', tender.pk, job_id=job.pk, fragment_id=identity)
                    continue
                source = source_map.get(str(block['doc_id']), source_map.get(block['doc_id'], {}))
                url = source.get('url') if isinstance(source, dict) else None
                if url not in desired or lookup.get(block['doc_id'], {}).get('url') != url:
                    issue('reading_document_unresolved', tender.pk, job_id=job.pk, fragment_id=identity)
                    continue
                try:
                    validate_reading([row], [block], set(sources))
                    validate_extracted_facts([row], [block])
                except ValueError:
                    issue('reading_validation_failed', tender.pk, job_id=job.pk, fragment_id=identity)
                    continue
                preview = previews[url]
                change = preview_changes.setdefault(preview.pk, {
                    'pk': preview.pk, 'structure': deepcopy(preview.structure), 'extraction': deepcopy(preview.extraction),
                })
                if (not isinstance(change['extraction'], dict) or not isinstance(change['structure'], dict)
                        or change['extraction'] and change['extraction'].get('version') != VERSION
                        or change['structure'] and change['structure'].get('version') != VERSION
                        or not isinstance(change['extraction'].get('facts', {}), dict)
                        or not isinstance(change['structure'].get('fragments', {}), dict)):
                    issue('extraction_target_conflict', tender.pk, preview_id=preview.pk)
                    continue
                change['extraction'].setdefault('version', VERSION)
                facts = change['extraction'].setdefault('facts', {})
                fragment_key = signature([VERSION, block['version'], identity])
                extracted = {signature([fragment_key, fact]): {**deepcopy(fact), 'fragment_id': fragment_key} for fact in row['facts']}
                if any(key in facts and facts[key] != value for key, value in extracted.items()):
                    issue('extraction_target_conflict', tender.pk, preview_id=preview.pk)
                    continue
                facts.update(extracted)
                fact_keys = list(extracted)
                occurrences = []
                for occurrence in block.get('occurrences', []):
                    doc = lookup.get(occurrence['doc_id'], {})
                    occurrences.append({key: value for key, value in {**occurrence, 'url': doc.get('url')}.items()
                                        if key in {'url', 'version', 'location', 'structure'}})
                change['structure'].setdefault('version', VERSION)
                fragment = change['structure'].setdefault('fragments', {}).setdefault(fragment_key, {
                    'source_fragment_id': identity, 'version': block['version'], 'location': block['location'], 'occurrences': [],
                })
                for occurrence in occurrences:
                    if occurrence not in fragment['occurrences']:
                        fragment['occurrences'].append(occurrence)
                assigned = [binding for binding in bindings if isinstance(binding, dict) and binding.get('id') == identity]
                if len(assigned) > 1:
                    binding = {'id': identity, 'source_ids': [], 'scope': 'unassigned', 'reason': 'legacy_assignment_unverified'}
                else:
                    binding = assigned[0] if assigned else row
                try:
                    validate_reading([binding], [block], set(sources))
                except ValueError:
                    binding = {'scope': 'unassigned', 'reason': 'legacy_assignment_unverified'}
                assignments = _assignments(binding, fact_keys, resolve)
                for assignment in assignments:
                    if assignment['scope'] == 'unassigned':
                        issue('ambiguous_subject' if assignment['reason'] == 'ambiguous_subject' else 'unresolved_assignment', tender.pk, fragment_id=identity)
                    if assignment not in desired[url]['assignments']:
                        desired[url]['assignments'].append(assignment)
                seen.add(identity)
            if (job.diagnostics or {}).get('tender_conditions'):
                issue('conditions_need_acceptance', tender.pk, job_id=job.pk)

        for url, entry in desired.items():
            matches = [link for link in links if link.source_key == url and link.preview_id == entry['preview_id']]
            if matches:
                existing = matches[0]
                if existing.is_current and existing.assignments == entry['assignments']:
                    continue
                issue('document_target_conflict', tender.pk, document_id=existing.pk)
                continue
            plan['documents'].append(entry)

        for line in lines:
            if line.commercial_item_id or line.component_id:
                line._state.db = using
                try:
                    line.clean()
                except ValidationError:
                    issue('existing_subject_invalid', tender.pk, line_id=line.pk)
                continue
            source_id = line.requirements.get('source_item_id') if isinstance(line.requirements, dict) else None
            if source_id is None:
                issue('line_source_missing', tender.pk, line_id=line.pk)
                continue
            subject = resolve(source_id) if type(source_id) is int else None
            if subject is None:
                issue('ambiguous_subject', tender.pk, line_id=line.pk)
                continue
            kind, pk = subject
            plan['lines'].append({'pk': line.pk, 'commercial_item_id': pk if kind == 'item' else None, 'component_id': pk if kind == 'component' else None})

        for kind, result, timestamp in (
            ('risk', tender.risk_assessment, tender.risk_checked_at),
            ('profile', {'assessment': tender.profile_assessment, 'signal': tender.profile_signal,
                         'confidence': str(tender.profile_confidence) if tender.profile_confidence is not None else None,
                         'reason': tender.profile_reason, 'model': tender.profile_model}, tender.profile_checked_at),
        ):
            if not (tender.risk_assessment if kind == 'risk' else tender.profile_assessment or tender.profile_signal):
                continue
            fingerprint = signature(['legacy-analysis', kind, result, timestamp])
            existing = [obj for obj in analyses if obj.kind == kind and obj.input_fingerprint == fingerprint and obj.commercial_item_id is None and obj.component_id is None]
            if existing:
                if any(obj.result != result for obj in existing):
                    issue('analysis_target_conflict', tender.pk, analysis_id=existing[0].pk)
                continue
            plan['analyses'].append({'tender_id': tender.pk, 'kind': kind, 'result': deepcopy(result),
                                     'input_fingerprint': fingerprint, 'is_current': False,
                                     'basis': [{'legacy_field': 'risk_assessment' if kind == 'risk' else 'profile_assessment',
                                                'checked_at': timestamp.isoformat() if timestamp else None}]})

        if tender.selected_estimate_id is None and tender.estimates.using(using).exists():
            issue('proposal_calculation_not_selected', tender.pk)
        unlinked_lines = list(TenderComputeLine.objects.using(using).filter(job__tender=tender, provider_quote__isnull=True).values('pk', 'result'))
        unlinked_units = list(TenderComputeWorkUnit.objects.using(using).filter(job__tender=tender, provider_quote__isnull=True).values('pk', 'result'))
        observed.append([unlinked_lines, unlinked_units])
        for line in unlinked_lines:
            if 'provider_quote' in (line['result'] or {}):
                issue('provider_quote_unresolved', tender.pk, compute_line_id=line['pk'])
        for unit in unlinked_units:
            if 'provider_quote' in (unit['result'] or {}):
                issue('provider_quote_unresolved', tender.pk, work_unit_id=unit['pk'])

    for pk, change in preview_changes.items():
        preview = original_previews[pk]
        if change['structure'] != preview.structure or change['extraction'] != preview.extraction:
            plan['previews'].append(change)
    plan['digest'] = signature([plan, observed])
    return plan


def transfer_report(plan):
    """Only identifiers, hashes and counts; no documents, facts or customer text."""
    counts = {key: len(plan[key]) for key in ('previews', 'documents', 'analyses', 'lines')}
    return {'database': plan['database'], 'digest': plan['digest'], 'tenders': len(plan['tender_ids']),
            'changes': sum(counts.values()), 'counts': counts, 'issues': plan['issues']}


def apply_transfer_plan(plan, *, using):
    """Apply an unchanged preview on an isolated, quiescent database, retaining legacy fields."""
    if using != plan['database']:
        raise ValueError('База данных не соответствует предварительной проверке.')
    with transaction.atomic(using=using):
        list(Tender.objects.using(using).select_for_update().filter(pk__in=plan['tender_ids']).order_by('pk'))
        preview_ids = {entry['preview_id'] for entry in plan['documents']} | {entry['pk'] for entry in plan['previews']}
        list(DocumentPreview.objects.using(using).select_for_update().filter(pk__in=preview_ids).order_by('pk'))
        list(TenderLine.objects.using(using).select_for_update().filter(pk__in=[entry['pk'] for entry in plan['lines']]).order_by('pk'))
        fresh = collect_transfer_plan(using=using, tender_ids=plan['tender_ids'])
        if fresh['digest'] != plan['digest']:
            raise ValueError('Данные изменились после предварительной проверки; нужен новый отчёт.')
        for entry in fresh['previews']:
            DocumentPreview.objects.using(using).filter(pk=entry['pk']).update(structure=entry['structure'], extraction=entry['extraction'])
        for entry in fresh['documents']:
            link = TenderDocument(**entry)
            link._state.db = using
            link.full_clean()
            link.save(using=using)
        for entry in fresh['analyses']:
            analysis = TenderAnalysis(**entry)
            analysis._state.db = using
            analysis.full_clean()
            analysis.save(using=using)
        for entry in fresh['lines']:
            line = TenderLine.objects.using(using).get(pk=entry['pk'])
            line.commercial_item_id, line.component_id = entry['commercial_item_id'], entry['component_id']
            line.clean()
            line.save(using=using, update_fields=['commercial_item', 'component'])
    return transfer_report(fresh)
