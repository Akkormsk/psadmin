"""Bounded source fragments and durable reading state, independent of AI provider."""
import hashlib
import json

VERSION = 'source-memory-v1'
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
        tables = doc.get('tables', [])
        def add_table(number, heading):
            table = tables[number - 1]
            table_start = table.get('rows', [])[:2]
            for row_no, row in enumerate(table.get('rows', []), 1):
                entries.append(('table', f'{heading} / Таблица {number}, строка {row_no}', json.dumps({'table_start': table_start, 'row': row}, ensure_ascii=False)))
        for node in root.xpath('.//p[not(ancestor::table)] | .//li[not(ancestor::table) and not(.//p)] | .//h1 | .//h2 | .//h3 | .//h4 | .//table[not(ancestor::table)]'):
            if node.tag == 'table':
                table_no += 1
                if table_no <= len(tables):
                    add_table(table_no, section)
                continue
            text = ' '.join(node.itertext()).strip()
            if node.tag in {'h1', 'h2', 'h3', 'h4'}:
                section = text[:160]
            if text:
                paragraph_no += 1
                entries.append(('paragraph', f'{section} / Абзац {paragraph_no}', text))
        if not entries and doc.get('text'):
            entries.append(('paragraph', 'Абзац 1', doc['text']))
        for number in range(table_no + 1, len(tables) + 1):
            add_table(number, section)
        for kind, location, text in entries:
            for part, fragment in enumerate(text_parts(text), 1):
                anchor = signature([doc['url'], kind, fragment])
                occurrence = occurrences.get(anchor, 0)
                occurrences[anchor] = occurrence + 1
                blocks.append({'id': f'{anchor}:{occurrence}', 'version': version, 'doc_id': doc_id, 'location': f'{location}, часть {part}', 'text': fragment})
        image_indexes = doc.get('image_indexes', [])
        pages = doc.get('visual_pages', [])
        for offset, image_no in enumerate(image_indexes):
            location = f'Изображение {image_no}'
            if len(pages) == len(image_indexes):
                location = f'Страница {pages[offset]} / {location}'
            blocks.append({'id': signature([doc['url'], 'image', image_no, signature(images[image_no - 1])]), 'doc_id': doc_id,
                'version': version, 'location': location, 'image_index': image_no, 'text': ''})
    return blocks


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


class SourceMemory:
    def __init__(self, blocks, inventory, model, source_versions=None):
        self.blocks = {block['id']: block for block in blocks}
        self.pages = list(packets(inventory, 12000)) or [[]]
        self.page_keys = [signature([VERSION, model, page, [(source['source_id'], (source_versions or {}).get(source['source_id'])) for source in page]]) for page in self.pages]
        self.records = {}

    def restore(self, state):
        if state.get('version') != VERSION:
            return
        for key, row in state.get('records', {}).items():
            # The key binds the exact block and inventory page, not a document-wide hash.
            if key not in self.records:
                self.records[key] = row

    def key(self, block_id, page_key):
        return signature([block_id, self.blocks[block_id].get('version'), page_key])

    def pending(self):
        for page, page_key in zip(self.pages, self.page_keys):
            missing = [block for block in self.blocks.values() if self.key(block['id'], page_key) not in self.records]
            # Keep image input bounded as well as text; identical images are deduplicated by gateway.
            for batch in packets(missing, 6500):
                for offset in range(0, len(batch), 12):
                    yield page, page_key, batch[offset:offset + 12]

    def save(self, rows, blocks, page, page_key):
        validate_reading(rows, blocks, {source['source_id'] for source in page})
        for row in rows:
            self.records[self.key(row['id'], page_key)] = row

    def snapshot(self):
        current = {self.key(block_id, page_key) for block_id in self.blocks for page_key in self.page_keys}
        records = {key: row for key, row in self.records.items() if key in current}
        return {'version': VERSION, 'fragments': self.blocks, 'source_map': getattr(self, 'source_map', {}),
            'records': records, 'total': len(current), 'read': len(records),
            'complete': len(current) == len(records), 'sources': len(self.blocks)}

    def relevant(self, source_ids):
        for block_id, block in self.blocks.items():
            rows = [self.records.get(self.key(block_id, key)) for key in self.page_keys]
            rows = [row for row in rows if row]
            linked = {ref for row in rows if row['scope'] == 'item' for ref in row['source_ids']}
            global_scope = any(row['scope'] == 'global' for row in rows)
            uncertain = not linked and any(row['scope'] == 'unassigned' for row in rows)
            if global_scope or uncertain or linked.intersection(source_ids):
                yield block
