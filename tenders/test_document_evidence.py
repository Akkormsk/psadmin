from unittest.mock import patch
from django.test import TestCase, override_settings

from tender_selection.models import DocumentPreview, TenderDocument
from .models import TenderComputeJob
from . import test_source_memory as fixtures


@override_settings(**fixtures.SourceMemoryPipelineTests._overridden_settings)
class DocumentEvidenceSmokeTests(TestCase):
    setUp = fixtures.SourceMemoryPipelineTests.setUp
    engine = fixtures.SourceMemoryPipelineTests.engine

    def read(self, job, scope='item', purposes=('tz',)):
        import json
        def answer(prompt, **kwargs):
            task = json.loads(prompt.rsplit('\n', 1)[1])
            return {'items': [{'ids': [block['id'] for block in task['blocks']],
                'scope': scope, 'source_ids': [self.source.pk] if scope == 'item' else [], 'purposes': list(purposes)}]}, {}
        engine = self.engine()
        with patch('tenders.services._ai_gateway_json', side_effect=answer):
            engine.read_source_memory(job)
        return engine

    def test_reading_lives_with_document_and_survives_removal_of_job_diagnostics(self):
        job = TenderComputeJob.objects.create(tender=self.tender)
        engine = self.read(job)
        preview = DocumentPreview.objects.get(url=self.documents[0]['url'])
        self.assertTrue(preview.extraction['facts'])
        self.assertTrue(TenderDocument.objects.get(tender=self.tender).assignments)
        self.assertNotIn('fragments', job.diagnostics['source_reading'])
        self.assertNotIn('Требование 0:', str(job.diagnostics))
        expected = engine.source_memory.snapshot()['records']
        job.diagnostics = {}
        job.save(update_fields=['diagnostics'])
        repeat = TenderComputeJob.objects.create(tender=self.tender)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated paid reading')):
            repeated = self.engine()
            repeated.read_source_memory(repeat)
        self.assertEqual(repeated.source_memory.snapshot()['records'], expected)
        preview.refresh_from_db()
        self.assertEqual(len(preview.extraction['facts']), len(engine.source_memory.blocks))

    def test_new_content_does_not_reuse_old_reading(self):
        job = TenderComputeJob.objects.create(tender=self.tender)
        self.read(job)
        DocumentPreview.objects.filter(url=self.documents[0]['url']).update(html='<p>Новая редакция: размер 45 мм.</p>')
        repeat = TenderComputeJob.objects.create(tender=self.tender)
        with patch('tenders.services._ai_gateway_json', side_effect=RuntimeError('New content needs reading')):
            with self.assertRaisesRegex(RuntimeError, 'New content'):
                self.engine().read_source_memory(repeat)

    def test_general_conditions_are_references_and_remain_readable_without_job(self):
        from .document_evidence import condition_context
        self.tender.notification_raw = {'source': {'attachmentsInfo': {'attachmentInfo': [
            {'url': self.documents[0]['url'], 'fileName': 'Условия поставки'}]}}}
        self.tender.save(update_fields=['notification_raw'])
        job = TenderComputeJob.objects.create(tender=self.tender)
        engine = self.read(job, scope='global', purposes=('delivery', 'conditions'))
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            engine.bind_source_memory(job)
        self.tender.refresh_from_db()
        self.assertTrue(self.tender.conditions['delivery'])
        self.assertNotIn('Требование 0:', str(self.tender.conditions))
        self.assertNotIn('Требование 0:', str(job.diagnostics))
        context = condition_context(self.tender)
        self.assertIn('Требование 0:', str(context))
        self.assertEqual(context, condition_context(self.tender, job))
        job.delete()
        self.assertEqual(context, condition_context(self.tender))
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        owner = get_user_model().objects.create_superuser('conditions-owner', password='test')
        self.client.force_login(owner)
        with patch('tender_selection.views.notification_for', return_value=self.tender.notification_raw), patch('tender_selection.views.extras_for', return_value=([], [])):
            response = self.client.get(reverse('tender_selection:detail', args=[self.tender.pk]))
        self.assertContains(response, 'Поставка и общие условия')
        self.assertContains(response, 'Требование 0:')
        # A temporary parsing failure must not erase already known conditions.
        from .calculation_v2_pipeline import GatewayDocumentEnricher
        unavailable = GatewayDocumentEnricher(max_cost_rub='10')
        with patch('tender_selection.services.documents_html'), patch.object(unavailable, '_bounded_documents',
                return_value=([], [{'url': self.documents[0]['url'], 'name': 'Условия поставки', 'reading_status': 'unreadable'}])):
            unavailable.prepare_stages(self.tender, [self.source], self.documents)
        partial = TenderComputeJob.objects.create(tender=self.tender)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Unavailable source')):
            unavailable.read_source_memory(partial)
            unavailable.bind_source_memory(partial)
        self.tender.refresh_from_db()
        self.assertEqual(condition_context(self.tender, partial), context)

    def test_reassignment_reuses_facts_and_keeps_previous_job_address(self):
        from .models import TenderSourceItem
        from .calculation_v2_pipeline import GatewayDocumentEnricher
        original = TenderComputeJob.objects.create(tender=self.tender)
        self.read(original)
        other = TenderSourceItem.objects.create(tender=self.tender, source_key='second', original_text='Другое изделие')
        engine = GatewayDocumentEnricher(max_cost_rub='10')
        with patch('tender_selection.services.documents_html'):
            engine.prepare_stages(self.tender, [self.source, other], self.documents)
        correction = TenderComputeJob.objects.create(tender=self.tender)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            engine.read_source_memory(correction)
        memory = engine.source_memory
        memory.begin_assignment('Проверенное исправление адресата')
        memory.assign([{'id': block['id'], 'scope': 'item', 'source_ids': [other.pk], 'purposes': ['tz']}
                       for block in memory.blocks.values()], list(memory.blocks.values()))
        correction.diagnostics['source_reading'] = engine.document_evidence.publish()
        correction.save(update_fields=['diagnostics'])
        from .document_evidence import DocumentEvidence
        from .source_memory import SourceMemory
        historical = SourceMemory(list(memory.blocks.values()), memory.inventory, 'model')
        historical_store = DocumentEvidence(original, historical, engine.document_lookup)
        historical_store.restore()
        self.assertTrue(all(row['source_ids'] == [self.source.pk] for row in historical.snapshot()['records'].values()))
        current = SourceMemory(list(memory.blocks.values()), memory.inventory, 'model')
        DocumentEvidence(TenderComputeJob.objects.create(tender=self.tender), current, engine.document_lookup).restore()
        self.assertTrue(all(row['source_ids'] == [other.pk] for row in current.snapshot()['records'].values()))
        historical_store.publish()
        unchanged = SourceMemory(list(memory.blocks.values()), memory.inventory, 'model')
        DocumentEvidence(TenderComputeJob.objects.create(tender=self.tender), unchanged, engine.document_lookup).restore()
        self.assertTrue(all(row['source_ids'] == [other.pk] for row in unchanged.snapshot()['records'].values()))
        # Returning to a previous address changes the current choice, not the facts.
        before = len(TenderDocument.objects.get(tender=self.tender).assignments)
        DocumentEvidence(TenderComputeJob.objects.create(tender=self.tender), historical, engine.document_lookup).publish()
        restored = SourceMemory(list(memory.blocks.values()), memory.inventory, 'model')
        DocumentEvidence(TenderComputeJob.objects.create(tender=self.tender), restored, engine.document_lookup).restore()
        self.assertTrue(all(row['source_ids'] == [self.source.pk] for row in restored.snapshot()['records'].values()))
        self.assertEqual(len(TenderDocument.objects.get(tender=self.tender).assignments), before)

    def test_shared_file_does_not_share_tender_specific_addresses(self):
        from tender_selection.models import Tender
        from .models import TenderSourceItem
        from .calculation_v2_pipeline import GatewayDocumentEnricher
        self.read(TenderComputeJob.objects.create(tender=self.tender))
        other = Tender.objects.create(purchase_number='another-order')
        source = TenderSourceItem.objects.create(tender=other, source_key='item', original_text='Товар другого тендера')
        engine = GatewayDocumentEnricher(max_cost_rub='10')
        with patch('tender_selection.services.documents_html'):
            engine.prepare_stages(other, [source], self.documents)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            engine.read_source_memory(TenderComputeJob.objects.create(tender=other))
        self.assertTrue(engine.source_memory.snapshot()['complete'])
        self.assertTrue(all(row['scope'] == 'unassigned' and not row['source_ids']
                            for row in engine.source_memory.snapshot()['records'].values()))

    def test_new_document_version_retires_current_conditions_but_keeps_history(self):
        from .document_evidence import condition_context
        from tender_selection.services import store_document_preview
        self.tender.notification_raw = {'source': {'attachmentsInfo': {'attachmentInfo': [
            {'url': self.documents[0]['url'], 'fileName': 'Условия поставки'}]}}}
        self.tender.save(update_fields=['notification_raw'])
        job = TenderComputeJob.objects.create(tender=self.tender)
        engine = self.read(job, scope='global', purposes=('delivery',))
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            engine.bind_source_memory(job)
        previous = condition_context(self.tender, job)
        with patch('tender_selection.services.extract_preview', return_value={'kind': 'docx', 'html': '<p>Новая редакция условий.</p>'}):
            store_document_preview(self.documents[0]['url'], 'Условия поставки', b'new-document', tender=self.tender)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.conditions, {'delivery': [], 'conditions': []})
        self.assertEqual(condition_context(self.tender, job, current=False), previous)
        self.assertEqual(condition_context(self.tender, job), {'delivery': [], 'conditions': []})
        engine.document_evidence.save_conditions()
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.conditions, {'delivery': [], 'conditions': []})

    def test_old_complete_snapshot_is_adopted_without_paid_reading(self):
        original = TenderComputeJob.objects.create(tender=self.tender)
        engine = self.read(original)
        engine.source_memory.begin_assignment('Прежняя подтверждённая привязка')
        snapshot = engine.source_memory.snapshot()
        original.diagnostics = {'source_reading': snapshot}
        original.save(update_fields=['diagnostics'])
        DocumentPreview.objects.filter(url=self.documents[0]['url']).update(structure={}, extraction={})
        TenderDocument.objects.filter(tender=self.tender).update(assignments=[])
        job = TenderComputeJob.objects.create(tender=self.tender)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            self.assertTrue(self.engine().adopt_source_memory(job, snapshot))
        self.assertEqual(job.diagnostics['source_reading']['storage'], 'documents')
        self.assertNotIn('fragments', job.diagnostics['source_reading'])
        self.assertTrue(DocumentPreview.objects.get(url=self.documents[0]['url']).extraction['facts'])
        original.refresh_from_db()
        self.assertIn('fragments', original.diagnostics['source_reading'])

    def test_repeated_text_keeps_each_source_address_without_copying_facts(self):
        DocumentPreview.objects.filter(url=self.documents[0]['url']).update(
            html='<h2>Первая позиция</h2><p>Цвет синий.</p><h2>Вторая позиция</h2><p>Цвет синий.</p>')
        self.read(TenderComputeJob.objects.create(tender=self.tender))
        preview = DocumentPreview.objects.get(url=self.documents[0]['url'])
        facts = [fact for fact in preview.extraction['facts'].values() if fact['quote'] == 'Цвет синий.']
        self.assertEqual(len(facts), 1)
        fragment = preview.structure['fragments'][facts[0]['fragment_id']]
        self.assertEqual(len(fragment['occurrences']), 2)
        self.assertEqual(len({occurrence['location'] for occurrence in fragment['occurrences']}), 2)

    def test_document_proposal_keeps_reference_to_shared_reading(self):
        from .calculation_v2_pipeline import assess_quality, enrich_suspicious_tender
        self.tender.notification_raw = {'source': {'attachmentsInfo': {'attachmentInfo': [
            {'url': self.documents[0]['url'], 'fileName': 'ТЗ'}]}}}
        self.tender.save(update_fields=['notification_raw'])
        job = TenderComputeJob.objects.create(tender=self.tender)
        engine = self.read(job)
        identity, block = next(iter(engine.source_memory.blocks.items()))
        proof = engine.stage_quote({'fragment_id': identity, 'quote': block['text']})
        self.assertIn('document_ref', proof)
        goods = [{'key': 'good', 'source_id': self.source.pk, 'kind': 'finished', 'name': self.source.original_text,
            'unit': self.source.unit, 'quantity_per_parent': '1', 'parent_name': self.source.original_text,
            'reason': 'Один готовый товар', 'provenance': proof}]
        specs = engine.validate_specifications([{'key': 'good', 'characteristics': [{'name': 'Требование',
            'value': block['text'], 'resolution': 'new', 'baseline_refs': [], 'fragment_id': identity, 'quote': block['text']}]}], goods)
        engine.prepared_entries(goods, specs)
        with patch('tenders.services._ai_gateway_json', side_effect=AssertionError('Repeated reading')):
            result = enrich_suspicious_tender(self.tender, assess_quality(self.tender, [self.source]), engine)
        proposal = result['created'][0]
        self.assertEqual(proposal.provenance['document_ref'], proof['document_ref'])
        link = TenderDocument.objects.get(pk=proof['document_ref']['document_id'])
        self.assertIn(proof['document_ref']['fragment_id'], link.preview.structure['fragments'])
