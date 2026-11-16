"""Trial transfer of an anonymized cached specification, without network or AI."""
from io import StringIO
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from tender_selection.models import DocumentPreview, Tender, TenderDocument
from tender_selection.test_document_cache import CONTRACT_URL, _notification
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher
from tenders.models import TenderAnalysis, TenderCommercialItem, TenderComputeJob, TenderEstimate, TenderLine, TenderSourceItem
from tenders.source_memory import SourceMemory, document_blocks
from tenders.tender_data_transfer import apply_transfer_plan, collect_transfer_plan, transfer_report


class TenderDataTransferTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user('transfer-owner')
        self.tender = Tender.objects.create(
            purchase_number='anonymized-print-order', notification_raw=_notification(),
            risk_assessment={'risk_factors': [{'label': 'Образцы'}], 'risk_level': 'medium'},
            profile_assessment={'decision': 'fit'},
        )
        self.source = TenderSourceItem.objects.create(tender=self.tender, source_key='notice:paper', original_text='Буклет', quantity=100)
        self.item = TenderCommercialItem.objects.create(tender=self.tender, source_key='working:paper', display_name='Буклет', quantity=100)
        self.item.source_items.add(self.source)
        estimate = TenderEstimate.objects.create(owner=owner, tender=self.tender, tender_number='local', name='Печать')
        self.line = TenderLine.objects.create(
            estimate=estimate, name='Буклет', quantity=100, nmck_unit=50,
            requirements={'source_item_id': self.source.pk, 'requirements': [{'name': 'Бумага', 'value': 'мелованная'}]},
        )
        self.preview = DocumentPreview.objects.create(url=CONTRACT_URL, kind='docx', html='<p>Бумага мелованная.</p><p>Поставка за 10 дней.</p>')
        engine = GatewayDocumentEnricher()
        documents, _ = engine._bounded_documents([{'url': CONTRACT_URL, 'name': 'ТЗ.docx'}], compact=True)
        memory = SourceMemory(document_blocks({1: documents[0]}), [{'source_id': self.source.pk}], 'saved-policy')
        memory.source_map = {1: {'url': CONTRACT_URL, 'name': 'ТЗ.docx'}}
        for page, key, blocks in list(memory.pending()):
            memory.save([{'id': b['id'], 'scope': 'item', 'source_ids': [self.source.pk], 'facts': [{'quote': b['text']}]} for b in blocks], blocks, page, key)
        self.job = TenderComputeJob.objects.create(tender=self.tender, diagnostics={'source_reading': memory.snapshot()})
        self.job.refresh_from_db()

    def collect(self):
        def select_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(select_only), patch('tenders.services._ai_gateway_json', side_effect=AssertionError('AI forbidden')):
            return collect_transfer_plan(using='default', tender_ids=[self.tender.pk])

    def test_preview_apply_and_repeat_keep_legacy_data_and_paid_reading(self):
        before = (self.tender.notification_raw, self.job.diagnostics, self.line.requirements, self.preview.html)
        plan = self.collect()
        self.assertFalse(TenderDocument.objects.exists())
        self.assertNotIn('мелованная', str(transfer_report(plan)))
        apply_transfer_plan(plan, using='default')
        self.assertEqual(TenderDocument.objects.count(), 1)
        self.assertEqual(TenderAnalysis.objects.count(), 2)
        self.assertFalse(TenderAnalysis.objects.filter(is_current=True).exists())
        self.line.refresh_from_db()
        self.assertEqual(self.line.commercial_item_id, self.item.pk)
        self.preview.refresh_from_db()
        self.assertEqual(len(self.preview.extraction['facts']), 2)
        self.assertEqual(len(TenderDocument.objects.get().assignments), 2)
        self.tender.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(before, (self.tender.notification_raw, self.job.diagnostics, self.line.requirements, self.preview.html))
        repeat = self.collect()
        self.assertEqual(transfer_report(repeat)['changes'], 0)
        apply_transfer_plan(repeat, using='default')
        self.assertEqual(TenderAnalysis.objects.count(), 2)

    def test_changed_document_does_not_receive_old_facts(self):
        self.preview.html = '<p>Срок поставки изменён: 30 дней.</p>'
        self.preview.save(update_fields=['html'])
        plan = self.collect()
        self.assertIn('stale_reading', {issue['code'] for issue in transfer_report(plan)['issues']})
        apply_transfer_plan(plan, using='default')
        self.preview.refresh_from_db()
        self.assertFalse(self.preview.extraction)
        self.job.refresh_from_db()
        self.assertTrue(self.job.diagnostics['source_reading']['complete'])

    def test_ambiguous_item_is_reported_without_guessing_or_losing_facts(self):
        another = TenderCommercialItem.objects.create(tender=self.tender, source_key='other', display_name='Буклет')
        another.source_items.add(self.source)
        plan = self.collect()
        self.assertIn('ambiguous_subject', {issue['code'] for issue in transfer_report(plan)['issues']})
        apply_transfer_plan(plan, using='default')
        self.line.refresh_from_db()
        self.assertIsNone(self.line.commercial_item_id)
        self.assertTrue(all(row['scope'] == 'unassigned' for row in TenderDocument.objects.get().assignments))
        self.preview.refresh_from_db()
        self.assertEqual(len(self.preview.extraction['facts']), 2)

    def test_stale_plan_is_rejected_and_command_defaults_to_read_only(self):
        output = StringIO()
        call_command('trial_tender_data_transfer', tender=[self.tender.pk], stdout=output)
        self.assertFalse(TenderDocument.objects.exists())
        with self.assertRaises(CommandError):
            call_command('trial_tender_data_transfer', apply=True, database='default', stdout=StringIO())
        plan = self.collect()
        self.line.requirements = {'source_item_id': self.source.pk, 'manual': 'уточнение'}
        self.line.save(update_fields=['requirements'])
        with self.assertRaises(ValueError):
            apply_transfer_plan(plan, using='default')
        self.assertFalse(TenderDocument.objects.exists())

    def test_shared_document_keeps_one_extraction_and_tender_scoped_assignments(self):
        other = Tender.objects.create(purchase_number='another-print-order', notification_raw=_notification())
        diagnostics = deepcopy(self.job.diagnostics)
        for row in diagnostics['source_reading']['records'].values():
            row.update(scope='global', source_ids=[])
        TenderComputeJob.objects.create(tender=other, diagnostics=diagnostics)
        plan = collect_transfer_plan(using='default', tender_ids=[self.tender.pk, other.pk])
        apply_transfer_plan(plan, using='default')
        self.assertEqual(DocumentPreview.objects.count(), 1)
        self.assertEqual(TenderDocument.objects.count(), 2)
        self.preview.refresh_from_db()
        self.assertEqual(len(self.preview.extraction['facts']), 2)
        self.assertTrue(all(row['scope'] == 'item' for row in TenderDocument.objects.get(tender=self.tender).assignments))
        self.assertTrue(all(row['scope'] == 'tender' for row in TenderDocument.objects.get(tender=other).assignments))
        repeat = collect_transfer_plan(using='default', tender_ids=[self.tender.pk, other.pk])
        self.assertEqual(transfer_report(repeat)['changes'], 0)
