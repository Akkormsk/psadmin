from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from tender_selection.models import Tender, DocumentPreview
from tender_selection.services import _document_input_fingerprint, tender_refresh_status_for, update_tender_review
from .analysis_presentation import specification_status, analysis_issues
from .calculation_v2_pipeline import GatewayDocumentEnricher
from .models import TenderComputeJob, TenderSourceItem


class SpecificationStatusTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser('specification-owner', password='test')
        self.tender = Tender.objects.create(purchase_number='specification-status', risk_checked_at=timezone.now(),
            notification_raw={'source': {'attachmentsInfo': {'attachmentInfo': [
                {'url': 'https://zakupki.gov.ru/spec', 'fileName': 'ТЗ.docx'}]}}})
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key='notice:0', original_text='Изделие',
            source_type='notification', quantity=1, unit='шт', requirements={'characteristics': [{'name': 'Материал', 'value': 'металл'}]})
        DocumentPreview.objects.create(url='https://zakupki.gov.ru/spec', html='<p>Материал металл.</p>', kind='docx')

    def completed(self):
        return TenderComputeJob.objects.create(tender=self.tender, version='v2-intake-refresh', status='ready', diagnostics={
            'document_input_fingerprint': _document_input_fingerprint(self.tender),
            'stages': {name: {'state': 'complete'} for name in ('documents', 'structure', 'tz', 'routing')},
            'source_reading': {'read': 1, 'total': 1, 'complete': True}})

    def rejected(self, value):
        engine = GatewayDocumentEnricher()
        engine.stage_sources = {self.source.pk: self.source}
        engine.baselines = {self.source.pk: [{'id': 0, 'name': 'Материал', 'value': 'металл'}]}
        engine.document_lookup = {1: {'url': 'https://zakupki.gov.ru/spec', 'evidence_text': 'Материал металл.'}}
        good = {'key': 'item', 'source_id': self.source.pk, 'name': 'Изделие', 'kind': 'finished'}
        engine.reconcile_specifications([{'key': good['key'], 'characteristics': [
            {'name': 'Материал', 'value': value, 'resolution': 'new', 'doc_id': 1, 'quote': 'Несуществующая цитата'}]}], [good])
        return engine.specification_gaps

    def test_initial_files_are_gray_and_do_not_block_work(self):
        self.assertEqual(specification_status(self.tender)['state'], 'original')
        update_tender_review(self.owner, self.tender.pk, Tender.INTERESTING)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.review, Tender.INTERESTING)

    def test_verified_is_read_only_and_independent_of_routing_and_risk_flags(self):
        job = self.completed()
        job.diagnostics['analysis_gaps'] = [{'stage': 'routing', 'reason': 'Выбор способа изготовления'}]
        job.save(update_fields=['diagnostics'])
        self.tender.risk_assessment = {'stale': True}
        def read_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(read_only):
            result = specification_status(self.tender, job=job)
        self.assertEqual(result['state'], 'checked')
        self.assertEqual(result['label'], 'ТЗ проверено')

    def test_hidden_rejected_new_requirement_keeps_yellow_without_technical_message(self):
        job = self.completed()
        job.diagnostics['analysis_gaps'] = self.rejected('пластик')
        job.save(update_fields=['diagnostics'])
        self.assertTrue(job.diagnostics['analysis_gaps'][0]['affects_specification'])
        self.assertEqual(analysis_issues(job), [])
        result = specification_status(self.tender, job=job)
        self.assertEqual(result['state'], 'partial')
        self.assertIn('Материал', ' '.join(result['details']))
        self.assertNotIn('Цитата', ' '.join(result['details']))
        update_tender_review(self.owner, self.tender.pk, Tender.INTERESTING)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.review, Tender.INTERESTING)

    def test_failed_duplicate_of_known_value_does_not_lower_completeness(self):
        job = self.completed()
        job.diagnostics['analysis_gaps'] = self.rejected('металл')
        job.save(update_fields=['diagnostics'])
        self.assertFalse(job.diagnostics['analysis_gaps'][0]['affects_specification'])
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'checked')

    def test_incomplete_reading_and_changed_inputs_cannot_be_green(self):
        job = self.completed()
        job.diagnostics['source_reading']['complete'] = False
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')
        job.diagnostics['source_reading']['complete'] = True
        self.source.requirements = {'characteristics': [{'name': 'Материал', 'value': 'дерево'}]}
        self.source.save(update_fields=['requirements'])
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')

    def test_route_alone_and_unclassified_legacy_gap_do_not_claim_verified(self):
        job = self.completed()
        job.diagnostics['analysis_gaps'] = [{'stage': 'tz', 'key': 'item', 'name': 'Материал', 'reason': 'Internal failure'}]
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')
        job.diagnostics = {'stages': {'routing': {'state': 'complete'}}}
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')

    def test_shared_card_status_has_coverage_but_bulk_status_can_skip_it(self):
        self.completed()
        result = tender_refresh_status_for(self.owner, self.tender.pk)
        self.assertEqual(result['specification']['state'], 'checked')
        self.assertNotIn('specification', tender_refresh_status_for(self.owner, self.tender.pk, include_specification=False))


    def test_legacy_completion_needs_saved_reading_evidence(self):
        job = self.completed()
        job.diagnostics.pop('source_reading')
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')
        job.diagnostics['stages']['documents']['result'] = {'documents': [{'reading_status': 'read'}]}
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'checked')

    def test_new_update_does_not_call_previously_processed_data_original(self):
        self.completed()
        job = TenderComputeJob.objects.create(tender=self.tender, version='v2-intake-refresh', status='queued')
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')

    def test_omitted_item_specification_is_not_a_harmless_empty_answer(self):
        job = self.completed()
        engine = GatewayDocumentEnricher()
        engine.stage_sources = {self.source.pk: self.source}
        engine.baselines = {self.source.pk: [{'id': 0, 'name': 'Материал', 'value': 'металл'}]}
        good = {'key': 'item', 'source_id': self.source.pk, 'name': 'Изделие', 'kind': 'finished'}
        engine.reconcile_specifications([], [good])
        job.diagnostics['analysis_gaps'] = engine.specification_gaps
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')


    def test_failed_source_binding_cannot_be_hidden_by_complete_later_stages(self):
        job = self.completed()
        job.diagnostics['stages']['source_binding'] = {'state': 'failed'}
        self.assertEqual(specification_status(self.tender, job=job)['state'], 'partial')
