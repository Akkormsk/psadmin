import io
import json
import zipfile
from unittest.mock import Mock, patch

from django.test import TestCase, SimpleTestCase
from docx import Document

from tender_selection.documents import DocumentError, extract_preview
from tender_selection.models import DocumentPreview, Tender
from tender_selection.services import store_document_preview
from .calculation_v2_pipeline import GatewayDocumentEnricher
from .models import TenderComputeJob, TenderSourceItem


class DocumentReadingInputTests(TestCase):
    @patch('tenders.services._ai_gateway_json')
    def test_incomplete_answer_is_kept_in_private_diagnostics_without_creating_items(self, gateway):
        from .services import TenderAIError
        DocumentPreview.objects.create(url=self.documents[0]['url'], html='<p>Документ</p>')
        error = TenderAIError('Ответ оборван')
        error.usage = {'prompt_tokens': 100, 'completion_tokens': 32000}
        error.incomplete_response = {'content': '{"items":[', 'output_token_limit': 32000}
        gateway.side_effect = error
        enricher = GatewayDocumentEnricher()
        with self.assertRaises(TenderAIError):
            enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        self.assertEqual(enricher.diagnostics['incomplete_response'], error.incomplete_response)
        self.assertEqual(enricher.diagnostics['usage'], error.usage)
        self.assertTrue(gateway.call_args.kwargs['capture_incomplete_response'])
        self.assertEqual(self.tender.v2_source_items.count(), 1)

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
        self.assertNotIn('reasoning_effort', gateway.call_args.kwargs)
        self.assertEqual(gateway.call_args.kwargs['thinking'], {'type': 'disabled'})
        self.assertIsNone(GatewayDocumentEnricher(model='other/model').reasoning_effort)

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
        router = Mock(cost_rub=0, diagnostics={})
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


class InlineDocumentVisionTests(TestCase):
    def setUp(self):
        import base64
        from PIL import Image
        self.tender = Tender.objects.create(purchase_number='inline-vision', notification_raw={'source': {}})
        self.item = TenderSourceItem.objects.create(tender=self.tender, source_key='source', source_type='notification', original_text='Item', quantity=1)
        image = io.BytesIO(); Image.new('RGB', (64, 64), 'white').save(image, format='PNG')
        self.image_url = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode('ascii')
        self.documents = [{'url': 'https://example.test/vision', 'name': 'Document.docx'}]
        DocumentPreview.objects.create(url=self.documents[0]['url'], kind='docx', html=f'<p>Text facts</p><img src="{self.image_url}">')

    @patch('tenders.services._ai_gateway_json', return_value=({'items': []}, {'prompt_tokens': 100, 'completion_tokens': 10}))
    def test_picture_is_forwarded_in_the_same_call_and_bound_to_its_document(self, gateway):
        enricher = GatewayDocumentEnricher()
        enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        gateway.assert_called_once()
        self.assertEqual(gateway.call_args.kwargs['image_data_urls'], [self.image_url])
        self.assertNotIn('base64', gateway.call_args.args[0])
        self.assertIn('"image_indexes": [1]', gateway.call_args.args[0])
        considered = enricher.diagnostics['documents']['considered'][0]
        self.assertEqual(considered['reading_status'], 'read')
        self.assertEqual(considered['image_count'], 1)

    @patch('tenders.services._ai_gateway_json')
    def test_image_allowance_is_reserved_before_any_paid_call(self, gateway):
        enricher = GatewayDocumentEnricher(max_cost_rub='0.00001')
        enricher.extract_tender(tender=self.tender, source_items=[self.item], documents=self.documents)
        self.assertEqual(enricher.diagnostics['outcome'], 'budget_blocked')
        gateway.assert_not_called()
        with_image = enricher._preflight_cost('prompt')[1]['prompt_tokens']
        enricher.document_images = []
        self.assertGreater(with_image, enricher._preflight_cost('prompt')[1]['prompt_tokens'])

    def test_external_picture_is_never_fetched_and_remains_a_warning(self):
        DocumentPreview.objects.filter(url=self.documents[0]['url']).update(html='<p>Facts</p><img src="https://external.test/picture">')
        enricher = GatewayDocumentEnricher()
        available, considered = enricher._bounded_documents(self.documents)
        self.assertFalse(enricher.document_images)
        self.assertEqual(considered[0]['reading_status'], 'partial')
        self.assertTrue(considered[0]['warnings'])


class StagedTableEvidenceTests(SimpleTestCase):
    def test_row_evidence_preserves_values_and_rejects_cross_row_or_reordered_cells(self):
        engine = GatewayDocumentEnricher()
        engine.document_lookup = {1: {'url': 'https://example.test/spec', 'evidence_text': '',
            'tables': [{'rows': [['', 'Размер', '≥ 10 и ≤ 12', 'мм'], ['Цвет', 'синий']]}]}}
        proof = engine.stage_quote({'doc_id': 1, 'quote': 'Размер | ≥ 10 и ≤ 12 | мм'})
        self.assertEqual(proof['evidence'], 'Размер | ≥ 10 и ≤ 12 | мм')
        self.assertEqual(proof['page_or_section'], 'Таблица 1, строка 1')
        for quote in ('Размер | синий', 'Размер | ≥ 11 и ≤ 12 | мм', 'мм | Размер'):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                engine.stage_quote({'doc_id': 1, 'quote': quote})


    def test_multi_cell_specs_accept_inequality_glyph_equivalents_only_within_one_row(self):
        engine = GatewayDocumentEnricher()
        engine.document_lookup = {3: {'url': 'https://example.test/spec', 'evidence_text': '',
            'tables': [{'rows': [
                ['Блокнот: Размер (длина)', '≥ 210 и ⩽ 240', 'Миллиметр'],
                ['Блокнот: Формат А5', 'тип', 'недатированный'],
                ['Тип', 'шариковая ручка'],
                ['Размер', '≥ 10 и ≤ 12', 'мм'],
                ['Ручка: Длина', '≥ 13.5 и ⩽ 15', 'Сантиметр'],
            ]}]}}
        for quote, row in [
            ('Блокнот: Размер (длина) | 210 и ≤ 240 | мм', 1),
            ('Блокнот: Формат | А5', 2),
            ('Тип | шариковая ручка', 3),
            ('Размер | ≥ 10 и ≤ 12 | мм', 4),
            ('Ручка: Длина | 13,5 ≤ 15 | см', 5),
        ]:
            with self.subTest(quote=quote):
                proof = engine.stage_quote({'doc_id': 3, 'quote': quote})
                self.assertEqual(proof['page_or_section'], f'Таблица 1, строка {row}')
                self.assertNotIn('Сочи', proof['evidence'])

        for invented in [
            'Блокнот: Размер (длина) | 210 и ≤ 241 | мм',
            'Блокнот: Размер (длина) | 210 и ≥ 240 | мм',
            'Блокнот: Размер (длина) | 210 и ≤ 240 | см',
            'Тип | ≥ 10 и ≤ 12',
            'Ручка: Длина | 13,5 ≤ 16 | см',
            'Ручка: Длина | 13,5 ≥ 15 | см',
            'Блокнот: Формат | шариковая ручка',
        ]:
            with self.subTest(invented=invented), self.assertRaises(ValueError):
                engine.stage_quote({'doc_id': 3, 'quote': invented})


class StagedResponseCacheTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="staged-response-cache")
        self.engine = GatewayDocumentEnricher()
        self.engine.shared_context = '{}'
        self.engine.document_images = []

    def fingerprint(self):
        from .calculation_v2_pipeline import _fingerprint
        return _fingerprint({
            'contract': self.engine.STAGED_CONTRACT, 'context': '{}',
            'task': 'Read TZ\n{"goods": []}', 'model': self.engine.model,
            'thinking': self.engine.thinking, 'effort': self.engine.reasoning_effort,
        })

    def create_job(self, *, state=None, version=None, rows=None):
        stage = {'state': state, 'input_fingerprint': self.fingerprint(),
                 'validation_version': version, 'result': rows} if state else None
        return TenderComputeJob.objects.create(
            tender=self.tender, status="partial",
            diagnostics={'stages': {'tz': stage}} if stage else {},
        )

    @patch('tenders.services._ai_gateway_json')
    def test_old_invalid_response_is_revalidated_for_free_even_if_latest_failure_is_not_reusable(self, gateway):
        old = self.create_job(state='failed', version='old-table-v0', rows=[{'key': 'old-quote'}])
        self.create_job(state='failed', version=self.engine.STAGED_VALIDATION_VERSION,
                        rows=[{'key': 'already-rejected'}])
        current = self.create_job()
        rows, metrics = self.engine.ask_stage(current, 'tz', 'Read TZ', {'goods': []}, output_tokens=500)
        self.assertEqual(rows, [{'key': 'old-quote'}])
        self.assertEqual(metrics['state'], 'pending_validation')
        self.assertEqual(metrics['reused_from'], old.pk)
        self.assertEqual(metrics['cost_rub'], '0')
        gateway.assert_not_called()

    @patch('tenders.services._ai_gateway_json')
    def test_completed_response_preferred_over_failed_revalidation(self, gateway):
        complete = self.create_job(state='complete', version='old-version', rows=[{'key': 'verified'}])
        self.create_job(state='failed', version='old-table-v0', rows=[{'key': 'unverified'}])
        current = self.create_job()
        rows, metrics = self.engine.ask_stage(current, 'tz', 'Read TZ', {'goods': []}, output_tokens=500)
        self.assertEqual(rows, [{'key': 'verified'}])
        self.assertEqual(metrics['state'], 'complete')
        self.assertEqual(metrics['reused_from'], complete.pk)
        gateway.assert_not_called()
