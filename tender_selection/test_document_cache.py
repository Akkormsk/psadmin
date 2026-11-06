import io
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import documents, services
from .models import DocumentPreview, Tender

CONTRACT_URL = "https://zakupki.gov.ru/contract"
SPEC_URL = "https://zakupki.gov.ru/spec"
PRICE_URL = "https://zakupki.gov.ru/price"


def _notification(prefix=""):
    def attachment(url, name, kind):
        return {"url": f"{url}{prefix}", "fileName": name, "docKindInfo": {"name": kind}}

    return {"source": {"attachmentsInfo": {"attachmentInfo": [
        attachment(PRICE_URL, "Обоснование НМЦК.xlsx", "Обоснование начальной цены"),
        attachment(CONTRACT_URL, "Проект контракта.docx", "Проект контракта"),
        attachment(SPEC_URL, "Описание объекта закупки.docx", "Описание объекта закупки"),
    ]}}}


def _sufficient():
    return {"data": {"risk_facts": {"documents_sufficient": "true", "execution_days": 90}, "legal_risks": "по контракту"}}


class DocumentHtmlTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="1")

    def test_cached_preview_is_used_without_going_to_eis(self):
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Проект контракта.docx", html="<p>Пени 1/300</p>")
        with patch.object(services, "_fetch_doc_bytes") as fetch:
            html = services.document_html(self.tender, CONTRACT_URL, "Проект контракта.docx")

        fetch.assert_not_called()
        self.assertEqual(html, "<p>Пени 1/300</p>")

    def test_fetched_document_is_cached_for_next_time(self):
        with patch.object(services, "_fetch_doc_bytes", return_value=b"raw"), \
                patch.object(services, "extract_preview", return_value={"kind": "docx", "html": "<p>текст</p>"}):
            html = services.document_html(self.tender, CONTRACT_URL, "Проект контракта.docx")

        self.assertEqual(html, "<p>текст</p>")
        self.assertEqual(DocumentPreview.objects.get(url=CONTRACT_URL).html, "<p>текст</p>")

    def test_document_cache_uses_full_archive_reader(self):
        with patch.object(services, "_fetch_doc_bytes", return_value=b"raw"), \
                patch.object(services, "extract_preview", return_value={"kind": "zip-content", "html": "<p>контракт</p>"}) as parse:
            services.document_html(self.tender, CONTRACT_URL, "Документы.zip")

        self.assertTrue(parse.call_args.kwargs["read_archive"])

    def test_stale_zip_preview_is_reparsed(self):
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Проект контракта.doc.zip", kind="zip", html="<p>Архив</p>")
        with patch.object(services, "_fetch_doc_bytes", return_value=b"raw"), \
                patch.object(services, "extract_preview", return_value={"kind": "pdf", "html": "<p>контракт</p>"}):
            html = services.document_html(self.tender, CONTRACT_URL, "Проект контракта.doc.zip")

        self.assertEqual(html, "<p>контракт</p>")
        self.assertEqual(DocumentPreview.objects.get(url=CONTRACT_URL).kind, "pdf")

    def test_old_doc_preview_is_reparsed_with_office_reader(self):
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Описание.doc", kind="doc", html="<p>мГБ</p>")
        with patch.object(services, "_fetch_doc_bytes", return_value=b"legacy"), \
                patch.object(services, "extract_preview", return_value={"kind": "doc", "html": "<section class='ts-doc-page'><p>Нормальный текст</p></section>"}):
            html = services.document_html(self.tender, CONTRACT_URL, "Описание.doc")
        self.assertIn("Нормальный текст", html)


class TenderOfficePreviewTests(TestCase):
    def test_archive_filename_bypasses_legacy_pdf_text_cache(self):
        self.client.force_login(self.admin)
        DocumentPreview.objects.create(url=CONTRACT_URL, kind="pdf", html="old extracted text")
        with patch("tender_selection.views._doc_by_idx", return_value=({"url": CONTRACT_URL, "name": "spec.docx.zip"}, None)), \
                patch("tender_selection.views._fetch_doc_bytes", return_value=b"archive"), \
                patch("tender_selection.views.extract_preview", return_value={"kind": "zip", "zip_entries": ["spec.docx"]}) as parse:
            response = self.client.get(f"/tender-selection/{self.tender.pk}/doc/1/")
        self.assertEqual(response.json()["zip_entries"], ["spec.docx"])
        self.assertTrue(parse.call_args.kwargs["browse_archive"])

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.admin = get_user_model().objects.create_superuser("office-admin", "office@example.test", "password")
        self.tender = Tender.objects.create(purchase_number="office-1", notification_raw=_notification())

    def test_legacy_document_is_served_as_private_inline_pdf(self):
        self.client.force_login(self.admin)
        with patch("tender_selection.views._fetch_doc_bytes", return_value=b"legacy"), \
                patch("tender_selection.office.convert_office", return_value=b"%PDF-test"):
            response = self.client.get(f"/tender-selection/{self.tender.pk}/doc/1/?format=pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-test")
        self.assertEqual(response["Cache-Control"], "private, no-store")


class ArchivePreviewTests(TestCase):
    def test_archive_browser_returns_links_even_for_single_file(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nested/spec.pdf", b"%PDF")
        result = documents.extract_preview(archive.getvalue(), "docs.zip", browse_archive=True)
        self.assertEqual(result["zip_entries"], ["nested/spec.pdf"])
        self.assertEqual(documents.extract_zip_entry(archive.getvalue(), "nested/spec.pdf"), b"%PDF")

    def test_nested_archive_can_be_read_without_extracting_paths(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as zf:
            zf.writestr("terms.html", "<p>Terms</p>")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nested.zip", nested.getvalue())
        result = documents.extract_preview(archive.getvalue(), "docs.zip", read_archive=True)
        self.assertIn("Terms", result["html"])

    def test_zip_with_doc_and_pdf_copies_uses_pdf(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Контракт.doc", b"old doc")
            zf.writestr("Контракт.doc.pdf", b"%PDF")

        with patch.object(documents, "_pdf_html", return_value="<p>контракт</p>"):
            result = documents.extract_preview(archive.getvalue(), "Контракт.doc.zip")

        self.assertEqual(result, {"kind": "pdf", "html": "<p>контракт</p>"})

    def test_archive_content_reads_supported_files_with_bounded_reader(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ТЗ.html", "<p>Техническое задание</p><script>alert(1)</script>")
            zf.writestr("Подпись.sig", b"signature")

        result = documents.extract_preview(archive.getvalue(), "Документы.zip", read_archive=True)

        self.assertEqual(result["kind"], "zip-content")
        self.assertIn("Техническое задание", result["html"])
        self.assertIn("Электронная подпись", result["html"])
        self.assertNotIn("alert(1)", result["html"])

    def test_image_attachment_is_rendered_as_safe_reduced_page(self):
        from PIL import Image

        source = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(source, format="PNG")
        result = documents.extract_preview(source.getvalue(), "scan.png")

        self.assertEqual(result["kind"], "png")
        self.assertIn('data:image/jpeg;base64,', result["html"])

    def test_html_attachment_keeps_only_safe_text_structure(self):
        result = documents.extract_preview(
            '<table><tr><td colspan="2">Условие</td></tr></table><iframe src="evil"></iframe><a href="evil">ссылка</a>'.encode(),
            "terms.html",
        )

        self.assertEqual(result["kind"], "html")
        self.assertIn('<td colspan="2">Условие</td>', result["html"])
        self.assertIn("ссылка", result["html"])
        self.assertNotIn("iframe", result["html"])
        self.assertNotIn("href=", result["html"])


class RiskAssessmentDocumentsTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(
            purchase_number="1", review=Tender.INTERESTING, notification_raw=_notification(),
        )
        DocumentPreview.objects.create(url=CONTRACT_URL, filename="Проект контракта.docx", html="<p>Пени 1/300</p>")
        DocumentPreview.objects.create(url=SPEC_URL, filename="Описание объекта закупки.docx", html="<p>Открытки</p>")

    def test_assessment_reads_prefetched_documents(self):
        with patch.object(services, "_fetch_doc_bytes") as fetch, \
                patch("tender_selection.risk_assessment.assess", return_value=_sufficient()) as assess:
            services.risk_assessment_for(self.tender)

        fetch.assert_not_called()
        self.assertIn("Пени 1/300", assess.call_args.args[0])
        self.assertEqual(self.tender.risk_assessment_docs, ["Проект контракта.docx", "Описание объекта закупки.docx"])

    def test_refresh_does_not_replace_full_assessment_with_worse_one(self):
        with patch("tender_selection.risk_assessment.assess", return_value=_sufficient()):
            services.risk_assessment_for(self.tender)
        DocumentPreview.objects.all().delete()

        insufficient = {"data": {"risk_facts": {"documents_sufficient": "false"}, "legal_risks": "без документов"}}
        with patch.object(services, "_fetch_doc_bytes", side_effect=services.DocumentError("ЕИС молчит")), \
                patch("tender_selection.risk_assessment.assess", return_value=insufficient):
            result = services.risk_assessment_for(self.tender, force=True)
        self.tender.refresh_from_db()

        self.assertEqual(result["legal_risks"], "по контракту")
        self.assertEqual(self.tender.risk_assessment["legal_risks"], "по контракту")


class PrefetchDocumentsTests(TestCase):
    def _tender(self, number, *, closes_in, prefix):
        return Tender.objects.create(
            purchase_number=number, notification_raw=_notification(prefix),
            collecting_finished_at=timezone.now() + closes_in,
        )

    def test_prefetches_contract_and_spec_of_open_tenders_nearest_deadline_first(self):
        self._tender("later", closes_in=timedelta(days=5), prefix="?later")
        self._tender("sooner", closes_in=timedelta(days=1), prefix="?sooner")
        self._tender("closed", closes_in=-timedelta(days=1), prefix="?closed")

        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            attempted, succeeded = services.retry_pending_documents(limit=3, pause=0)

        fetched = [call.args[1] for call in read.call_args_list]
        self.assertEqual((attempted, succeeded), (3, 3))
        self.assertEqual(fetched, [f"{CONTRACT_URL}?sooner", f"{SPEC_URL}?sooner", f"{CONTRACT_URL}?later"])

    def test_already_cached_documents_are_skipped(self):
        self._tender("sooner", closes_in=timedelta(days=1), prefix="")
        DocumentPreview.objects.create(url=CONTRACT_URL, html="<p>готово</p>")

        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            services.retry_pending_documents(limit=5, pause=0)

        self.assertEqual([call.args[1] for call in read.call_args_list], [SPEC_URL])


class UnreadableDocumentTests(TestCase):
    def test_unreadable_download_is_not_fetched_again(self):
        tender = Tender.objects.create(
            purchase_number="1", notification_raw=_notification(), collecting_finished_at=timezone.now() + timedelta(days=1),
        )
        DocumentPreview.objects.create(url=CONTRACT_URL, kind="rar", error="Формат не поддерживается для предпросмотра.")

        with patch.object(services, "_fetch_doc_bytes") as fetch:
            self.assertEqual(services.document_html(tender, CONTRACT_URL, "Проект контракта.rar"), "")
        fetch.assert_not_called()
        with patch.object(services, "document_html", return_value="<p>ok</p>") as read:
            services.retry_pending_documents(limit=5, pause=0)
        self.assertEqual([call.args[1] for call in read.call_args_list], [SPEC_URL])

    def test_archives_are_not_picked_for_risk_assessment(self):
        from .risk_assessment import select_documents

        picked = select_documents([
            {"name": "Проект контракта.rar", "kind": "Проект контракта", "url": "a"},
            {"name": "Проект контракта.docx", "kind": "Проект контракта", "url": "b"},
        ])
        self.assertEqual([doc["url"] for doc in picked], ["b"])

    def test_docx_wrapped_in_zip_is_still_picked(self):
        """ЕИС часто отдаёт сам .docx обёрнутым в .zip («Проект контракта.docx.zip») —
        это не архив-мусор, а тот же документ (распаковку умеет documents_html),
        и раньше он ошибочно отбрасывался наравне с .rar/.7z."""
        from .risk_assessment import select_documents

        picked = select_documents([
            {"name": "Проект ГПД.doc.zip", "kind": "Проект контракта", "url": "a"},
            {"name": "Описание объекта закупки (товара).docx.zip", "kind": "Описание объекта закупки", "url": "b"},
            {"name": "Требования к содержанию, составу заявки.docx.zip", "kind": "Требования к заявке", "url": "c"},
            {"name": "НМЦК.xlsx.zip", "kind": "Обоснование начальной (максимальной) цены контракта", "url": "d"},
        ])
        self.assertEqual({doc["url"] for doc in picked}, {"a", "b"})
