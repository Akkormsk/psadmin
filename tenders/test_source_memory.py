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


class SourceMemoryPipelineTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number='memory-proof', notification_raw={'source': {}})
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key='notice:0', original_text='Изделие', quantity=1, unit='шт')
        self.documents = [{'url': 'https://example.test/spec', 'name': 'ТЗ'}]
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
            rows = [{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk]} for block in task['blocks']]
            rows[-1]['source_ids'] = [99999]
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
            return {'items': [{'id': key, 'scope': 'item', 'source_ids': [self.source.pk]} for key in ids]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=rest):
            self.engine().read_source_memory(second)
        self.assertTrue(second.diagnostics['source_reading']['complete'])
        third = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'refresh': {}})
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')) as gateway:
            self.engine().read_source_memory(third)
        gateway.assert_not_called()
        self.assertTrue(third.diagnostics['source_reading']['complete'])

    def test_oversized_single_position_uses_windows_and_keeps_all_result_records(self):
        engine = self.engine()
        engine.all_document_images = []
        blocks = [{'id': str(i), 'doc_id': 1, 'text': f'Требование {i}: ' + 'А' * 900} for i in range(20)]
        memory = SourceMemory(blocks, [{'source_id': self.source.pk}], engine.model)
        for page, key, batch in list(memory.pending()):
            memory.save([{'id': block['id'], 'scope': 'item', 'source_ids': [self.source.pk]} for block in batch], batch, page, key)
        engine.source_memory = memory
        job = TenderComputeJob.objects.create(tender=self.tender)
        key = f'{self.source.pk}:0'
        def answer(prompt, **kwargs):
            self.assertLessEqual(len((prompt + kwargs['shared_context']).encode()), MAX_INPUT_BYTES)
            task = json.loads(prompt.rsplit('\n', 1)[1])
            return {'items': [{'key': key, 'characteristics': [{'name': 'Требование', 'value': block_id} for block_id in task['_source_window']]}]}, {}
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            rows, metrics = engine.ask_stage(job, 'tz:1', 'Read', {'goods': [{'source_id': self.source.pk, 'key': key}], 'baseline_facts': {key: []}}, output_tokens=2048)
        self.assertEqual({fact['value'] for fact in rows[0]['characteristics']}, {block['id'] for block in blocks})
        self.assertTrue(metrics['batches'])

    @override_settings(CALCULATION_V2_SOURCE_MEMORY_ENABLED=True)
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
            self.assertLessEqual(len((prompt + kwargs['shared_context']).encode()), MAX_INPUT_BYTES)
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
