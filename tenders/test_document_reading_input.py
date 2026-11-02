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



class StagedMultiSourceReconciliationTests(SimpleTestCase):
    def make_engine(self):
        engine = GatewayDocumentEnricher()
        engine.baselines = {7: [
            {'id': 0, 'name': 'Цвет уголков', 'value': 'золотой'},
            {'id': 1, 'name': 'Материал ручки', 'value': 'металл'},
        ]}
        engine.stage_sources = {}
        engine.document_lookup = {1: {'url': 'https://example.test/spec', 'evidence_text': 'Цвет уголков золотой и серебряный',
            'tables': [{'rows': [['Цвет уголков', 'золотой и серебряный']]}]}}
        return engine

    def test_kit_facts_from_notification_are_retained_without_a_document_quote(self):
        engine = self.make_engine()
        goods = [
            {'key': '7:0', 'source_id': 7, 'kind': 'kit'},
            {'key': '7:1', 'source_id': 7, 'kind': 'kit'},
        ]
        rows = [
            {'key': '7:0', 'keep_refs': [], 'characteristics': [
                {'name': 'Цвет уголков', 'value': 'золотой', 'resolution': 'new', 'baseline_refs': [], 'notification_refs': [0], 'scope': 'product'}]},
            {'key': '7:1', 'keep_refs': [], 'characteristics': [
                {'name': 'Материал ручки', 'value': 'металл', 'resolution': 'new', 'baseline_refs': [], 'notification_refs': [1], 'scope': 'product'}]},
        ]
        result = engine.validate_specifications(rows, goods)
        self.assertEqual(result['7:0']['characteristics'][0]['source'], 'notification')
        self.assertEqual(result['7:0']['characteristics'][0]['source_facts'][0]['name'], 'Цвет уголков')
        self.assertEqual(result['7:1']['characteristics'][0]['source'], 'notification')
        self.assertNotIn('doc_id', result['7:0']['characteristics'][0])

    def test_sources_survive_canonical_compilation_and_display(self):
        from types import SimpleNamespace
        from .document_enrichment import effective_item_requirements, characteristic_display
        engine = self.make_engine()
        goods = [{'key': '7:0', 'source_id': 7, 'kind': 'kit'}]
        for document in (False, True):
            fact = {'name': 'Цвет уголков', 'value': 'золотой', 'resolution': 'new', 'baseline_refs': [], 'notification_refs': [0]}
            if document:
                fact.update(value='золотой и серебряный', resolution='merge', reason='Документ уточняет исходное значение', doc_id=1, quote='Цвет уголков | золотой и серебряный')
            compiled = engine.validate_specifications([{'key': '7:0', 'characteristics': [fact]}], goods)['7:0']
            source = SimpleNamespace(pk=1, source_type='document_proposal', metadata={'relationship': 'component'}, parent_id=None, requirements=compiled, provenance={})
            canonical = effective_item_requirements(source, strict=True)
            self.assertTrue(canonical['compiled_reconciliation'])
            row = canonical['characteristics'][0]
            self.assertEqual(row['source'], 'combined' if document else 'notification')
            self.assertTrue(any(item['source'] == 'notification' for item in row['source_facts']))
            self.assertEqual(any(part['added'] for part in characteristic_display(row)['parts']), document)
            self.assertEqual(effective_item_requirements(source, strict=True), canonical)

    def test_no_unverified_changes_or_fact_leak_between_components(self):
        engine = self.make_engine()
        goods = [{'key': '7:0', 'source_id': 7, 'kind': 'kit'},
                 {'key': '7:1', 'source_id': 7, 'kind': 'kit'}]
        source = {'key': '7:0', 'keep_refs': [], 'characteristics': [
            {'name': 'Цвет уголков', 'value': 'серебряный', 'resolution': 'new', 'baseline_refs': [], 'notification_refs': [0]}]}
        with self.assertRaisesRegex(ValueError, 'изменено'):
            engine.validate_specifications([source, {'key': '7:1', 'characteristics': []}], goods)
        source['characteristics'][0]['value'] = 'золотой'
        duplicate = {'key': '7:1', 'characteristics': [{
            'name': 'Цвет уголков', 'value': 'золотой', 'resolution': 'new', 'baseline_refs': [], 'notification_refs': [0]}]}
        with self.assertRaisesRegex(ValueError, 'разным товарам'):
            engine.validate_specifications([source, duplicate], goods)
        source['characteristics'][0]['value'] = 'серебряный'
        source['characteristics'][0]['notification_refs'] = []
        with self.assertRaisesRegex(ValueError, 'не подтверждена'):
            engine.validate_specifications([source, {'key': '7:1', 'characteristics': []}], goods)

    def test_kit_uses_both_evidences_when_the_document_changes_a_fact(self):
        engine = self.make_engine()
        good = [{'key': '7:0', 'source_id': 7, 'kind': 'kit'}]
        rows = [{'key': '7:0', 'characteristics': [{
            'name': 'Цвет уголков', 'value': 'золотой и серебряный', 'resolution': 'merge',
            'reason': 'Документ уточняет исходное значение', 'baseline_refs': [], 'notification_refs': [0],
            'doc_id': 1, 'quote': 'Цвет уголков | золотой и серебряный'}]}]
        result = engine.validate_specifications(rows, good)
        self.assertEqual(result['7:0']['characteristics'][0]['source'], 'combined')
        self.assertEqual(result['7:0']['characteristics'][0]['source_facts'][0]['source'], 'notification')
        self.assertEqual(result['7:0']['characteristics'][0]['provenance']['document_url'], 'https://example.test/spec')



class StagedSingleSourceSelectionTests(SimpleTestCase):
    def test_readable_label_keeps_original_value_and_color_without_a_second_source(self):
        from .document_enrichment import characteristic_display
        self.engine.baselines[9] = [{'id': 0, 'name': 'Цвет корпуса изделия', 'value': 'синий'}]
        self.engine.stage_sources[9].requirements = {'characteristics': [{'name': 'Цвет корпуса изделия', 'value': 'синий'}]}
        rows = [{'key': '9:0', 'characteristics': [{
            'name': 'Цвет корпуса', 'value': 'синий', 'resolution': 'merge',
            'baseline_refs': [0], 'reason': 'Краткое название свойства'}]}]
        canonical = self.engine.validate_specifications(rows, self.goods)['9:0']
        fact = canonical['characteristics'][0]
        self.assertEqual(fact['name'], 'Цвет корпуса')
        self.assertEqual(fact['value'], 'синий')
        self.assertFalse(characteristic_display(fact)['name_added'])
        rows[0]['characteristics'][0]['value'] = 'красный'
        with self.assertRaisesRegex(ValueError, 'без подтверждения'):
            self.engine.validate_specifications(rows, self.goods)

    """A source-backed new fact must not require several documents."""

    def setUp(self):
        self.engine = GatewayDocumentEnricher()
        self.engine.baselines = {9: []}
        self.engine.stage_sources = {9: TenderSourceItem(
            source_type="notification", original_text="Наградная атрибутика",
            requirements={"characteristics": []},
        )}
        self.engine.document_lookup = {1: {
            "url": "https://example.test/tz",
            "evidence_text": "",
            "tables": [{"rows": [["Материал основания", "металл"]]}],
        }}
        self.goods = [{"key": "9:0", "source_id": 9, "kind": "finished",
                       "provenance": {"kind": "notification", "source_item_id": 9}}]

    def test_one_document_marked_merge_is_treated_as_new(self):
        rows = [{"key": "9:0", "keep_refs": [], "characteristics": [{
            "name": "Материал основания",
            "value": "металл",
            "resolution": "merge",
            "baseline_refs": [],
            "doc_id": 1,
            "quote": "Материал основания | металл",
            "reason": "Наиболее полная характеристика",
        }]}]
        result = self.engine.validate_specifications(rows, self.goods)
        actual = result["9:0"]["characteristics"][0]
        self.assertEqual(actual["source"], "document")
        self.assertEqual(actual["provenance"]["document_url"], "https://example.test/tz")
        self.assertEqual(actual["value"], "металл")
        from .document_enrichment import merge_item_requirements
        saved = merge_item_requirements({"characteristics": []}, result["9:0"])
        self.assertEqual(saved["characteristics"][0]["value"], "металл")
        self.assertEqual(saved["characteristics"][0]["source"], "document")

    def test_single_document_marked_conflict_is_not_an_unproved_disagreement(self):
        rows = [{"key": "9:0", "keep_refs": [], "characteristics": [{
            "name": "Материал основания", "value": "металл",
            "resolution": "conflict", "baseline_refs": [],
            "doc_id": 1, "quote": "Материал основания | металл",
            "reason": "Модель увидела различие без второй ссылки",
        }]}]
        result = self.engine.validate_specifications(rows, self.goods)
        actual = result["9:0"]["characteristics"][0]
        self.assertEqual(actual["source"], "document")
        self.assertIn("не подтверждено вторым источником", actual["reason"])
        from .document_enrichment import merge_item_requirements
        saved = merge_item_requirements({"characteristics": []}, result["9:0"])
        self.assertEqual(saved["characteristics"][0]["value"], "металл")
        self.assertFalse(saved["characteristics"][0].get("conflict", False))

    def test_two_independently_verified_conflicting_values_still_show_conflict(self):
        self.engine.document_lookup[2] = {
            "url": "https://example.test/appendix", "evidence_text": "",
            "tables": [{"rows": [["Материал основания", "пластик"]]}],
        }
        rows = [{"key": "9:0", "keep_refs": [], "characteristics": [
            {"name": "Материал основания", "value": "металл", "resolution": "conflict",
             "baseline_refs": [], "doc_id": 1, "quote": "Материал основания | металл"},
            {"name": "Материал основания", "value": "пластик", "resolution": "conflict",
             "baseline_refs": [], "doc_id": 2, "quote": "Материал основания | пластик"},
        ]}]
        result = self.engine.validate_specifications(rows, self.goods)
        from .document_enrichment import merge_item_requirements
        saved = merge_item_requirements({"characteristics": []}, result["9:0"])
        self.assertEqual(len(saved["characteristics"]), 1)
        self.assertIn("металл", saved["characteristics"][0]["value"])
        self.assertIn("пластик", saved["characteristics"][0]["value"])
        self.assertTrue(all(fact["conflict"] for fact in saved["characteristics"]))

    def test_real_merge_preserves_baseline_and_combines(self):
        self.engine.baselines[9] = [{"id": 0, "name": "Материал основания", "value": "металл"}]
        self.engine.stage_sources[9].requirements = {
            "characteristics": [{"name": "Материал основания", "value": "металл"}]
        }
        rows = [{"key": "9:0", "keep_refs": [], "characteristics": [{
            "name": "Материал основания",
            "value": "металл с покрытием",
            "resolution": "merge",
            "baseline_refs": [0],
            "doc_id": 1,
            "quote": "Материал основания | металл",
            "reason": "Документ уточняет покрытие",
        }]}]
        result = self.engine.validate_specifications(rows, self.goods)
        self.assertTrue(result["9:0"]["compiled_reconciliation"])
        from .document_enrichment import merge_item_requirements
        saved = merge_item_requirements(self.engine.stage_sources[9].requirements, result["9:0"])
        self.assertEqual(saved["characteristics"][0]["source"], "combined")

    def test_every_additional_document_has_verified_evidence(self):
        self.engine.document_lookup[2] = {'url': 'https://example.test/second', 'evidence_text': 'Покрытие матовое', 'tables': []}
        fact = {'name': 'Материал основания', 'value': 'металл с матовым покрытием', 'resolution': 'merge',
                'reason': 'Совместимые требования', 'baseline_refs': [], 'document_facts': [
                    {'doc_id': 1, 'quote': 'Материал основания | металл', 'value': 'металл'},
                    {'doc_id': 2, 'quote': 'Покрытие матовое', 'value': 'матовое покрытие'}]}
        rows = [{'key': '9:0', 'characteristics': [fact]}]
        canonical = self.engine.validate_specifications(rows, self.goods)['9:0']
        proofs = canonical['characteristics'][0]['source_facts']
        self.assertEqual({row['provenance']['document_url'] for row in proofs}, {'https://example.test/tz', 'https://example.test/second'})
        fact['document_facts'][1]['quote'] = 'Покрытие глянцевое'
        with self.assertRaisesRegex(ValueError, 'не подтверждён'):
            self.engine.validate_specifications(rows, self.goods)

    def test_unsupported_single_source_is_still_rejected(self):
        rows = [{"key": "9:0", "keep_refs": [], "characteristics": [{
            "name": "Материал основания", "value": "латунь",
            "resolution": "merge", "baseline_refs": [],
            "doc_id": 1, "quote": "Материал основания | латунь",
        }]}]
        with self.assertRaisesRegex(ValueError, "не подтверждён"):
            self.engine.validate_specifications(rows, self.goods)


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
