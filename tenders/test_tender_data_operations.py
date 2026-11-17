"""New/active tender write flows; archive reads never trigger conversion."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from tender_selection.models import DocumentPreview, Tender, TenderDocument
from tender_selection.services import preview_tender_document, upload_tender_document_preview, push_to_estimate
from tender_selection.test_document_cache import CONTRACT_URL, _notification
from tenders.estimate_actions import persist_estimate
from tenders.models import TenderAnalysis, TenderEstimate, TenderLine, TenderSettings
from tenders.tender_data import prepare_tender_for_write, publish_analysis


class TenderDataOperationsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser('active-owner', password='test')
        self.tender = Tender.objects.create(purchase_number='new-print-order', notification_raw=_notification())

    def test_archive_read_does_not_activate_or_fetch_and_explicit_work_preserves_calculation(self):
        self.tender.data_version = Tender.DataVersion.LEGACY
        self.tender.status = Tender.DISMISSED
        self.tender.save(update_fields=['data_version', 'status'])
        estimate = TenderEstimate.objects.create(owner=self.owner, tender=self.tender, tender_number='local', name='Печать')
        line = TenderLine.objects.create(estimate=estimate, name='Буклет', quantity=100, nmck_unit=50, material_unit=20)
        def read_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(read_only), patch('tender_selection.services._fetch_doc_bytes', side_effect=AssertionError('Archive fetch')):
            result = preview_tender_document(self.owner, self.tender.pk, 1)
        self.assertTrue(result['error'])
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.data_version, Tender.DataVersion.LEGACY)
        with self.assertRaises(ValueError):
            prepare_tender_for_write(self.tender)
        self.tender.status = Tender.NEW
        self.tender.save(update_fields=['status'])
        prepare_tender_for_write(self.tender)
        line.refresh_from_db()
        self.assertEqual(self.tender.data_version, Tender.DataVersion.CURRENT)
        self.assertEqual((line.estimate_id, line.quantity, line.material_unit), (estimate.pk, 100, 20))

    def test_shared_save_keeps_ids_and_rejects_foreign_or_duplicate_rows(self):
        estimate = TenderEstimate.objects.create(owner=self.owner, tender=self.tender, tender_number='local', name='Печать')
        line = TenderLine.objects.create(estimate=estimate, name='Буклет', quantity=100, nmck_unit=50)
        settings = TenderSettings.objects.create()
        row = {'id': line.pk, 'name': line.name, 'quantity': '100', 'nmck_unit': '50', 'material_unit': '20'}
        data = {'tender_number': 'local', 'name': 'Печать', 'lines_json': json.dumps([row])}
        _, error, _ = persist_estimate(self.owner, data, estimate, settings, pipeline=True)
        self.assertIsNone(error)
        self.assertEqual(estimate.lines.get().pk, line.pk)
        for rows in ([{**row, 'id': line.pk + 1}], [row, row]):
            _, error, _ = persist_estimate(self.owner, {**data, 'lines_json': json.dumps(rows)}, estimate, settings, pipeline=True)
            self.assertIsNotNone(error)
            self.assertEqual(estimate.lines.get().pk, line.pk)

    def test_document_versions_keep_previous_content_and_reuse_identical_upload(self):
        with patch('tender_selection.services.extract_preview', return_value={'kind': 'docx', 'html': '<p>Бумага мелованная</p>'}) as parse:
            upload_tender_document_preview(self.owner, self.tender.pk, 1, b'original-document')
            upload_tender_document_preview(self.owner, self.tender.pk, 1, b'original-document')
        self.assertEqual(parse.call_count, 1)
        original = TenderDocument.objects.get(tender=self.tender, is_current=True)
        other = Tender.objects.create(purchase_number='other-print-order', notification_raw=_notification())
        with patch('tender_selection.services.extract_preview', side_effect=AssertionError('Repeated parse')):
            upload_tender_document_preview(self.owner, other.pk, 1, b'original-document')
        with patch('tender_selection.services.extract_preview', return_value={'kind': 'docx', 'html': '<p>Бумага офсетная</p>'}):
            upload_tender_document_preview(self.owner, self.tender.pk, 1, b'changed-document')
        original.refresh_from_db()
        self.assertFalse(original.is_current)
        self.assertEqual(original.preview.html, '<p>Бумага мелованная</p>')
        self.assertEqual(DocumentPreview.objects.filter(url=CONTRACT_URL).count(), 2)
        result = preview_tender_document(self.owner, self.tender.pk, 1)
        self.assertEqual(result['html'], '<p>Бумага офсетная</p>')
        self.assertEqual(preview_tender_document(self.owner, other.pk, 1)['html'], '<p>Бумага мелованная</p>')

    def test_analysis_publication_is_idempotent_and_preserves_old_result(self):
        first = publish_analysis(self.tender, kind='risk', input_fingerprint='source-a', result={'level': 'unknown'})
        repeat = publish_analysis(self.tender, kind='risk', input_fingerprint='source-a', result={'level': 'unknown'})
        self.assertEqual(first.pk, repeat.pk)
        second = publish_analysis(self.tender, kind='risk', input_fingerprint='source-b', result={'level': 'attention'})
        first.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertEqual(first.result, {'level': 'unknown'})
        self.assertEqual(second.supersedes_id, first.pk)
        self.assertEqual(TenderAnalysis.objects.filter(is_current=True).count(), 1)

    def test_new_calculation_is_linked_at_creation_without_archive_transfer(self):
        from tenders.test_calculation_v2_pipeline import notification, raw_item
        self.tender.notification_raw = notification([raw_item('Буклет', '100')])
        self.tender.save(update_fields=['notification_raw'])
        estimate_id = push_to_estimate(self.tender, self.owner)
        line = TenderLine.objects.get(estimate_id=estimate_id)
        self.assertIsNotNone(line.commercial_item_id)
        self.assertEqual(line.commercial_item.tender_id, self.tender.pk)
        again = push_to_estimate(self.tender, self.owner)
        self.assertEqual(again, estimate_id)
        self.assertEqual(TenderLine.objects.get(estimate_id=again).pk, line.pk)
        _, error, _ = persist_estimate(self.owner,
            {'tender_number': 'local', 'name': 'Печать', 'lines_json': json.dumps([
                {'id': line.pk, 'name': line.name, 'quantity': '100', 'nmck_unit': '50',
                 'material_unit': '20', 'requirements': {'source_item_id': line.requirements['source_item_id'] + 1}},
            ])}, TenderEstimate.objects.get(pk=estimate_id), TenderSettings.objects.get_or_create(pk=1)[0], pipeline=True)
        self.assertIsNotNone(error)
        line.refresh_from_db()
        self.assertEqual(line.commercial_item.tender_id, self.tender.pk)

    def test_explicit_source_revision_keeps_working_subject_and_old_calculation(self):
        from tenders.test_calculation_v2_pipeline import notification, raw_item
        from tenders.calculation_v2_pipeline import ingest_source_items, build_commercial_items
        self.tender.notification_raw = notification([raw_item('Буклет', '100', '18.12')])
        self.tender.save(update_fields=['notification_raw'])
        estimate_id = push_to_estimate(self.tender, self.owner)
        line = TenderLine.objects.get(estimate_id=estimate_id)
        original_item = line.commercial_item_id
        original_component = line.commercial_item.components.get().pk
        self.tender.notification_raw = notification([raw_item('Буклет', '200', '18.12')])
        self.tender.save(update_fields=['notification_raw'])
        sources = ingest_source_items(self.tender)
        working = build_commercial_items(tender=self.tender, sources=sources)
        self.assertEqual(working[0].pk, original_item)
        self.assertEqual(working[0].components.get().pk, original_component)
        self.assertEqual(build_commercial_items(tender=self.tender, sources=sources)[0].pk, original_item)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 100)
        self.assertEqual(line.commercial_item.quantity, 200)
