"""Shared document facts, owner-specific bindings and compact job references."""
from copy import deepcopy

from django.db import DEFAULT_DB_ALIAS, transaction

from tender_selection.models import DocumentPreview, TenderDocument, Tender
from .source_memory import VERSION, signature, normalize_reading_scope, validate_reading, validate_extracted_facts
from .tender_data import prepare_tender_for_write, document_preview_map


class DocumentEvidence:
    def __init__(self, job, memory, documents):
        self.job, self.memory = job, memory
        self.using = job._state.db or DEFAULT_DB_ALIAS
        prepare_tender_for_write(job.tender)
        # Capture the exact previews used by the parser, not a later current version.
        self.notification_version = signature(job.tender.notification_raw)
        previews = document_preview_map(job.tender, [doc['url'] for doc in documents.values() if not doc.get('preview_id')], using=self.using)
        self.links = {}
        for doc_id, doc in documents.items():
            preview_id = doc.get('preview_id') or (previews[doc['url']].pk if doc['url'] in previews else None)
            if preview_id is None:
                raise ValueError('Не сохранена версия документа для извлечённых сведений.')
            link, _ = TenderDocument.objects.using(self.using).get_or_create(
                tender_id=job.tender_id, source_key=doc['url'], preview_id=preview_id,
                defaults={'is_current': False})
            self.links[doc_id] = link
        self.reload()

    def reload(self):
        self.previews = DocumentPreview.objects.using(self.using).only('pk', 'structure', 'extraction').in_bulk({link.preview_id for link in self.links.values()})
        for preview in self.previews.values():
            for value in (preview.structure, preview.extraction):
                if not isinstance(value, dict) or value and value.get('version') != VERSION:
                    raise ValueError('Формат сохранённых сведений документа несовместим; исходные данные сохранены.')
        links = TenderDocument.objects.using(self.using).in_bulk({link.pk for link in self.links.values()})
        self.links = {doc_id: links[link.pk] for doc_id, link in self.links.items()}

    @staticmethod
    def fragment_key(block, occurrence):
        return signature([VERSION, occurrence['version'], block['id']])

    def restore_legacy(self, state):
        if state.get('storage') == 'documents' or state.get('version') != VERSION:
            return
        records = {}
        source_ids = {row['source_id'] for row in self.memory.inventory}
        for key, row in state.get('records', {}).items():
            block = self.memory.blocks.get(row.get('id')) if isinstance(row, dict) else None
            if block is None or state.get('fragments', {}).get(row['id']) != block:
                continue
            row = normalize_reading_scope(row, source_ids)
            try:
                validate_reading([row], [block], source_ids)
                validate_extracted_facts([row], [block])
            except ValueError:
                continue
            records[key] = row
        self.memory.restore({**state, 'records': records})

    def restore(self):
        memory = self.memory
        source_ids = {row['source_id'] for row in memory.inventory}
        saved_refs = {ref['id']: ref for ref in (self.job.diagnostics.get('source_reading') or {}).get('records', {}).values() if 'id' in ref}
        fragments, records, assignments = {}, {}, {}
        bindings = {link.pk: {} for link in self.links.values()}
        for link in self.links.values():
            for binding in link.assignments:
                if binding.get('fragment_id'):
                    previous = bindings[link.pk].get(binding['fragment_id'], {})
                    if binding.get('last_job_id', 0) >= previous.get('last_job_id', 0):
                        bindings[link.pk][binding['fragment_id']] = binding
        for identity, block in memory.blocks.items():
            key = memory.key(identity, memory.page_keys[0])
            own_ref = saved_refs.get(identity, {})
            for occurrence in block['occurrences']:
                link = self.links.get(occurrence['doc_id'])
                if link is None:
                    continue
                if own_ref.get('document_id') and own_ref['document_id'] != link.pk:
                    continue
                preview = self.previews[link.preview_id]
                fragment_id = self.fragment_key(block, occurrence)
                fragment = (preview.structure.get('fragments') or {}).get(fragment_id, {})
                if not fragment.get('read') or fragment.get('source_fragment_id') != identity:
                    continue
                facts = preview.extraction.get('facts', {})
                if any(fact_key not in facts for fact_key in fragment.get('fact_keys', [])):
                    continue
                extracted = [{field: value for field, value in facts[fact_key].items() if field != 'fragment_id'}
                             for fact_key in fragment.get('fact_keys', [])]
                binding = None
                if own_ref.get('binding_key'):
                    binding = next((row for row in link.assignments if row.get('binding_key') == own_ref['binding_key']), None)
                else:
                    binding = bindings[link.pk].get(fragment_id)
                binding = binding or {'scope': 'unassigned', 'source_ids': [], 'reason': 'Назначение для этого тендера ещё не проверено.'}
                row = normalize_reading_scope({**binding, 'id': identity, 'facts': extracted}, source_ids)
                for field in ('fragment_id', 'binding_key', 'assignment_policy', 'last_job_id'):
                    row.pop(field, None)
                try:
                    validate_reading([row], [block], source_ids)
                    validate_extracted_facts([row], [block])
                except ValueError:
                    continue
                policy = binding.get('assignment_policy')
                # An old address is not a verified binding after its source disappeared.
                assigned = policy and row.get('scope') == binding.get('scope') and row.get('source_ids') == binding.get('source_ids')
                fragments[identity], records[key] = block, row
                if assigned:
                    assignments.setdefault(policy, {})[key] = {field: value for field, value in row.items() if field != 'facts'}
                break
        memory.restore({'version': VERSION, 'fragments': fragments, 'records': records})
        for policy, rows in assignments.items():
            memory.restore({'version': VERSION, 'fragments': fragments, 'records': {},
                            'assignment_policy': policy, 'assignments': rows})

    def publish(self):
        memory, using = self.memory, self.using
        state = memory.snapshot()
        refs, assigned_refs = {}, {}
        with transaction.atomic(using=using):
            previews = {preview.pk: preview for preview in DocumentPreview.objects.using(using).select_for_update().filter(
                pk__in=sorted(self.previews)).only('pk', 'structure', 'extraction').order_by('pk')}
            links = {link.pk: link for link in TenderDocument.objects.using(using).select_for_update().filter(
                pk__in=sorted({link.pk for link in self.links.values()})).order_by('pk')}
            changed_previews, changed_links = set(), set()
            binding_keys = {link.pk: {row.get('binding_key'): row for row in link.assignments} for link in links.values()}
            for key, row in state['records'].items():
                block = memory.blocks[row['id']]
                validate_reading([row], [block], {item['source_id'] for item in memory.inventory})
                validate_extracted_facts([row], [block])
                binding = memory.assignments.get(key) if memory.assignment_policy else None
                binding = {field: deepcopy(value) for field, value in (binding or row).items() if field not in {'facts', 'id'}}
                if key in memory.assignments:
                    binding['assignment_policy'] = memory.assignment_policy
                for occurrence in block['occurrences']:
                    original_link = self.links[occurrence['doc_id']]
                    link, preview = links[original_link.pk], previews[original_link.preview_id]
                    if any(value and value.get('version') != VERSION for value in (preview.structure, preview.extraction)):
                        raise ValueError('Формат сохранённых сведений документа несовместим; исходные данные сохранены.')
                    fragment_id = self.fragment_key(block, occurrence)
                    source_facts = row['facts']
                    if not source_facts and row['scope'] == 'irrelevant' and block.get('text') and not block.get('image_index'):
                        # Relevance belongs to the tender; the original text remains reusable.
                        source_facts = [{'quote': block['text']}]
                    extracted = {signature([fragment_id, fact]): {**deepcopy(fact), 'fragment_id': fragment_id} for fact in source_facts}
                    fragment = {'source_fragment_id': block['id'], 'version': occurrence['version'],
                                'location': occurrence['location'], 'read': True, 'fact_keys': list(extracted)}
                    structure, extraction = preview.structure, preview.extraction
                    structure.setdefault('version', VERSION)
                    extraction.setdefault('version', VERSION)
                    existing = structure.setdefault('fragments', {}).get(fragment_id)
                    if existing and existing.get('read') and existing.get('fact_keys') and existing['fact_keys'] != fragment['fact_keys']:
                        raise ValueError('Для этой версии уже сохранено другое извлечение; повтор не заменил сведения.')
                    occurrences = list((existing or {}).get('occurrences', []))
                    address = {field: value for field, value in occurrence.items()
                               if field in {'version', 'location', 'structure', 'image_digest'}}
                    address['url'] = link.source_key
                    if address not in occurrences:
                        occurrences.append(address)
                    fragment['occurrences'] = occurrences
                    fragment['location'] = (existing or {}).get('location', fragment['location'])
                    merged = {**(existing or {}), **fragment}
                    facts = extraction.setdefault('facts', {})
                    if existing != merged or any(facts.get(key) != value for key, value in extracted.items()):
                        structure['fragments'][fragment_id] = merged
                        facts.update(extracted)
                        changed_previews.add(preview.pk)
                    entry = {**binding, 'fragment_id': fragment_id}
                    entry['binding_key'] = signature(entry)
                    saved = binding_keys[link.pk].get(entry['binding_key'])
                    if saved is None:
                        entry['last_job_id'] = self.job.pk
                        link.assignments.append(entry)
                        binding_keys[link.pk][entry['binding_key']] = entry
                        changed_links.add(link.pk)
                    elif saved.get('last_job_id', 0) < self.job.pk:
                        saved['last_job_id'] = self.job.pk
                        changed_links.add(link.pk)
                    if key not in refs:
                        refs[key] = {'id': row['id'], 'document_id': link.pk, 'fragment_id': fragment_id, 'binding_key': entry['binding_key']}
                        if key in memory.assignments:
                            assigned_refs[key] = refs[key]
            for pk in changed_previews:
                previews[pk].save(using=using, update_fields=['structure', 'extraction'])
            for pk in changed_links:
                link = links[pk]
                link.preview = previews[link.preview_id]
                link.full_clean()
                link.save(using=using, update_fields=['assignments', 'updated_at'])
        self.previews = previews
        self.links = {doc_id: links[link.pk] for doc_id, link in self.links.items()}
        result = {field: value for field, value in state.items() if field not in {'fragments', 'records', 'assignments'}}
        return {**result, 'storage': 'documents', 'records': refs, 'assignments': list(assigned_refs)}

    def save_conditions(self, *, complete=True):
        memory = self.memory
        if not memory.snapshot()['complete'] or memory.pending_assignments():
            raise ValueError('Чтение или привязка документов не завершены; общие условия не заменены.')
        refs = self.publish()['records']
        conditions = {purpose: [] for purpose in ('delivery', 'conditions')}
        for purpose in conditions:
            for block in memory.context(set(), purpose):
                ref = refs[memory.key(block['fragment_id'], memory.page_keys[0])]
                conditions[purpose].append({key: ref[key] for key in ('document_id', 'fragment_id')})
        # Completed work on an earlier document remains historical, never current.
        with transaction.atomic(using=self.using):
            tender = Tender.objects.using(self.using).select_for_update().get(pk=self.job.tender_id)
            documents = list(TenderDocument.objects.using(self.using).select_for_update().filter(
                pk__in=[link.pk for link in self.links.values()]).order_by('pk').values('is_current', 'assignments'))
            superseded = any(row.get('last_job_id', 0) > self.job.pk for doc in documents for row in doc['assignments'])
            if complete and documents and signature(tender.notification_raw) == self.notification_version and all(doc['is_current'] for doc in documents) and not superseded:
                from .input_freshness import conditions_fingerprint
                changed = conditions_fingerprint(tender.conditions) != conditions_fingerprint(conditions)
                tender.conditions = conditions
                tender.save(using=self.using, update_fields=['conditions'])
                self.job.tender.conditions = conditions
                if changed:
                    from .tender_data import mark_tender_inputs_changed
                    mark_tender_inputs_changed(self.job.tender)
        return conditions


def condition_context(tender, job=None, *, current=True):
    """Resolve references for the existing card; old archive snapshots stay readable."""
    conditions = tender.conditions if current and tender.conditions else (
        job.diagnostics.get('tender_conditions', tender.conditions) if job else tender.conditions)
    conditions = conditions or {}
    refs = [ref for rows in conditions.values() if isinstance(rows, list) for ref in rows]
    ids = {ref['document_id'] for ref in refs if isinstance(ref, dict) and 'document_id' in ref}
    if not ids:
        return conditions
    query = TenderDocument.objects.using(tender._state.db or DEFAULT_DB_ALIAS).filter(tender=tender, pk__in=ids)
    if current:
        query = query.filter(is_current=True)
    links = {link.pk: link for link in query.select_related('preview').only('pk', 'source_key', 'preview_id', 'preview__structure', 'preview__extraction')}
    from tender_selection.notification import parse_notification
    documents = {doc['url']: {'index': index, 'name': doc['name']} for index, doc in enumerate(
        parse_notification(tender.notification_raw or {}).get('documents', []))}
    result = {purpose: [] for purpose in conditions}
    for purpose, rows in conditions.items():
        for ref in rows:
            if 'document_id' not in ref:
                result[purpose].append(ref)
                continue
            link = links.get(ref['document_id'])
            if link is None:
                continue
            fragment = link.preview.structure.get('fragments', {}).get(ref.get('fragment_id'), {})
            facts = link.preview.extraction.get('facts', {})
            result[purpose].append({'section': fragment.get('location', ''), 'document': documents.get(link.source_key),
                'occurrences': fragment.get('occurrences', []),
                'facts': [{field: value for field, value in facts[key].items() if field != 'fragment_id'}
                          for key in fragment.get('fact_keys', []) if key in facts]})
    return result
