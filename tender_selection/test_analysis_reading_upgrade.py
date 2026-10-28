import io
import zipfile
from unittest.mock import patch
from django.test import TestCase
from docx import Document

from .documents import extract_preview
from .models import DocumentPreview, Tender
from .services import documents_html
from tenders.analysis_presentation import analysis_issues
from tenders.models import TenderComputeJob


class AnalysisReadingUpgradeTests(TestCase):
    def test_html_is_read_without_executing_or_rendering_source_code(self):
        result = extract_preview(b'<html><body onload="evil()"><script>secret()</script><table><tr><td rowspan="2">Amount</td><td>100</td></tr></table><img src="https://evil.test/a"><a href="javascript:evil()">Fact</a></body></html>', 'document.html')
        self.assertEqual(result['kind'], 'html')
        self.assertIn('rowspan="2"', result['html'])
        self.assertIn('100', result['html'])
        for unsafe in ['secret', 'evil', '<img', 'onload', 'javascript:']:
            self.assertNotIn(unsafe, result['html'])

    def test_archive_reads_html_and_distinguishes_signature_from_missing_document(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('document.html', '<p>Document facts 100</p>')
            archive.writestr('document.sign', b'crypto-signature')
        result = extract_preview(data.getvalue(), 'archive.zip', read_archive=True)
        self.assertFalse(result['error'])
        self.assertIn('Document facts 100', result['html'])
        self.assertIn('data-document-info="signature"', result['html'])
        self.assertIn('data-document-archive-version="2"', result['html'])
        with zipfile.ZipFile(data, 'a') as archive:
            archive.writestr('unknown.data', b'unknown-business-document')
        self.assertTrue(extract_preview(data.getvalue(), 'archive.zip', read_archive=True)['error'])

    def test_old_doc_preview_is_rebuilt_once_using_existing_office_converter(self):
        tender = Tender.objects.create(purchase_number='cache-upgrade')
        url = 'https://zakupki.gov.ru/old'
        DocumentPreview.objects.create(url=url, kind='doc', html='<p>Old fragment</p><p class="ts-sub">Warning</p>')
        doc = Document(); doc.add_paragraph('Complete document facts'); data = io.BytesIO(); doc.save(data)
        with patch('tender_selection.services._fetch_doc_bytes', return_value=b'legacy-doc') as fetch, patch('tender_selection.office.convert_office', return_value=data.getvalue()) as convert:
            documents_html(tender, [{'url': url, 'name': 'document.doc'}])
            documents_html(tender, [{'url': url, 'name': 'document.doc'}])
        fetch.assert_called_once(); convert.assert_called_once()
        self.assertIn('Complete document facts', DocumentPreview.objects.get(url=url).html)
        self.assertNotIn('Warning', DocumentPreview.objects.get(url=url).html)

    def test_failed_upgrade_keeps_usable_old_evidence(self):
        tender = Tender.objects.create(purchase_number='failed-upgrade')
        url = 'https://zakupki.gov.ru/old'
        DocumentPreview.objects.create(url=url, kind='doc', html='<p>Existing evidence</p>')
        with patch('tender_selection.services._fetch_doc_bytes', return_value=b'legacy-doc'), patch('tender_selection.office.convert_office', side_effect=ValueError('conversion failed')):
            documents_html(tender, [{'url': url, 'name': 'document.doc'}])
        self.assertIn('Existing evidence', DocumentPreview.objects.get(url=url).html)

    def test_details_explain_saved_partial_reading(self):
        tender = Tender.objects.create(purchase_number='reading-explanation')
        job = TenderComputeJob.objects.create(tender=tender, status='needs_review', diagnostics={'enrichment': [{'documents': {'considered': [{'name': 'spec.doc', 'reading_status': 'partial', 'error': ''}, {'name': 'price.pdf', 'reading_status': 'unreadable', 'error': 'File error'}]}}]})
        issues = analysis_issues(job)
        self.assertEqual(len(issues), 2)
        self.assertIn('spec.doc', issues[0]); self.assertIn('price.pdf: File error', issues[1])

    def test_card_exposes_the_saved_reason_as_escaped_details(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from django.utils import timezone
        user = get_user_model().objects.create_superuser('status-owner', password='test')
        self.client.force_login(user)
        tender = Tender.objects.create(purchase_number='status-details', notification_raw={'source': {}}, extras_checked_at=timezone.now())
        TenderComputeJob.objects.create(tender=tender, version='v2-document-manual', status='partial', diagnostics={'enrichment': [{'documents': {'considered': [{'name': 'spec.doc', 'reading_status': 'partial', 'error': '<script>unsafe()</script>'}]}}]})
        response = self.client.get(reverse('tender_selection:detail', args=[tender.pk]) + '?workspace=1')
        self.assertContains(response, 'id="analysis-status"')
        self.assertContains(response, 'spec.doc: &lt;script&gt;unsafe()&lt;/script&gt;')
        self.assertNotContains(response, '<script>unsafe()</script>')
