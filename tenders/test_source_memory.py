import json
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from tender_selection.models import DocumentPreview, Tender
from .models import TenderComputeJob, TenderSourceItem
from .calculation_v2_pipeline import GatewayDocumentEnricher
from .source_memory import SourceMemory, document_blocks, packets, size, validate_reading, MAX_INPUT_BYTES


class SourceMemoryTests(SimpleTestCase):
    def test_large_text_is_bounded_and_changed_document_does_not_reuse_stale_reading(self):
        docs = {1: {'url': 'one', 'text': '≥ 10 мм. ' * 5000, 'evidence_text': 'version1', 'tables': []},
                2: {'url': 'two', 'text': 'Не более 20 шт.', 'evidence_text': 'stable', 'tables': []}}
        blocks = document_blocks(docs)
        self.assertTrue(all(size(batch) < MAX_INPUT_BYTES for batch in packets(blocks, 6500)))
        self.assertTrue(any('≥ 10 мм.' in block['text'] for block in blocks))
        memory = SourceMemory(blocks, [{'source_id': 1}], 'model')
        for page, key, batch in list(memory.pending()):
            memory.save([{'id': block['id'], 'scope': 'item', 'source_ids': [1]} for block in batch], batch, page, key)
        docs[1]['evidence_text'] = 'version2'
        changed = SourceMemory(document_blocks(docs), [{'source_id': 1}], 'model')
        changed.restore(memory.snapshot())
        missing = [block for _, _, batch in changed.pending() for block in batch]
        self.assertTrue(missing)
        self.assertTrue(all(block['doc_id'] == 1 for block in missing))

    def test_skipped_or_unbound_blocks_are_not_marked_read(self):
        block = {'id': 'a'}
        with self.assertRaises(ValueError):
            validate_reading([], [block], {1})
        with self.assertRaises(ValueError):
            validate_reading([{'id': 'a', 'scope': 'item', 'source_ids': [2]}], [block], {1})


@override_settings(CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {'max_input_tokens': 12000, 'max_output_tokens': 8192}})
class SourceMemoryPipelineTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number='memory-proof', notification_raw={'source': {}})
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key='notice:0', original_text='Изделие', quantity=1, unit='шт')
        self.documents = [{'url': 'https://zakupki.gov.ru/spec', 'name': 'ТЗ'}]
        DocumentPreview.objects.create(url=self.documents[0]['url'], kind='docx', html=''.join(f'<p>Требование {i}: ≥ {i} мм</p>' for i in range(30)))

    def engine(self):
        engine = GatewayDocumentEnricher(max_cost_rub='10')
        with patch('tender_selection.services.documents_html'):
            engine.prepare_stages(self.tender, [self.source], self.documents)
        return engine

    @patch('tender_selection.services._publish_intake', side_effect=lambda job: job.save())
    def test_interruption_resumes_and_unchanged_repeat_calls_no_model(self, publish):
        from .services import TenderAIError
        first = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        calls = []
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            calls.append([block['id'] for block in task['blocks']])
            if len(calls) == 2:
                raise TenderAIError('Interruption')
            rows = [{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk], 'facts': [{'quote': block['text']}]} for block in task['blocks']]
            return {'items': rows}, {'prompt_tokens': 1000, 'completion_tokens': 100}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            with self.assertRaises(TenderAIError):
                self.engine().read_source_memory(first)
        first.refresh_from_db()
        saved = {row['id'] for row in first.diagnostics['source_reading']['records'].values()}
        self.assertTrue(saved)
        self.assertFalse(first.diagnostics['source_reading']['complete'])
        self.assertTrue(any(float(stage.get('cost_rub') or 0) > 0 for stage in first.diagnostics['stages'].values()))
        calls.clear()
        second = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        def rest(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            ids = [block['id'] for block in task['blocks']]
            self.assertFalse(saved.intersection(ids))
            calls.append(ids)
            return {'items': [{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk], 'facts': [{'quote': block['text']}]} for block in task['blocks']]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=rest):
            self.engine().read_source_memory(second)
        self.assertTrue(second.diagnostics['source_reading']['complete'])
        third = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')) as gateway:
            self.engine().read_source_memory(third)
        gateway.assert_not_called()
        self.assertTrue(third.diagnostics['source_reading']['complete'])

    def test_table_row_text_is_not_resent_as_cells_and_its_own_header(self):
        text = 'Исходный текст ячейки'
        preview = DocumentPreview.objects.get(url=self.documents[0]['url'])
        preview.html = f'<table><tr><td colspan="2">{text}</td></tr></table>'
        preview.save(update_fields=['html'])
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            wire = json.dumps(task, ensure_ascii=False)
            self.assertEqual(wire.count(text), 1)
            block = task['blocks'][0]
            self.assertNotIn('table_start', block['table_context'])
            self.assertEqual(block['table_context']['cell_spans'][0]['colspan'], 2)
            return {'items': [{'ids': [block['id']], 'scope': 'global', 'source_ids': [], 'reason': 'Требование'}]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            self.engine().read_source_memory(job)
        self.assertTrue(job.diagnostics['source_reading']['complete'])

    def test_saved_unlinked_paid_rows_are_repaired_without_another_model_call(self):
        engine = self.engine()
        old_memory = SourceMemory(document_blocks(engine.document_lookup, engine.document_images),
                                  [{'source_id': self.source.pk}], engine.model)
        rows = [{'id': block['id'], 'scope': 'item', 'source_ids': [],
                 'facts': [{'quote': block['text']}]} for block in old_memory.blocks.values()]
        TenderComputeJob.objects.create(tender=self.tender, diagnostics={'source_reading': old_memory.snapshot(),
            'stages': {'reading:paid': {'state': 'pending_validation', 'result': rows}}})
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid content repeated')) as gateway:
            self.engine().read_source_memory(job)
        gateway.assert_not_called()
        self.assertEqual(job.diagnostics['source_reading']['read'], 30)
        self.assertTrue(job.diagnostics['source_reading']['complete'])

    def test_missing_item_link_is_retained_as_unassigned_without_repeating_reading(self):
        engine = self.engine()
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            return {'items': [{'ids': [block['id'] for block in task['blocks']], 'scope': 'item',
                               'source_ids': [], 'reason': 'Требования без подтверждённой привязки'}]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            engine.read_source_memory(job)
        self.assertTrue(job.diagnostics['source_reading']['complete'])
        self.assertTrue(all(row['scope'] == 'unassigned' for row in job.diagnostics['source_reading']['records'].values()))

    def test_grouped_references_attach_exact_originals_without_model_copying_text(self):
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            return {'items': [{'ids': [block['id'] for block in task['blocks']], 'scope': 'item',
                               'source_ids': [self.source.pk], 'reason': 'Требования товара'}]}, {}
        engine = self.engine()
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            engine.read_source_memory(job)
        self.assertTrue(job.diagnostics['source_reading']['complete'])
        for row in job.diagnostics['source_reading']['records'].values():
            self.assertEqual(row['facts'], [{'quote': engine.source_memory.blocks[row['id']]['text']}])

    def test_partial_reading_continues_without_repeating_confirmed_sources(self):
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        confirmed = set()
        calls = []
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            blocks = task['blocks']
            self.assertTrue(all('occurrences' not in block and 'version' not in block for block in blocks))
            self.assertFalse(confirmed.intersection(block['text'] for block in blocks))
            self.assertTrue(all(len(block['id']) < 8 for block in blocks))
            selected = blocks[:1] if not calls else blocks
            rows = [{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk],
                     'facts': [{'quote': block['text']}]} for block in selected]
            confirmed.update(block['text'] for block in selected)
            calls.append(len(blocks))
            return {'items': rows}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            self.engine().read_source_memory(job)
        self.assertGreater(len(calls), 1)
        self.assertTrue(job.diagnostics['source_reading']['complete'])
        self.assertEqual(len(confirmed), 30)

    @override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True, CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {'max_input_tokens': 30000, 'max_output_tokens': 32000}})
    def test_large_route_batches_persist_before_interruption_and_are_not_requested_again(self):
        from .models import ProcessDefinition, ComponentRoutePlan
        from .calculation_v2_pipeline import GatewayBatchRouter, route_tender_batch
        from .services import TenderAIError
        process = ProcessDefinition.objects.create(name='Изготовление', role='production')
        for i in range(20):
            TenderSourceItem.objects.create(tender=self.tender, source_key=f'extra:{i}', original_text=f'Изделие {i}', quantity=1,
                requirements={'characteristics': [{'name': 'Описание', 'value': 'A' * 1800}]})
        job = TenderComputeJob.objects.create(tender=self.tender)
        requested = []
        def answer(prompt, **kwargs):
            self.assertLessEqual(len((prompt + kwargs['shared_context']).encode()) + 4096, 30000)
            items = json.loads(prompt.rsplit('\n', 1)[1])['items']
            requested.append({item['component_id'] for item in items})
            if len(requested) == 2:
                raise TenderAIError('Interruption')
            return {'items': [{'component_id': item['component_id'], 'process_ids': [process.pk], 'confidence': 1, 'reason': 'Готовое изделие'} for item in items]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            with self.assertRaises(TenderAIError):
                route_tender_batch(job, GatewayBatchRouter(max_cost_rub=10))
        saved = set(ComponentRoutePlan.objects.filter(component__commercial_item__tender=self.tender).values_list('component_id', flat=True))
        self.assertTrue(saved)
        requested.clear()
        def remaining(prompt, **kwargs):
            items = json.loads(prompt.rsplit('\n', 1)[1])['items']
            self.assertFalse(saved.intersection(item['component_id'] for item in items))
            return {'items': [{'component_id': item['component_id'], 'process_ids': [process.pk], 'confidence': 1, 'reason': 'Готовое изделие'} for item in items]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=remaining):
            decisions = route_tender_batch(job, GatewayBatchRouter(max_cost_rub=10))
        self.assertEqual(len(decisions), 21)

class AddressedContextTests(SimpleTestCase):
    def test_inventory_size_never_multiplies_source_reading_and_occurrences_survive(self):
        for count in (1, 90, 1000):
            inventory = [{'source_id': i, 'label': 'Изделие ' + str(i)} for i in range(count)]
            blocks = [{'id': 'a', 'version': 'v', 'doc_id': 1, 'location': 'строка 1', 'text': 'Цвет: синий'},
                      {'id': 'b', 'version': 'v', 'doc_id': 2, 'location': 'строка 2', 'text': 'Цвет: синий'}]
            memory = SourceMemory(blocks, inventory, 'model')
            pending = [block for _, _, batch in memory.pending() for block in batch]
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(pending[0]['occurrences']), 2)
            self.assertEqual(memory.snapshot()['total'], 1)

    def test_identical_table_values_with_different_headers_remain_distinct(self):
        blocks = [{'id': str(i), 'text': '10', 'table_context': {'table_start': [header]}}
                  for i, header in enumerate(('Высота', 'Ширина'))]
        self.assertEqual(len(SourceMemory(blocks, [], 'model').blocks), 2)

    def test_only_extracted_facts_reach_later_tasks_and_all_originals_remain_addressable(self):
        inventory = [{'source_id': i, 'label': str(i)} for i in range(90)]
        blocks = [{'id': str(i), 'version': 'v', 'doc_id': 1, 'location': 'строка ' + str(i),
                   'text': 'EXCLUSIVE_RAW_' + str(i) + ' ' + ('Исходный текст ' * 300) + ' Требование: синий'} for i in range(90)]
        memory = SourceMemory(blocks, inventory, 'model')
        for page, key, batch in memory.pending():
            memory.save([{'id': block['id'], 'scope': 'item', 'source_ids': [int(block['location'].split()[-1])],
                'facts': [{'quote': 'Требование: синий'}]} for block in batch], batch, page, key)
        context = list(memory.context({0}))
        self.assertNotIn('EXCLUSIVE_RAW_', json.dumps(context))
        self.assertEqual(len(memory.blocks), 90)
        self.assertLess(size(context), size(blocks))
        self.assertTrue(memory.snapshot()['complete'])


@override_settings(CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {'max_input_tokens': 1000000, 'max_output_tokens': 32000}})
class SourceRetrievalTests(TestCase):
    setUp = SourceMemoryPipelineTests.setUp
    engine = SourceMemoryPipelineTests.engine
    @override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True)
    def test_addressed_document_pipeline_completes_without_repeating_original_documents(self):
        for count in (1, 90):
            for i in range(TenderSourceItem.objects.filter(tender=self.tender).count(), count):
                TenderSourceItem.objects.create(tender=self.tender, source_key=f"fixture:{i}", original_text=f"Изделие {i}", quantity=1, unit="шт")
            from tender_selection.services import _staged_document_analysis
            self.tender.notification_raw = {'source': {'attachmentsInfo': {'attachmentInfo': [
                {'url': self.documents[0]['url'], 'fileName': 'ТЗ'}]}}}
            self.tender.save(update_fields=['notification_raw'])
            engine = GatewayDocumentEnricher(max_cost_rub=10)
            job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
            current_sources = list(TenderSourceItem.objects.filter(tender=self.tender))
            calls = []
            def answer(prompt, **kwargs):
                task = json.loads(prompt.rsplit('\n', 1)[1])
                calls.append((task, kwargs['shared_context']))
                if 'blocks' in task:
                    return {'items': [{'id': block['id'], 'scope': 'item', 'source_ids': [source.pk for source in current_sources],
                        'facts': [{'quote': block['text']}]} for block in task['blocks']]}, {}
                if 'requested_source_ids' in task:
                    block = next(iter(engine.source_memory.blocks.values()))
                    return {'items': [{'source_id': pk, 'kind': 'finished', 'name': 'Изделие',
                        'reason': 'Один готовый товар', 'evidence': [{'fragment_id': block['id'], 'quote': block['text']}],
                        'goods': [{'name': 'Изделие', 'unit': 'шт', 'quantity_per_parent': 1}]} for pk in task['requested_source_ids']]}, {}
                return {'items': [{'key': good['key'], 'characteristics': []} for good in task.get('goods', [])]}, {}
            with override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True), patch('tender_selection.services.documents_html'), patch('tenders.services._ai_gateway_json', side_effect=answer):
                _staged_document_analysis(job, engine)
            self.assertTrue(job.diagnostics['source_reading']['complete'])
            self.assertTrue(all(job.diagnostics['stages'][name]['state'] == 'complete' for name in ('structure', 'national_regime', 'tz')))
            self.assertFalse(job.diagnostics.get('analysis_gaps'))
            self.assertEqual(sum('blocks' in task for task, context in calls), 1 if count == 1 else 0)
            repeat = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
            with override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True), patch('tender_selection.services.documents_html'), patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid repeat')):
                _staged_document_analysis(repeat, GatewayDocumentEnricher(max_cost_rub=10))
            self.assertEqual(repeat.diagnostics.get('tender_conditions'), job.diagnostics.get('tender_conditions'))
            self.assertTrue(repeat.diagnostics['source_reading']['complete'])

    def test_item_fingerprint_survives_repartition_and_changes_with_its_source(self):
        engine = self.engine()
        memory = SourceMemory([{'id': 'a', 'doc_id': 1, 'version': 'v', 'location': '1', 'text': 'Цвет синий'}],
                              [{'source_id': self.source.pk}], engine.model)
        for page, key, batch in memory.pending():
            memory.save([{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk],
                          'facts': [{'quote': 'Цвет синий'}]} for block in batch], batch, page, key)
        memory.source_map = {1: {'name': 'Source'}}
        engine.source_memory, engine.all_document_images = memory, []
        goods = [{'key': 'a', 'source_id': self.source.pk}, {'key': 'b', 'source_id': self.source.pk}]
        whole = engine.record_fingerprints('tz', 'Read', {'goods': goods})
        single = engine.record_fingerprints('tz', 'Read', {'goods': goods[:1]})
        self.assertEqual(whole['a'], single['a'])
        next(iter(memory.records.values()))['facts'][0]['quote'] = 'Цвет изменён'
        self.assertNotEqual(single['a'], engine.record_fingerprints('tz', 'Read', {'goods': goods[:1]})['a'])

    def test_model_can_read_addressed_original_without_repeating_other_documents(self):
        engine = self.engine()
        blocks = [{'id': str(i), 'doc_id': 1, 'version': 'v', 'location': str(i),
                   'text': 'ORIGINAL_' + str(i) + ' Цвет синий'} for i in range(2)]
        memory = SourceMemory(blocks, [{'source_id': self.source.pk}], engine.model)
        for page, key, batch in memory.pending():
            memory.save([{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk],
                'facts': [{'quote': 'Цвет синий'}]} for block in batch], batch, page, key)
        engine.source_memory = memory
        engine.all_document_images = []
        memory.source_map = {1: {'name': 'Source'}}
        ids = list(memory.blocks)
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        calls = []
        def answer(prompt, **kwargs):
            context = kwargs['shared_context']
            calls.append(context)
            if len(calls) == 1:
                self.assertNotIn('ORIGINAL_', context)
                return {'read_manifest': {'doc_id': 1}}, {'prompt_tokens': 1000, 'completion_tokens': 10}
            if len(calls) == 2:
                self.assertNotIn('ORIGINAL_', context)
                self.assertEqual(json.loads(context)['source_manifest']['entries'][0]['id'], ids[0])
                return {'read_fragments': [ids[0]]}, {'prompt_tokens': 1000, 'completion_tokens': 10}
            self.assertIn('ORIGINAL_0', context)
            self.assertNotIn('ORIGINAL_1', context)
            return {'items': [{'key': 'good', 'characteristics': []}]}, {'prompt_tokens': 1000, 'completion_tokens': 10}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            rows, metrics = engine.ask_stage(job, 'tz', 'Read', {'goods': [{'key': 'good', 'source_id': self.source.pk}]}, output_tokens=2048)
        self.assertEqual(len(calls), 3)
        self.assertEqual(rows[0]['key'], 'good')
        self.assertTrue(metrics['dependencies'])

class ItemEvidenceBindingTests(SimpleTestCase):
    def memory(self):
        blocks = [{'id': str(i), 'version': 'v', 'doc_id': 1, 'location': 'ТЗ / Таблица 1', 'text': text}
                  for i, text in enumerate(['Кружка белая 350 мл', 'Футболка хлопок', 'Доставка одной партией'])]
        memory = SourceMemory(blocks, [{'source_id': 1}, {'source_id': 2}], 'model')
        for page, key, batch in memory.pending():
            memory.save([{'id': b['id'], 'scope': 'unassigned', 'source_ids': [], 'facts': [{'quote': b['text']}]} for b in batch], batch, page, key)
        return memory

    def test_positions_receive_only_their_evidence_and_delivery_stays_on_tender(self):
        memory = self.memory()
        memory.begin_assignment({'inventory': [1, 2]})
        ids = list(memory.blocks)
        memory.assign([{'id': ids[0], 'scope': 'item', 'source_ids': [1], 'purposes': ['tz']},
                       {'id': ids[1], 'scope': 'item', 'source_ids': [2], 'purposes': ['tz']},
                       {'id': ids[2], 'scope': 'global', 'source_ids': [], 'purposes': ['delivery']}], list(memory.blocks.values()))
        self.assertEqual([b['facts'][0]['quote'] for b in memory.context({1}, 'tz')], ['Кружка белая 350 мл'])
        self.assertEqual([b['facts'][0]['quote'] for b in memory.context({2}, 'tz')], ['Футболка хлопок'])
        self.assertEqual([b['facts'][0]['quote'] for b in memory.context(set(), 'delivery')], ['Доставка одной партией'])

    def test_assignment_resume_preserves_reading_and_invalidates_changed_inventory_only(self):
        memory = self.memory()
        memory.begin_assignment({'inventory': [1, 2]})
        block = next(iter(memory.blocks.values()))
        memory.assign([{'id': block['id'], 'scope': 'item', 'source_ids': [1], 'purposes': ['tz']}], [block])
        restored = self.memory()
        restored.restore(memory.snapshot())
        restored.begin_assignment({'inventory': [1, 2]})
        self.assertEqual(len(restored.pending_assignments()), 2)
        restored.begin_assignment({'inventory': [1, 2, 3]})
        self.assertEqual(len(restored.pending_assignments()), 2)
        self.assertTrue(restored.snapshot()['complete'])

    def test_inventory_correction_reuses_exact_quotes_and_retained_links(self):
        memory = self.memory()
        memory.begin_assignment({'inventory': [1, 2]})
        blocks = list(memory.blocks.values())
        memory.assign([{'id': blocks[0]['id'], 'scope': 'item', 'source_ids': [1], 'purposes': ['tz']},
                       {'id': blocks[1]['id'], 'scope': 'item', 'source_ids': [2], 'purposes': ['tz']},
                       {'id': blocks[2]['id'], 'scope': 'unassigned', 'source_ids': [], 'purposes': ['delivery'], 'reason': 'Unclear'}], blocks)
        originals = [{**block, **block['occurrences'][0]} for block in blocks]
        corrected = SourceMemory(originals, [{'source_id': 1}, {'source_id': 3}], 'model')
        corrected.restore(memory.snapshot())
        self.assertFalse(list(corrected.pending()))
        corrected.begin_assignment({'inventory': [1, 3]})
        self.assertEqual(len(corrected.assignments), 1)
        self.assertEqual({block['id'] for block in corrected.pending_assignments()}, {blocks[1]['id'], blocks[2]['id']})

    def test_inventory_correction_does_not_reuse_changed_document(self):
        memory = self.memory()
        blocks = [{**block, 'version': 'changed'} for block in memory.blocks.values()]
        corrected = SourceMemory(blocks, [{'source_id': 1}], 'model')
        corrected.restore(memory.snapshot())
        self.assertEqual(len([block for _, _, batch in corrected.pending() for block in batch]), 3)

    def test_unresolved_binding_cannot_silently_complete_a_position(self):
        memory = self.memory()
        memory.begin_assignment({'inventory': [1, 2]})
        with self.assertRaisesMessage(ValueError, 'Привязка источников'):
            list(memory.context({1}, 'tz'))

    def test_reading_assignments_do_not_require_second_binding(self):
        memory = self.memory()
        blocks = list(memory.blocks.values())
        for block, scope, ids, purposes in ((blocks[0], 'item', [1], ['tz']), (blocks[1], 'item', [2], ['tz']), (blocks[2], 'global', [], ['delivery'])):
            key = memory.key(block['id'], memory.page_keys[0])
            memory.records[key].update(scope=scope, source_ids=ids, purposes=purposes)
        memory.begin_assignment({'inventory': [1, 2]})
        self.assertFalse(memory.pending_assignments())
        self.assertEqual(len(list(memory.context({1}, 'tz'))), 1)
        self.assertEqual(len(list(memory.context(set(), 'delivery'))), 1)

    def test_irrelevant_text_does_not_block_item_evidence(self):
        memory = self.memory()
        memory.begin_assignment({'inventory': [1, 2]})
        blocks = list(memory.blocks.values())
        memory.assign([{'id': blocks[0]['id'], 'scope': 'item', 'source_ids': [1], 'purposes': ['tz']},
                       {'id': blocks[1]['id'], 'scope': 'item', 'source_ids': [2], 'purposes': ['tz']},
                       {'id': blocks[2]['id'], 'scope': 'irrelevant', 'source_ids': [], 'purposes': [], 'reason': 'Не относится к товарам'}], blocks)
        self.assertEqual([b['facts'][0]['quote'] for b in memory.context({1}, 'tz')], ['Кружка белая 350 мл'])
        self.assertEqual(len(memory.snapshot()['assignments']), 3)

    def test_heading_hierarchy_is_retained_without_changing_existing_addresses(self):
        docs = {1: {'url': 'spec', 'source_html': '<h1>ТЗ</h1><h2>Спецификация</h2><p>Материал</p>', 'tables': []}}
        blocks = document_blocks(docs)
        self.assertEqual(blocks[-1]['structure']['headings'], ['ТЗ', 'Спецификация'])
        original = SourceMemory(blocks, [{'source_id': 1}], 'model')
        stripped = SourceMemory([{k: v for k, v in b.items() if k != 'structure'} for b in blocks], [{'source_id': 1}], 'model')
        self.assertEqual(list(original.blocks), list(stripped.blocks))

@override_settings(CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {'max_input_tokens': 10000, 'max_output_tokens': 4096}})
class ItemBindingResumeTests(TestCase):
    setUp = SourceMemoryPipelineTests.setUp
    engine = SourceMemoryPipelineTests.engine

    def prepared(self, state=None):
        engine = self.engine()
        memory = SourceMemory(document_blocks(engine.document_lookup), [{'source_id': self.source.pk, 'label': self.source.original_text}], 'model')
        for page, key, batch in memory.pending():
            memory.save([{'id': b['id'], 'scope': 'unassigned', 'source_ids': [], 'facts': [{'quote': b['text']}]} for b in batch], batch, page, key)
        if state:
            memory.restore(state)
        engine.source_memory, engine.all_document_images = memory, []
        return engine

    def test_inventory_fact_references_preserve_exact_values(self):
        engine = self.prepared()
        facts = [{'id': 0, 'name': 'Размер', 'value': '≥ 10 × 20 мм'},
                 {'id': 1, 'name': 'Размер', 'value': '≥ 10 × 20 мм'}]
        engine.working_context[self.source.pk]['baseline_facts'] = facts
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            expanded = [task['inventory_facts'][ref] for ref in task['inventory'][0]['baseline_facts']]
            self.assertEqual(expanded, [{k: v for k, v in fact.items() if k != 'id'} for fact in facts])
            self.assertEqual(len(task['inventory_facts']), 1)
            return {'items': [{'ids': [b['id'] for b in task['blocks']], 'scope': 'item',
                'source_ids': [self.source.pk], 'purposes': ['tz']}]}, {}
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            engine.bind_source_memory(job)

    @override_settings(CALCULATION_V2_CONTEXT_LIMITS={'anthropic/claude-haiku-5-5': {'max_input_tokens': 16000, 'max_output_tokens': 4096}})
    def test_empty_binding_response_is_saved_and_remaining_packet_is_split(self):
        job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        sizes = []
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            sizes.append(len(task['blocks']))
            self.assertTrue(all(b['id'].startswith('b') for b in task['blocks']))
            if len(sizes) == 1:
                return {'items': []}, {'prompt_tokens': 100, 'completion_tokens': 9}
            return {'items': [{'ids': [b['id'] for b in task['blocks']], 'scope': 'item',
                'source_ids': [self.source.pk], 'purposes': ['tz']}]}, {'prompt_tokens': 100, 'completion_tokens': 30}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            self.prepared().bind_source_memory(job)
        self.assertLess(sizes[1], sizes[0])
        stages = [row for name, row in job.diagnostics['stages'].items() if name.startswith('binding:')]
        self.assertTrue(any(row.get('result') == [] and row.get('state') == 'failed' for row in stages))
        self.assertEqual(job.diagnostics['stages']['source_binding']['state'], 'complete')
        repeated = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid repeat')):
            self.prepared().bind_source_memory(repeated)
        self.assertEqual(repeated.diagnostics['stages']['source_binding']['state'], 'complete')

    def test_interrupted_binding_resumes_without_repaying_confirmed_associations(self):
        from .services import TenderAIError
        first = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        seen, calls = set(), []
        def answer(prompt, **kwargs):
            self.assertEqual(kwargs['thinking'], {'type': 'disabled'})
            self.assertIsNone(kwargs['reasoning_effort'])
            task = json.loads(prompt.rsplit('\n', 1)[1])
            calls.append(task)
            if len(calls) == 2:
                raise TenderAIError('Stopped')
            seen.update(b['text'] for b in task['blocks'])
            return {'items': [{'ids': [b['id'] for b in task['blocks']], 'scope': 'item',
                              'source_ids': [self.source.pk], 'purposes': ['tz']}]}, {'prompt_tokens': 100, 'completion_tokens': 20}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            with self.assertRaises(TenderAIError):
                self.prepared().bind_source_memory(first)
        first.refresh_from_db()
        state = first.diagnostics['source_reading']
        self.assertTrue(state['complete'])
        self.assertGreater(len(state['assignments']), 0)
        second = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        def rest(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            self.assertFalse(seen.intersection(b['text'] for b in task['blocks']))
            self.assertEqual(task['inventory'][0]['quantity'], '1')
            seen.update(b['text'] for b in task['blocks'])
            return {'items': [{'ids': [b['id'] for b in task['blocks']], 'scope': 'item',
                              'source_ids': [self.source.pk], 'purposes': ['tz']}]}, {'prompt_tokens': 100, 'completion_tokens': 20}
        with patch('tenders.services._ai_gateway_json', side_effect=rest):
            engine = self.prepared(state)
            engine.bind_source_memory(second)
        self.assertEqual(len(seen), 30)
        self.source.refresh_from_db()
        self.assertEqual(len(self.source.metadata['evidence_links']['fragment_ids']), 30)
        self.assertEqual(self.source.metadata['evidence_links']['job_id'], second.pk)
        self.assertEqual(second.diagnostics['stages']['source_binding']['state'], 'complete')
        self.assertEqual(second.diagnostics['tender_conditions'], {'delivery': [], 'conditions': []})
        third = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Paid repeat')):
            self.prepared(second.diagnostics['source_reading']).bind_source_memory(third)


class WordOutlineTests(SimpleTestCase):
    def test_word_outline_survives_without_new_visible_markup(self):
        import io
        from docx import Document
        from tender_selection.documents import _docx_html
        doc = Document()
        doc.add_heading('Техническое задание', level=1)
        doc.add_heading('Спецификация', level=2)
        doc.add_paragraph('Материал: керамика')
        stream = io.BytesIO()
        doc.save(stream)
        html = _docx_html(stream.getvalue())
        self.assertNotIn('<h1', html)
        blocks = document_blocks({1: {'url': 'word', 'source_html': html, 'tables': []}})
        self.assertEqual(blocks[-1]['structure']['headings'], ['Техническое задание', 'Спецификация'])
