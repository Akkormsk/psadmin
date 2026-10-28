import io
import json
import zipfile
from unittest.mock import Mock, patch

from django.test import TestCase
from docx import Document

from tender_selection.documents import DocumentError, extract_preview
from tender_selection.models import DocumentPreview, Tender
from tender_selection.services import store_document_preview
from .calculation_v2_pipeline import GatewayDocumentEnricher
from .models import TenderComputeJob, TenderSourceItem


class DocumentReadingInputTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="reading-input")
        self.item = TenderSourceItem.objects.create(tender=self.tender, source_key="notification:1", original_text="Комплект")
        self.documents = [{"url": "https://example.test/spec", "name": "spec.docx"}]

    def docx(self, text):
        document = Document()
        document.add_paragraph(text)
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()

    def archive(self, files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return output.getvalue()

    @patch("tenders.services._ai_gateway_json", return_value=({"items": []}, {}))
    def test_missing_document_is_prepared_before_model_call(self, gateway):
        with patch("tender_selection.services._fetch_doc_bytes", return_value=self.docx("В комплекте блокнот и ручка")) as fetch:
            enricher = GatewayDocumentEnricher()
            enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        fetch.assert_called_once()
        self.assertIn("В комплекте блокнот и ручка", gateway.call_args.args[0])
        self.assertEqual(enricher.diagnostics["documents"]["considered"][0]["reading_status"], "read")

    def test_archive_analysis_reads_every_document_and_nested_archive(self):
        data = self.archive({"ТЗ.docx": self.docx("Блокнот"), "НМЦК.zip": self.archive({"Состав.docx": self.docx("Ручка")})})
        result = store_document_preview("https://example.test/archive", "documents.zip", data)
        self.assertEqual(result["kind"], "zip-content")
        self.assertIn("Блокнот", result["html"])
        self.assertIn("Ручка", result["html"])
        self.assertFalse(result["error"])
        self.assertEqual(extract_preview(data, "documents.zip", browse_archive=True)["kind"], "zip")

    @patch("tenders.services._ai_gateway_json")
    def test_archive_listing_and_preview_warnings_are_not_evidence(self, gateway):
        DocumentPreview.objects.create(url=self.documents[0]["url"], html="<p class='ts-sub'>В PDF нет извлекаемого текста</p>", kind="pdf")
        enricher = GatewayDocumentEnricher()
        result = enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        self.assertEqual(result, [])
        gateway.assert_not_called()
        self.assertEqual(enricher.diagnostics["outcome"], "document_reading_incomplete")
        DocumentPreview.objects.filter(url=self.documents[0]["url"]).update(kind="zip", html="<ul><li>Состав.docx</li></ul>")
        available, considered = enricher._bounded_documents(self.documents)
        self.assertEqual(available[0]["text"], "")
        self.assertEqual(considered[0]["reading_status"], "unreadable")

    def test_images_markup_and_truncation_are_reported_without_sending_base64(self):
        DocumentPreview.objects.create(url=self.documents[0]["url"], html='<p>Блокнот</p><img src="data:image/png;base64,AAAA"><p data-document-warning="truncated">…</p>', kind="docx")
        available, considered = GatewayDocumentEnricher()._bounded_documents(self.documents)
        self.assertEqual(available[0]["text"], "Блокнот")
        self.assertNotIn("base64", json.dumps(available))
        self.assertEqual(considered[0]["reading_status"], "partial")

    @patch("tenders.services._ai_gateway_json", return_value=({"items": []}, {}))
    def test_unavailable_file_does_not_hide_successful_source(self, gateway):
        DocumentPreview.objects.create(url=self.documents[0]["url"], html="<p>Блокнот</p>")
        documents = self.documents + [{"url": "https://example.test/missing", "name": "missing.docx"}]
        with patch("tender_selection.services._fetch_doc_bytes", side_effect=DocumentError("Недоступен")):
            enricher = GatewayDocumentEnricher()
            enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=documents)
        self.assertEqual(gateway.call_count, 1)
        self.assertEqual(enricher.diagnostics["outcome"], "document_reading_incomplete")
        self.assertIn("Блокнот", gateway.call_args.args[0])

    @patch("tenders.services._ai_gateway_json")
    def test_unreadable_input_is_partial_and_does_not_reach_router(self, gateway):
        from .analysis_presentation import resolve_tender_analysis_status
        from .calculation_v2_pipeline import run_next_tender_understanding_job

        self.tender.notification_raw = {"source": {
            "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [{"name": "Комплект", "quantity": {"value": "1"}}]}}},
            "attachmentsInfo": {"attachmentInfo": [{"url": "https://zakupki.gov.ru/synthetic-spec", "fileName": "spec.docx"}]},
        }}
        self.tender.save(update_fields=["notification_raw"])
        TenderComputeJob.objects.create(tender=self.tender, version="v2")
        router = Mock(cost_rub=0)
        with patch("tender_selection.services._fetch_doc_bytes", side_effect=DocumentError("Недоступен")):
            job = run_next_tender_understanding_job(router=router, enricher=GatewayDocumentEnricher())
        self.assertEqual(job.status, TenderComputeJob.Status.PARTIAL)
        self.assertEqual(resolve_tender_analysis_status(self.tender).code, "ANALYSIS_INCOMPLETE")
        gateway.assert_not_called()
        router.route.assert_not_called()

    def test_archive_warning_is_bounded_to_existing_database_field(self):
        data = self.archive({("unsupported" + str(index) + "x" * 90 + ".bin"): b"data" for index in range(10)})
        store_document_preview("https://example.test/error-archive", "files.zip", data)
        cached = DocumentPreview.objects.get(url="https://example.test/error-archive")
        self.assertTrue(cached.error)
        self.assertLessEqual(len(cached.error), DocumentPreview._meta.get_field("error").max_length)

    @patch("tenders.services._ai_gateway_json")
    def test_explicit_budget_stops_before_paid_request(self, gateway):
        DocumentPreview.objects.create(url=self.documents[0]["url"], html="<p>Блокнот</p>")
        enricher = GatewayDocumentEnricher(max_cost_rub="0.0001")
        enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        self.assertEqual(enricher.diagnostics["outcome"], "budget_blocked")
        gateway.assert_not_called()

    @patch("tenders.services._ai_gateway_json", return_value=({"items": []}, {}))
    def test_repeated_names_retain_notification_positions_and_prices(self, gateway):
        from .calculation_v2_pipeline import ingest_source_items
        rows = [{"name": "Одинаковое название", "quantity": {"value": "100"}, "price": price, "sum": str(int(price) * 100)} for price in ["2410", "3700", "2770"]]
        self.tender.notification_raw = {"source": {"notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": rows}}}}}
        self.tender.save(update_fields=["notification_raw"])
        items = ingest_source_items(self.tender)
        DocumentPreview.objects.create(url=self.documents[0]["url"], html="<p>Три различных состава</p>")
        GatewayDocumentEnricher().extract_tender(tender=self.tender, source_items=items, documents=self.documents)
        payload = json.loads(gateway.call_args.args[0].split("\n", 1)[1])
        self.assertEqual([row["notification_position"] for row in payload["notification_items"]], [1, 2, 3])
        self.assertEqual([row["notification_unit_price"] for row in payload["notification_items"]], ["2410", "3700", "2770"])

    def test_every_document_retains_text_beyond_old_equal_share_limit(self):
        documents = []
        for index in range(6):
            url = f"https://example.test/complete-{index}"
            text = "Подробные характеристики изделия. " * 300 + f" КОНЕЦ_{index}"
            DocumentPreview.objects.create(url=url, kind="docx", html=f"<p>{text}</p>")
            documents.append({"url": url, "name": f"Document {index}"})
        available, considered = GatewayDocumentEnricher()._bounded_documents(documents)
        self.assertGreater(sum(len(doc["text"]) for doc in available), 18000)
        for index, (doc, diagnostic) in enumerate(zip(available, considered)):
            self.assertTrue(doc["text"].endswith(f"КОНЕЦ_{index}"))
            self.assertEqual(diagnostic["cached_text_chars"], diagnostic["sent_text_chars"])
            self.assertEqual(diagnostic["reading_status"], "read")

    def test_oversized_preview_is_rejected_instead_of_silently_clipped(self):
        DocumentPreview.objects.create(url=self.documents[0]["url"], html="<p>Длинный документ</p>")
        with patch("tender_selection.documents.MAX_BYTES", 8):
            with self.assertRaises(ValueError):
                GatewayDocumentEnricher()._bounded_documents(self.documents)
