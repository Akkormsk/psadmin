"""Bounded source fragments and durable reading state, independent of AI provider."""
import hashlib
import json

VERSION = 'source-memory-v2'
MAX_INPUT_BYTES = 24000


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode())


def text_parts(text, limit=3000):
    """Split at whitespace, retaining every character and a small boundary overlap."""
    while len(text.encode()) > limit:
        end = min(len(text), limit // 4)
        boundary = text.rfind(' ', 0, end)
        end = boundary if boundary > end // 2 else end
        yield text[:end]
        # The previous ending remains available when a condition crosses a boundary.
        text = text[max(1, end - 80):]
    if text:
        yield text


def packets(values, limit=MAX_INPUT_BYTES):
    batch = []
    for value in values:
        if size([value]) > limit:
            raise ValueError('Фрагмент превышает размер задания; требуется разбиение источника.')
        if batch and size(batch + [value]) > limit:
            yield batch
            batch = []
        batch.append(value)
    if batch:
        yield batch


def document_blocks(documents, images=()):
    """Keep content addresses; a changed document invalidates its dependent reading."""
    from lxml import html
    blocks, occurrences = [], {}
    for doc_id, doc in documents.items():
        version = signature([doc.get('source_html'), doc.get('evidence_text'), doc.get('tables'), [signature(images[i - 1]) for i in doc.get('image_indexes', [])]])
        root = html.fragment_fromstring(doc.get('source_html') or '<div/>', create_parent='div')
        entries, section, paragraph_no, table_no = [], '', 0, 0
        table_contexts, structures, headings = {}, {}, []
        tables = doc.get('tables', [])
        def add_table(number, heading):
            table = tables[number - 1]
            for row_no, row in enumerate(table.get('rows', []), 1):
                location = f'{heading} / Таблица {number}, строка {row_no}'
                table_contexts[location] = {'table_start': table.get('rows', [])[:1], 'row_cells': row}
                structures[location] = {'headings': [label for _, label in headings], 'table': number, 'row': row_no}
                entries.append(('table', location, ' | '.join(cell if isinstance(cell, str) else str(cell.get('text', '')) for cell in row)))
        for node in root.xpath('.//p[not(ancestor::table)] | .//li[not(ancestor::table) and not(.//p)] | .//h1 | .//h2 | .//h3 | .//h4 | .//table[not(ancestor::table)]'):
            if node.tag == 'table':
                table_no += 1
                if table_no <= len(tables):
                    add_table(table_no, section)
                continue
            text = ' '.join(node.itertext()).strip()
            level = int(node.tag[1]) if node.tag in {'h1', 'h2', 'h3', 'h4'} else int(node.get('data-outline-level')) if node.get('data-outline-level', '').isdigit() else None
            if level is not None:
                headings = [(depth, label) for depth, label in headings if depth < level] + [(level, text)]
                section = text[:160]
            if text:
                paragraph_no += 1
                location = f'{section} / Абзац {paragraph_no}'
                structures[location] = {'headings': [label for _, label in headings]}
                entries.append(('paragraph', location, text))
        outside = ' '.join(root.xpath('.//text()[not(ancestor::p or ancestor::li or ancestor::h1 or ancestor::h2 or ancestor::h3 or ancestor::h4 or ancestor::table)]')).strip()
        if entries and outside:
            entries.append(('paragraph', 'Прочий текст документа', outside))
        if not entries and doc.get('text'):
            entries.append(('paragraph', 'Абзац 1', doc['text']))
        for number in range(table_no + 1, len(tables) + 1):
            add_table(number, section)
        for kind, location, text in entries:
            parts = [text] if kind == 'table' else text_parts(text)
            for part, fragment in enumerate(parts, 1):
                anchor = signature([doc['url'], kind, fragment])
                occurrence = occurrences.get(anchor, 0)
                occurrences[anchor] = occurrence + 1
                blocks.append({'id': f'{anchor}:{occurrence}', 'version': version, 'doc_id': doc_id, 'location': f'{location}, часть {part}', 'text': fragment, 'structure': structures.get(location, {}), **({'table_context': table_contexts[location]} if location in table_contexts else {})})
        image_indexes = doc.get('image_indexes', [])
        pages = doc.get('visual_pages', [])
        for offset, image_no in enumerate(image_indexes):
            location = f'Изображение {image_no}'
            if len(pages) == len(image_indexes):
                location = f'Страница {pages[offset]} / {location}'
            blocks.append({'id': signature([doc['url'], 'image', image_no, signature(images[image_no - 1])]), 'doc_id': doc_id,
                'version': version, 'location': location, 'image_index': image_no, 'image_digest': signature(images[image_no - 1]), 'text': ''})
    return blocks


def normalize_reading_scope(row, source_ids):
    """An unknown association is unresolved evidence, not an invented item link."""
    if not isinstance(row, dict) or row.get('scope') != 'item':
        return row
    refs = row.get('source_ids')
    if not isinstance(refs, list) or not refs or any(type(ref) is not int or ref not in source_ids for ref in refs):
        return {**row, 'scope': 'unassigned', 'source_ids': [],
                'reason': 'Связь с конкретной позицией не подтверждена; сохранены исходные сведения.'}
    return row


def validate_reading(rows, blocks, source_ids):
    if not isinstance(rows, list):
        raise ValueError('Не получено состояние прочитанных фрагментов.')
    expected = {block['id'] for block in blocks}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] not in expected or row['id'] in seen:
            raise ValueError('Ответ чтения содержит лишний или повторный фрагмент.')
        seen.add(row['id'])
        refs = row.get('source_ids')
        if not isinstance(refs, list) or any(type(ref) is not int or ref not in source_ids for ref in refs):
            raise ValueError('Фрагмент связан с неизвестной позицией.')
        if not isinstance(row.get('scope'), str) or row['scope'] not in {'item', 'global', 'unassigned', 'irrelevant'}:
            raise ValueError('Не определена область применения фрагмента.')
        if row['scope'] == 'item' and not refs:
            raise ValueError('Не указана позиция для требования.')
        if row['scope'] == 'irrelevant' and (not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('Исключение фрагмента не объяснено.')
    if seen != expected:
        raise ValueError('Часть фрагментов пропущена при чтении.')
    return rows


def validate_extracted_facts(rows, blocks):
    by_id = {block['id']: block for block in blocks}
    normalize = lambda text: ' '.join(str(text).casefold().split())
    for row in rows:
        facts = row.get('facts')
        if not isinstance(facts, list) or (row['scope'] != 'irrelevant' and not facts):
            raise ValueError('Не получены факты источника; исходный блок остаётся необработанным.')
        block = by_id[row['id']]
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get('quote'), str) or not fact['quote'].strip():
                raise ValueError('Факт не связан с исходной цитатой.')
            if not block.get('image_index') and normalize(fact['quote']) not in normalize(block['text']):
                raise ValueError('Цитата факта отсутствует в исходном блоке.')


class SourceMemory:
    """Read source content once; retain occurrences separately from applicability."""
    def __init__(self, blocks, inventory, model, source_versions=None):
        self.blocks = {}
        for block in blocks:
            identity = signature([block.get('text'), block.get('table_context'), block.get('image_digest'), bool(block.get('image_index'))])
            occurrence = {key: value for key, value in block.items() if key not in {'text', 'occurrences'}}
            if identity not in self.blocks:
                self.blocks[identity] = {**block, 'id': identity, 'occurrences': []}
            self.blocks[identity]['occurrences'].append(occurrence)
        # A single compact inventory, never blocks × inventory pages.
        self.pages = [inventory]
        self.page_keys = [signature([VERSION, model, inventory, source_versions or {}])]
        self.records = {}
        self.inventory = inventory
        self.assignment_states = []
        self.assignment_policy = None
        self.assignments = {}

    def restore(self, state):
        if state.get('version') == VERSION:
            self.records.update({key: row for key, row in state.get('records', {}).items() if key not in self.records})
            source_ids = {row['source_id'] for row in self.inventory}
            # Reading facts survive an inventory correction; applicability is checked separately.
            for row in state.get('records', {}).values():
                identity = row.get('id')
                block = self.blocks.get(identity)
                if block is None or state.get('fragments', {}).get(identity) != block:
                    continue
                key = self.key(identity, self.page_keys[0])
                if key in self.records:
                    continue
                row = normalize_reading_scope(row, source_ids)
                try:
                    validate_reading([row], [block], source_ids)
                    validate_extracted_facts([row], [block])
                except ValueError:
                    continue
                self.records[key] = row
            if state.get('assignment_policy'):
                rows = {}
                for row in state.get('assignments', {}).values():
                    identity = row.get('id')
                    block = self.blocks.get(identity)
                    if block is None or state.get('fragments', {}).get(identity) != block:
                        continue
                    try:
                        validate_reading([row], [block], source_ids)
                    except ValueError:
                        continue
                    rows[self.key(identity, self.page_keys[0])] = row
                self.assignment_states.append((state['assignment_policy'], rows))
                if self.assignment_policy is None:
                    self.assignment_policy = state['assignment_policy']
                    self.assignments = dict(state.get('assignments', {}))

    def key(self, block_id, page_key):
        block = self.blocks[block_id]
        return signature([block_id, page_key, [(o.get('doc_id'), o.get('version')) for o in block['occurrences']]])

    def pending(self, limit=None):
        page, page_key = self.pages[0], self.page_keys[0]
        missing = (block for block in self.blocks.values() if self.key(block['id'], page_key) not in self.records)
        for batch in packets(missing, limit) if limit is not None else [list(missing)]:
            if batch:
                yield page, page_key, batch

    def save(self, rows, blocks, page, page_key):
        validate_reading(rows, blocks, {source['source_id'] for source in page})
        for row in rows:
            self.records[self.key(row['id'], page_key)] = row

    def snapshot(self):
        current = {self.key(block_id, self.page_keys[0]) for block_id in self.blocks}
        records = {key: row for key, row in self.records.items() if key in current}
        return {'version': VERSION, 'fragments': self.blocks, 'source_map': getattr(self, 'source_map', {}),
            'records': records, 'total': len(current), 'read': len(records),
            'complete': len(current) == len(records), 'sources': len(self.blocks),
                'assignment_policy': self.assignment_policy, 'assignments': self.assignments}

    def begin_assignment(self, policy):
        self.assignment_policy = signature(['item-evidence-v1', policy])
        self.assignments = {}
        for saved_policy, rows in self.assignment_states:
            for key, row in rows.items():
                if saved_policy == self.assignment_policy or row.get('scope') in {'item', 'global'}:
                    self.assignments.setdefault(key, row)
        current = {self.key(identity, self.page_keys[0]) for identity in self.blocks}
        self.assignments = {key: row for key, row in self.assignments.items() if key in current}
        for identity in self.blocks:
            key = self.key(identity, self.page_keys[0])
            row = self.records.get(key, {})
            if row.get('scope') == 'item' and row.get('source_ids'):
                self.assignments.setdefault(key, {**row, 'purposes': ['structure', 'tz', 'national_regime', 'delivery', 'conditions']})

    def pending_assignments(self):
        return [block for identity, block in self.blocks.items()
                if self.records.get(self.key(identity, self.page_keys[0]), {}).get('scope') != 'irrelevant'
                and self.key(identity, self.page_keys[0]) not in self.assignments]

    def assign(self, rows, blocks):
        validate_reading(rows, blocks, {row['source_id'] for row in self.inventory})
        for row in rows:
            purposes = row.get('purposes')
            if row['scope'] == 'unassigned' and (not isinstance(row.get('reason'), str) or not row['reason'].strip()):
                raise ValueError('Для неясной привязки требуется объяснение.')
            if row['scope'] == 'global' and row['source_ids']:
                raise ValueError('Общие условия не должны подменять отдельную позицию.')
            if row['scope'] == 'irrelevant' or not isinstance(purposes, list) or not purposes or any(
                    not isinstance(purpose, str) or purpose not in {'structure', 'tz', 'national_regime', 'delivery', 'conditions'} for purpose in purposes):
                raise ValueError('Не определено назначение сведений позиции.')
        for row in rows:
            self.assignments[self.key(row['id'], self.page_keys[0])] = row

    def relevant(self, source_ids, purpose=None):
        if self.assignment_policy is not None and (self.pending_assignments() or any(r['scope'] == 'unassigned' for r in self.assignments.values())):
            raise ValueError('Привязка источников к позициям не завершена; исходные сведения сохранены.')
        for block_id, block in self.blocks.items():
            row = self.records.get(self.key(block_id, self.page_keys[0]))
            if not row:
                # Unread material can never be treated as evidence of absence.
                yield block
                continue
            if self.assignment_policy is not None:
                row = self.assignments.get(self.key(block_id, self.page_keys[0]), row)
                if purpose is not None and purpose not in row.get('purposes', []):
                    continue
            if row['scope'] in {'global', 'unassigned'} or set(row['source_ids']).intersection(source_ids):
                yield block

    def context(self, source_ids, purpose=None):
        """Only verified extracted facts; originals remain addressable by block ID."""
        for block in self.relevant(source_ids, purpose):
            row = self.records.get(self.key(block['id'], self.page_keys[0]))
            if not row:
                raise ValueError('Источник ещё не обработан; факты не готовы.')
            yield {'fragment_id': block['id'], 'doc_id': block['doc_id'], 'section': block['location'],
                   'facts': row.get('facts', []), 'occurrences': [{key: value for key, value in occurrence.items() if key in {'doc_id', 'location', 'structure'}} for occurrence in block['occurrences']]}


def token_bound(value):
    """UTF-8 byte upper bound, not an advertised exact provider token count."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode())


def plan_packets(values, fits):
    """Greedy atomic packets sized by the full request, never by product count."""
    batch = []
    for value in values:
        if batch and not fits(batch + [value]):
            yield batch
            batch = []
        if not fits([value]):
            raise ValueError('Один связанный блок не помещается в доступный контекст/бюджет.')
        batch.append(value)
    if batch:
        yield batch

class RequestLimits:
    """Provider limits and an uncached reservation; no token count claimed as exact."""
    def __init__(self, model, remaining, output_limit):
        from django.conf import settings
        from .gateway_budget import model_catalog
        override = getattr(settings, 'CALCULATION_V2_CONTEXT_LIMITS', {}).get(model)
        row = override or next((entry for entry in model_catalog() if entry['id'] == model), {})
        self.input_limit = row.get('max_input_tokens')
        self.output_limit = min(output_limit, row.get('max_output_tokens') or output_limit)
        if not isinstance(self.input_limit, int) or self.input_limit <= 0:
            raise ValueError('Шлюз не сообщил размер контекста модели; задание сохранено для продолжения.')
        self.model, self.remaining = model, remaining

    def fits(self, input_tokens, output_tokens):
        from decimal import Decimal
        from .gateway_budget import spend_rub
        cost = spend_rub({'prompt_tokens': input_tokens, 'completion_tokens': output_tokens}, self.model)
        return (output_tokens <= self.output_limit and input_tokens + output_tokens <= self.input_limit
                and cost is not None and Decimal(str(cost)) * Decimal('1.25') <= self.remaining)
