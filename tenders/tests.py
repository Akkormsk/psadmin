import json
import zipfile
from io import BytesIO, StringIO
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from docx import Document
from openpyxl import Workbook

from calculator.models import CalculatorSettings, PriceItem
from . import views as tender_views
from .models import CatalogCategory, CatalogMatchDecision, CatalogProduct, CatalogSupplier, CatalogSyncRun, Lesson, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, RequirementSkipRule, TenderEstimate, TenderKnowledgeSource, TenderSettings
from .catalog import CatalogSyncError, GiftsXmlClient, OasisClient, _category_candidates, catalog_candidates_for_line, parse_gifts_catalog, sync_gifts_catalog, sync_gifts_categories, sync_oasis_catalog
from .services import _VisibleTextParser, _collapse_requirements, _evaluate_cost_recipe, _format_html_tables, _json_from_model, _knowledge_sources_for_line, _normalize_catalog_intent, _normalize_training_hypothesis, _paper_candidates, _parse_document_decimal, _resolve_line_match, _run_shortlist_pass, _select_html_price_quote, _shorten_structured_item_names, _source_text_quality, _strip_shared_item_boilerplate, _technical_source_chunks, _validate_public_url, analyze_tender_requirements, apply_catalog_candidate, apply_verified_source_quote, build_training_hypothesis, calculate_sheet_imposition, calculate_tender, detect_tender_document_type, extract_tender_source, inspect_tender_document, recognize_tender_items, TenderAIError


class TenderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="manager", password="password")
        self.other = get_user_model().objects.create_user(username="other", password="password")
        self.payload = [{"name": "Ручка", "quantity": "10", "nmck_unit": "100", "material_unit": "40", "application_unit": "10", "logistics_unit": "5", "product_url": "https://example.com/item", "comment": "Синяя"}]

    def test_smart_upload_has_separate_nmck_and_technical_fields(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, 'id="tender-ai-nmck-file"')
        self.assertContains(response, 'id="tender-ai-technical-file"')
        self.assertNotContains(response, 'id="tender-tech-modal"')

    def test_pending_nmck_rows_show_technical_status_and_use_duplicate_guard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, "data-ai-technical-status")
        self.assertContains(response, "function applyTechnicalResultToNmckRows")
        self.assertContains(response, "function findExistingTenderLine")

    def test_line_ai_button_opens_drawer_and_starts_hypothesis_immediately(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, "openRequirements=(index,autoCalculate=false)=>")
        self.assertContains(response, "questionControl.onclick=()=>openRequirements(index,true)")
        self.assertContains(response, "shouldAutoCalculate=autoCalculate&&!info.production")
        self.assertContains(response, "if(shouldAutoCalculate)build.click()")

    def test_catalog_ui_distinguishes_empty_results_from_supplier_failure(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, "catalogSourceStatusHtml")
        self.assertContains(response, "Каталог временно недоступен")
        self.assertContains(response, "выполнен поиск по названию и описанию")

    def test_assistant_calculation_has_sticky_totals_and_scoped_loading_state(self):
        self.client.force_login(self.user)

        content = self.client.get(reverse("tender_home")).content.decode()
        styles = (Path(__file__).resolve().parents[1] / "static" / "core" / "index.css").read_text(encoding="utf-8")

        self.assertIn("data-route-unit-total", content)
        self.assertIn("data-route-order-total", content)
        self.assertIn("function updateRouteToolbar", content)
        self.assertIn("function setProductionBusy", content)
        self.assertIn("aria-busy", content)
        self.assertIn(".tender-production-header { position:fixed", styles)
        self.assertIn(".tender-production-result.is-loading::after", styles)
        self.assertIn(".is-loading-button::before", styles)
        self.assertIn("bottom:0", styles)

    def test_smart_import_finishes_in_the_product_list_for_nmck_or_technical_document(self):
        self.client.force_login(self.user)

        content = self.client.get(reverse("tender_home")).content.decode()

        nmck_apply = content[content.index("document.getElementById('tender-ai-add').onclick"):]
        technical_apply = content[content.index("function applyTechnicalResult(result,sourceName)"):]
        self.assertIn("aiModal.classList.remove('is-open')", nmck_apply)
        self.assertIn("aiModal.classList.remove('is-open')", technical_apply)
        self.assertIn("function isPristineTenderLine", content)

    def test_cost_row_places_comment_and_link_before_assistant_action(self):
        self.client.force_login(self.user)

        content = self.client.get(reverse("tender_home")).content.decode()

        expected_order = "${field('Комментарий','comment','text','Необязательно')}${linkControl()}${questionButton(line)}"
        self.assertIn(expected_order, content)

    def test_assistant_deduplicates_questions_and_highlights_route(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))
        styles = (Path(__file__).resolve().parents[1] / "static" / "core" / "index.css").read_text(encoding="utf-8")

        self.assertContains(response, "function uniqueAssistantQuestions")
        # The route is one block-diagram now — a vertical column of numbered
        # blocks joined by connectors, each anchor-linking to its own step
        # section — not a route-name string plus a duplicate row of numbered
        # chips. Each production step is its own collapsible block.
        self.assertIn(".training-route__flow", styles)
        self.assertIn(".training-step-block", styles)
        self.assertNotIn(".training-dialogue__route span b", styles)

    def test_line_assistant_button_matches_compact_metric_height(self):
        styles = (Path(__file__).resolve().parents[1] / "static" / "core" / "index.css").read_text(encoding="utf-8")

        self.assertIn(".tender-line-questions.ai-route-button { min-height:27px", styles)

    def test_assistant_dialogue_route_block_layout(self):
        self.client.force_login(self.user)

        content = self.client.get(reverse("tender_home")).content.decode()
        fn = content[content.index("function trainingDialogueHtml"):][:5000]
        markup = fn[fn.index("return `"):]

        # Route block in the rendered markup: reason text, then the flow
        # diagram, then a collapsed "Исправить маршрут" — the feedback box is
        # tucked away, not on screen at rest.
        r, f, x = markup.index("training-route__reason"), markup.index("${flowHtml}"), markup.index("training-route__fix")
        self.assertLess(r, f)
        self.assertLess(f, x)
        # Accepted search rules ("Ваши корректировки") sit with the catalog
        # feedback box inside the product step, not in the route block.
        self.assertIn("${catalogSuggestionsHtml(result)}${changesHtml}${feedbackWidgetHtml('catalog'", fn)
        self.assertIn("Ваши корректировки", fn)
        # The standalone "add supplier" block is gone while the route is frozen.
        self.assertNotIn("Добавить поставщика или источник", fn)

    def test_each_dialogue_block_has_its_own_scoped_feedback_box(self):
        self.client.force_login(self.user)

        content = self.client.get(reverse("tender_home")).content.decode()

        # The route block and the catalog step each render the reusable
        # feedback widget, and its "Учесть и пересчитать" tells the backend
        # which block to recompute (data-feedback-scope) — no LLM guesses.
        self.assertIn("feedbackWidgetHtml('route'", content)
        self.assertIn("feedbackWidgetHtml('catalog'", content)
        self.assertIn("data-feedback-scope", content)
        self.assertIn("runRevise({feedback,scope}", content)
        # One finalize button on the sticky bar replaces the old trio.
        self.assertIn("data-finalize-training", content)
        self.assertNotIn("data-revise-training", content)
        self.assertNotIn("data-confirm-training", content)
        # Both step blocks are collapsed at rest — the product block is not
        # special, just opened by clicking it or its route-diagram anchor.
        self.assertNotIn('id="training-step-${index}"${open?', content)

    def test_formula_includes_every_expense_in_roi(self):
        _, result = calculate_tender([{**self.payload[0], **{key: Decimal(self.payload[0][key]) for key in ("quantity", "nmck_unit", "material_unit", "application_unit", "logistics_unit")}}], Decimal("30"), Decimal("100"), Decimal("5"))
        self.assertEqual(result["rrp_total"], Decimal("700.00"))
        self.assertEqual(result["vat"], Decimal("35.00"))
        self.assertEqual(result["all_expenses"], Decimal("685.00"))
        self.assertEqual(result["net_profit"], Decimal("15.00"))
        self.assertEqual(result["roi"], Decimal("2.19"))

    def test_user_can_save_and_open_own_estimate(self):
        self.client.force_login(self.user)
        payload = [{**self.payload[0], "requirements": {"requirements": [{"label": "Материал", "value": "пластик"}], "questions": []}}]
        analysis = {"technical": {"name": "ТЗ.pdf", "matched": 1, "questions": 0}}
        response = self.client.post(reverse("tender_home"), {"tender_number": "123", "name": "Тест", "reduction_percent": "30", "russia_delivery": "100", "lines_json": json.dumps(payload), "document_analysis_json": json.dumps(analysis)})
        self.assertEqual(response.status_code, 302)
        estimate = TenderEstimate.objects.get()
        self.assertEqual(estimate.owner, self.user)
        self.assertEqual(estimate.lines.count(), 1)
        self.assertEqual(estimate.vat_rate_snapshot, Decimal("5.00"))
        self.assertFalse(estimate.summary_snapshot["is_incomplete"])
        self.assertEqual(estimate.document_analysis["technical"]["matched"], 1)
        self.assertEqual(estimate.lines.get().requirements["requirements"][0]["value"], "пластик")

    def test_saved_estimate_list_shows_status_selector_without_draft_exclamation(self):
        TenderEstimate.objects.create(
            owner=self.user,
            tender_number="123",
            name="Тест",
            summary_snapshot={"is_incomplete": True, "net_profit": "1000", "roi": "10"},
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_home"))

        self.assertContains(response, 'class="saved-estimate__status-form is-draft"')
        self.assertContains(response, 'data-estimate-status-form')
        self.assertNotContains(response, 'onchange="this.form.submit()"')
        self.assertContains(response, 'const data=new FormData(form);')
        self.assertContains(response, 'body:data')
        self.assertContains(response, '<option value="draft" selected>Черновик</option>', html=True)
        self.assertContains(response, '<option value="pending">В ожидании</option>', html=True)
        self.assertContains(response, '<option value="not_participated">Не участвовали</option>', html=True)
        self.assertContains(response, '<option value="lost">Проигран</option>', html=True)
        self.assertContains(response, '<option value="won">Выигран</option>', html=True)
        self.assertNotContains(response, 'class="saved-estimate__draft"')

    def test_status_selector_stays_a_compact_pill_on_narrow_screens(self):
        # On a wrapped row the status control must not stretch to the row's
        # full width or full height — it stays its natural pill size.
        styles = (Path(__file__).resolve().parents[1] / "static" / "core" / "index.css").read_text(encoding="utf-8")
        self.assertNotIn(".saved-estimate__status-form { flex:1 1 180px; }", styles)
        self.assertIn(".saved-estimate__status-form { flex:0 0 auto; align-self:flex-start;", styles)

    def test_user_can_change_own_estimate_status(self):
        estimate = TenderEstimate.objects.create(owner=self.user, tender_number="123", name="Тест")
        updated_at = estimate.updated_at
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("tender_estimate_status", args=[estimate.pk]),
            {"status": "won"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "won")
        self.assertTrue(response.json()["requires_result"])
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, "won")
        self.assertEqual(estimate.updated_at, updated_at)

    def test_result_note_is_shown_for_finished_tender_and_saved(self):
        estimate = TenderEstimate.objects.create(
            owner=self.user,
            tender_number="123",
            name="Тест",
            status=TenderEstimate.LOST,
            result_notes="Победитель снизился на 30%.",
        )
        self.client.force_login(self.user)

        opened = self.client.get(reverse("tender_estimate", args=[estimate.pk]))

        self.assertContains(opened, 'data-tender-result')
        self.assertContains(opened, "Победитель снизился на 30%.")
        self.assertNotContains(opened, 'data-tender-result hidden')

        response = self.client.post(
            reverse("tender_estimate", args=[estimate.pk]),
            {
                "tender_number": "123",
                "name": "Тест",
                "reduction_percent": "20",
                "russia_delivery": "0",
                "result_notes": "Проиграли: победитель снизился на 30%.",
                "lines_json": json.dumps(self.payload),
            },
        )

        self.assertEqual(response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.result_notes, "Проиграли: победитель снизился на 30%.")

    def test_user_cannot_change_another_users_estimate_status(self):
        estimate = TenderEstimate.objects.create(owner=self.other, tender_number="777", name="Чужой")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("tender_estimate_status", args=[estimate.pk]),
            {"status": "lost"},
        )

        self.assertEqual(response.status_code, 404)

    def test_user_cannot_see_another_users_estimate(self):
        estimate = TenderEstimate.objects.create(owner=self.other, tender_number="777", name="Чужой")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("tender_estimate", args=[estimate.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("tender_home")), "Чужой")

    def test_admin_sees_all_and_can_change_owner(self):
        admin = get_user_model().objects.create_superuser(username="admin", password="password")
        estimate = TenderEstimate.objects.create(owner=self.other, tender_number="777", name="Просчёт")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("tender_home")), "Просчёт")
        response = self.client.post(reverse("tender_estimate", args=[estimate.pk]), {"tender_number": "777", "name": "Просчёт", "owner_id": self.user.pk, "reduction_percent": "30", "russia_delivery": "0", "lines_json": json.dumps(self.payload)})
        self.assertEqual(response.status_code, 302)
        estimate.refresh_from_db()
        self.assertEqual(estimate.owner, self.user)

    def test_vat_rate_comes_from_admin_setting(self):
        TenderSettings.objects.create(pk=1, vat_rate="7")
        self.client.force_login(self.user)
        self.client.post(reverse("tender_home"), {"tender_number": "123", "name": "Тест", "reduction_percent": "30", "russia_delivery": "0", "lines_json": json.dumps(self.payload)})
        self.assertEqual(TenderEstimate.objects.get().vat_rate_snapshot, Decimal("7.00"))

    def test_empty_optional_costs_link_and_comment_are_allowed(self):
        line = {**self.payload[0], "application_unit": "", "logistics_unit": "", "product_url": "", "comment": ""}
        self.client.force_login(self.user)
        response = self.client.post(reverse("tender_home"), {"tender_number": "123", "name": "Только материал", "reduction_percent": "30", "russia_delivery": "", "lines_json": json.dumps([line])})
        self.assertEqual(response.status_code, 302)
        saved = TenderEstimate.objects.get().lines.get()
        self.assertEqual(saved.application_unit, Decimal("0.00"))
        self.assertEqual(saved.logistics_unit, Decimal("0.00"))

    def test_incomplete_lines_are_saved_as_draft_and_reopened(self):
        invalid = {**self.payload[0], "material_unit": "", "application_unit": "", "logistics_unit": ""}
        self.client.force_login(self.user)
        response = self.client.post(reverse("tender_home"), {"tender_number": "ABC-999", "name": "Не терять", "reduction_percent": "30", "russia_delivery": "", "lines_json": json.dumps([invalid])})
        self.assertEqual(response.status_code, 302)
        estimate = TenderEstimate.objects.get()
        self.assertTrue(estimate.summary_snapshot["is_incomplete"])
        self.assertEqual(estimate.lines.get().name, "Ручка")
        reopened = self.client.get(reverse("tender_estimate", args=[estimate.pk]))
        self.assertContains(reopened, "Ручка")
        self.assertContains(reopened, 'class="saved-estimate__status-form is-draft"')
        self.assertNotContains(reopened, 'class="saved-estimate__draft"')

    def test_partially_filled_line_values_are_preserved_in_draft(self):
        partial = {"name": "", "quantity": "50", "nmck_unit": "", "material_unit": "12.50", "application_unit": "", "logistics_unit": "", "product_url": "", "comment": "Уточнить товар", "requirements": {}}
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_home"), {"tender_number": "DRAFT-1", "name": "Черновик", "reduction_percent": "30", "russia_delivery": "", "lines_json": json.dumps([partial])})

        self.assertEqual(response.status_code, 302)
        line = TenderEstimate.objects.get().lines.get()
        self.assertEqual(line.name, "")
        self.assertEqual(line.quantity, Decimal("50"))
        self.assertEqual(line.material_unit, Decimal("12.50"))
        self.assertEqual(line.comment, "Уточнить товар")

    def test_excel_preview_returns_sheets_and_rows(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "НМЦК"
        sheet.append(["Наименование", "Количество", "Цена"])
        sheet.append(["Ручка", 100, 25.5])
        content = BytesIO()
        workbook.save(content)
        content.seek(0)
        content.name = "nmck.xlsx"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_import_preview"), {"file": content}, format="multipart")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["sheet"], "НМЦК")
        self.assertEqual(payload["rows"][1], ["Ручка", "100", "25.5"])

    def test_excel_preview_requires_login(self):
        response = self.client.post(reverse("tender_import_preview"))
        self.assertEqual(response.status_code, 302)

    def test_docx_tables_are_extracted_without_saving_file(self):
        document = Document()
        table = document.add_table(rows=2, cols=3)
        table.rows[0].cells[0].text = "Наименование"
        table.rows[0].cells[1].text = "Количество"
        table.rows[0].cells[2].text = "Цена"
        table.rows[1].cells[0].text = "Блокнот"
        table.rows[1].cells[1].text = "20"
        table.rows[1].cells[2].text = "150"
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "nmck.docx"

        text, truncated = extract_tender_source(content)

        self.assertIn("Блокнот | 20 | 150", text)
        self.assertFalse(truncated)

    def test_nested_docx_tables_include_technical_values(self):
        document = Document()
        outer = document.add_table(rows=1, cols=3)
        outer.cell(0, 0).text = "Папка «Благодарность»"
        nested = outer.cell(0, 1).add_table(rows=2, cols=2)
        nested.cell(0, 0).text = "Материал"
        nested.cell(0, 1).text = "Дизайнерский картон"
        nested.cell(1, 0).text = "Плотность"
        nested.cell(1, 1).text = "не менее 290 г/м²"
        outer.cell(0, 2).text = "1000"
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "tz.docx"

        text, truncated = extract_tender_source(content)

        self.assertIn("Дизайнерский картон", text)
        self.assertIn("не менее 290 г/м²", text)
        self.assertFalse(truncated)

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_docx_embedded_excel_is_detected_and_parsed_locally(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["№", "Наименование", "Ед.", "Кол-во", "Средняя цена", "НМЦК"])
        sheet.append([1, "Календарь настенный", "шт", 100, 440, 44000])
        sheet.append([2, "Календарь настольный", "шт", 300, 401.67, 120501])
        embedded = BytesIO()
        workbook.save(embedded)

        document = Document()
        document.add_heading("Обоснование НМЦК")
        base = BytesIO()
        document.save(base)
        content = BytesIO()
        with zipfile.ZipFile(BytesIO(base.getvalue())) as source, zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("word/embeddings/Microsoft_Excel_Worksheet.xlsx", embedded.getvalue())
        content.seek(0)
        content.name = "Обоснование НМЦК.docx"

        inspection = inspect_tender_document(content)
        content.seek(0)
        result = recognize_tender_items(content)

        self.assertEqual(inspection["processing_mode"], "embedded")
        self.assertEqual(inspection["components"]["embedded_spreadsheets"], 1)
        self.assertTrue(result["local_parse"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(sum(Decimal(value["nmck_total"]) for value in result["items"]), Decimal("164501.00"))

    def test_model_json_accepts_explanatory_wrapper_and_control_characters(self):
        result = _json_from_model('Ответ модели:\n```json\n{"items":[{"name":"строка\tс табуляцией"}]}\n```')

        self.assertEqual(result["items"][0]["name"], "строка\tс табуляцией")

    @patch("tenders.views.detect_tender_document_type", return_value="unknown")
    @patch("tenders.views.analyze_tender_requirements")
    def test_second_smart_upload_can_be_forced_to_technical_document(self, analyze, detect):
        analyze.return_value = {"document_summary": "ТЗ", "global_requirements": [], "items": [], "warnings": [], "scan_ocr": False, "usage": {}}
        content = BytesIO(b"placeholder")
        content.name = "requirements.docx"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_document_preview"), {
            "file": content,
            "lines_json": json.dumps(self.payload),
            "document_role": "technical",
        }, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document_type"], "technical")
        self.assertIn("technical", response.json())

    @patch("tenders.views.recognize_tender_items")
    @patch("tenders.views.detect_tender_document_type", return_value="technical")
    def test_explicit_nmck_action_rejects_obvious_technical_document(self, detect, recognize):
        content = BytesIO(b"placeholder")
        content.name = "requirements.docx"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_document_preview"), {
            "file": content,
            "lines_json": "[]",
            "document_role": "nmck",
        }, format="multipart")

        self.assertEqual(response.status_code, 422)
        self.assertIn("Загрузить ООЗ / ТЗ", response.json()["error"])
        recognize.assert_not_called()

    @patch("tenders.views.analyze_tender_requirements")
    @patch("tenders.views.detect_tender_document_type", return_value="nmck")
    def test_explicit_technical_action_rejects_obvious_nmck_document(self, detect, analyze):
        content = BytesIO(b"placeholder")
        content.name = "nmck.xlsx"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_document_preview"), {
            "file": content,
            "lines_json": "[]",
            "document_role": "technical",
        }, format="multipart")

        self.assertEqual(response.status_code, 422)
        self.assertIn("Загрузить НМЦК", response.json()["error"])
        analyze.assert_not_called()

    @patch("tenders.services._ai_gateway_json")
    def test_technical_analysis_excludes_delivery_terms_but_keeps_shared_specs(self, gateway):
        gateway.return_value = ({
            "document_summary": "Футболки",
            "global_requirements": [
                {"label": "Срок поставки", "value": "10 дней", "source": "стр. 1"},
                {"label": "Общий цвет", "value": "чёрный", "source": "стр. 1"},
            ],
            "items": [{
                "line_index": 0,
                "source_name": "Футболка",
                "quantity": 20,
                "requirements": [{"label": "Материал", "value": "хлопок"}],
                "missing": [],
                "questions": [],
                "confidence": .9,
            }],
            "warnings": [],
        }, {})
        document = Document()
        document.add_paragraph("Футболка, хлопок, чёрный цвет. Поставка за 10 дней.")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "ТЗ.docx"

        result = analyze_tender_requirements(content, [{"name": "Футболка", "quantity": "20"}])

        self.assertEqual(result["global_requirements"], [{"label": "Общий цвет", "value": "чёрный", "source": "стр. 1"}])
        self.assertIn("Не извлекай условия поставки", gateway.call_args.args[0])
        self.assertIn("Все числовые технические характеристики значимы", gateway.call_args.args[0])

    @patch("tenders.services._ai_gateway_json")
    def test_requirements_match_is_recovered_when_model_omits_confidence(self, gateway):
        gateway.return_value = ({"document_summary": "Папки", "global_requirements": [], "items": [{"line_index": None, "source_name": "Папка Благодарность 18.12.19.190", "quantity": 1000, "requirements": [{"label": "Материал", "value": "картон"}], "missing": [], "questions": []}], "warnings": []}, {})
        document = Document()
        document.add_paragraph("Папка Благодарность, 1000 штук, материал картон")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "tz.docx"

        result = analyze_tender_requirements(content, [{"name": "Папка «Благодарность»", "quantity": "1000"}])

        self.assertEqual(result["items"][0]["line_index"], 0)
        self.assertEqual(result["items"][0]["match_status"], "matched")
        self.assertGreater(result["items"][0]["confidence"], .8)

    @patch("tenders.services._ai_gateway_json")
    def test_matched_technical_quantity_comes_from_nmck_line(self, gateway):
        gateway.return_value = ({"document_summary": "Футболка", "global_requirements": [], "items": [{"line_index": 0, "source_name": "Футболка", "quantity": 1, "requirements": [{"label": "Материал", "value": "хлопок"}], "missing": [], "questions": [], "confidence": .9}], "warnings": []}, {})
        document = Document()
        document.add_paragraph("Футболка, количество 20, материал хлопок")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "ТЗ.docx"

        result = analyze_tender_requirements(content, [{"name": "Футболка", "quantity": "20"}])

        self.assertEqual(result["items"][0]["quantity"], "20")

    @patch("tenders.views.recognize_tender_items")
    def test_ai_preview_returns_editable_items(self, recognize):
        recognize.return_value = {"items": [{"name": "Блокнот", "quantity": "20", "nmck_unit": "150.00", "nmck_total": "3000.00", "total_from_source": True, "total_matches": True, "confidence": 0.9}], "warnings": [], "usage": {}}
        content = BytesIO(b"placeholder")
        content.name = "nmck.pdf"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_ai_import_preview"), {"file": content}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["name"], "Блокнот")
        recognize.assert_called_once()

    def test_ai_preview_rejects_unsupported_file(self):
        content = BytesIO(b"text")
        content.name = "nmck.txt"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_ai_import_preview"), {"file": content}, format="multipart")

        self.assertEqual(response.status_code, 400)

    @patch("tenders.views.analyze_tender_requirements")
    def test_legacy_doc_is_accepted_for_requirements(self, analyze):
        analyze.return_value = {"document_summary": "ТЗ", "global_requirements": [], "items": [], "warnings": [], "scan_ocr": False, "usage": {}}
        content = BytesIO(b"legacy-doc-placeholder")
        content.name = "tz.doc"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_requirements_preview"), {"file": content, "lines_json": "[]"}, format="multipart")

        self.assertEqual(response.status_code, 200)
        analyze.assert_called_once()

    def test_scanned_pdf_is_detected_for_ocr_fallback(self):
        content = BytesIO(b"%PDF-1.4")
        content.name = "scan.pdf"
        with patch("tenders.services.PdfReader") as reader:
            reader.return_value.pages = [type("Page", (), {"extract_text": lambda self: ""})()]
            text, truncated = extract_tender_source(content)
        self.assertEqual(text, "")
        self.assertFalse(truncated)

    @patch("tenders.views.inspect_tender_document")
    def test_document_preflight_reports_visual_processing_mode(self, inspect):
        inspect.return_value = {"document_type": "unknown", "processing_mode": "visual", "truncated": False, "quality": {"usable": False}}
        content = BytesIO(b"%PDF-1.4")
        content.name = "scan.pdf"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_document_inspect"), {"file": content}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processing_mode"], "visual")

    def test_project_contract_is_not_classified_as_technical_document(self):
        document = Document()
        document.add_heading("Проект контракта")
        document.add_paragraph("Приложение содержит описание объекта закупки и требования к товару.")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "Проект контракта.docx"

        self.assertEqual(detect_tender_document_type(content), "unknown")

    def test_technical_filename_and_content_are_classified_together(self):
        document = Document()
        document.add_heading("Описание объекта закупки")
        document.add_paragraph("Технические характеристики футболки: хлопок, плотность 180 г/м².")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "Описание объекта закупки.docx"

        self.assertEqual(detect_tender_document_type(content), "technical")

    def test_short_tz_filename_is_a_valid_technical_signal(self):
        document = Document()
        document.add_paragraph("Футболка: материал хлопок, плотность 180 г/м², нанесение DTF.")
        content = BytesIO()
        document.save(content)
        content.seek(0)
        content.name = "ТЗ_мерч_2_позиции.docx"

        self.assertEqual(detect_tender_document_type(content), "technical")

    def test_broken_nonempty_pdf_text_layer_requires_visual_fallback(self):
        quality = _source_text_quality("/i1041 /i1086 /i1083 /i1086 /i0003 /i1090 /i1077 /i1082 /i1089 /i1090")

        self.assertFalse(quality["usable"])

    def test_normal_russian_pdf_text_layer_stays_on_fast_path(self):
        quality = _source_text_quality("Описание объекта закупки. Футболка хлопок, размер XL, тираж 100 штук.")

        self.assertTrue(quality["usable"])

    def test_document_numbers_accept_comma_dot_and_group_separators(self):
        self.assertEqual(_parse_document_decimal("3 870,67"), Decimal("3870.67"))
        self.assertEqual(_parse_document_decimal("3,870.67 руб."), Decimal("3870.67"))
        self.assertEqual(_parse_document_decimal("2.129,21"), Decimal("2129.21"))

    @patch("tenders.services._pdf_page_count", return_value=1)
    @patch("tenders.services.extract_tender_source", return_value=("/i1041 /i1086 /i1083 /i1086 /i0003", False))
    @patch("tenders.services._ai_gateway_json")
    def test_broken_pdf_text_layer_uses_visual_recognition(self, gateway, _extract, _page_count):
        gateway.return_value = ({"items": [{"name": "Футболка", "quantity": "10", "nmck_unit": "100", "nmck_total": None, "confidence": .9}], "warnings": []}, {})
        content = BytesIO(b"%PDF-1.4")
        content.name = "nmck.pdf"

        result = recognize_tender_items(content)

        self.assertTrue(result["scan_ocr"])
        self.assertEqual(result["processing_mode"], "visual")
        self.assertEqual(result["items"][0]["nmck_total"], "1000.00")
        self.assertTrue(gateway.call_args.kwargs["scan_ocr"])

    @patch("tenders.services._pdf_page_count", return_value=25)
    @patch("tenders.services._scan_pdf_images", return_value=["encoded-page"])
    @patch("tenders.services._ai_gateway_json")
    def test_long_scanned_pdf_is_processed_in_bounded_page_batches(self, gateway, scan_images, _page_count):
        from .services import _visual_gateway_responses

        gateway.return_value = ({"items": [], "warnings": []}, {"prompt_tokens": 1, "completion_tokens": 1})
        content = BytesIO(b"%PDF-1.4")
        content.name = "long-scan.pdf"

        responses = _visual_gateway_responses("Распознай документ", content, max_tokens=1000)

        self.assertEqual(len(responses), 3)
        self.assertEqual(scan_images.call_count, 3)
        self.assertEqual(scan_images.call_args_list[0].kwargs, {"start_page": 0, "page_limit": 12})
        self.assertEqual(scan_images.call_args_list[-1].kwargs, {"start_page": 24, "page_limit": 12})
        self.assertEqual(gateway.call_count, 3)
        self.assertEqual(gateway.call_args.kwargs["image_data_urls"], ["data:image/jpeg;base64,encoded-page"])

    @patch("tenders.views.analyze_tender_requirements")
    def test_technical_document_preview_uses_current_lines(self, analyze):
        analyze.return_value = {"document_summary": "Печать буклетов", "global_requirements": [], "items": [{"line_index": 0, "source_name": "Буклет", "requirements": [], "missing": [], "questions": [], "confidence": .9}], "warnings": [], "scan_ocr": False, "usage": {}}
        content = BytesIO(b"placeholder")
        content.name = "tz.pdf"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_requirements_preview"), {"file": content, "lines_json": json.dumps(self.payload)}, format="multipart")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["line_index"], 0)
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[1][0]["name"], "Ручка")

    def test_a5_imposition_uses_parent_sheet_and_rotation(self):
        self.assertEqual(calculate_sheet_imposition(148, 210, 210, 297, 0), {"ups": 2, "rotation": True})
        self.assertEqual(calculate_sheet_imposition(148, 210, 320, 450, 0)["ups"], 4)
        self.assertEqual(calculate_sheet_imposition(148, 210, 320, 450, 3)["ups"], 4)

    def test_unknown_exact_sra3_paper_stays_a_priced_question(self):
        a4 = PriceItem.objects.create(category="paper", name="Обычная A4 80", unit_price=Decimal("0.8"))
        a3 = PriceItem.objects.create(category="paper", name="Maestro Special A3 80", unit_price=Decimal("1.7"))
        sra3 = PriceItem.objects.create(category="paper", name="Немел SRA3 120г", unit_price=Decimal("3"))
        candidates = _paper_candidates({"finished_width_mm": 148, "finished_height_mm": 210, "units_per_product": 60, "material_query": "офсетная бумага", "grammage_gsm": 80, "bleed_mm": 0}, 300, [a4, a3, sra3])

        exact_sra3 = next(value for value in candidates if value["format"] == "SRA3" and value["grammage_gsm"] == 80)
        self.assertTrue(exact_sra3["price_missing"])
        self.assertEqual(exact_sra3["ups"], 4)
        self.assertEqual(exact_sra3["sheets"], 4635)

    @patch("tenders.views._submit_assistant_job")
    def test_production_route_preview_starts_background_job(self, submit):
        self.client.force_login(self.user)

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])

        response = self.client.post(reverse("tender_production_route_preview"), {"line_json": json.dumps({"name": "Блокнот А5", "quantity": 300, "requirements": {}})})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "processing")
        session = ProductionTrainingSession.objects.get(pk=response.json()["session_id"])
        submit.assert_called_once()
        self.assertEqual(submit.call_args[0][0], session.pk)

    @patch("tenders.views._submit_assistant_job")
    def test_production_route_preview_reuses_a_running_session(self, submit):
        self.client.force_login(self.user)
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        line = {"line_json": json.dumps({"name": "Блокнот А5", "quantity": 300, "requirements": {}})}

        first = self.client.post(reverse("tender_production_route_preview"), line)
        second = self.client.post(reverse("tender_production_route_preview"), line)

        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["session_id"], second.json()["session_id"])
        self.assertEqual(ProductionTrainingSession.objects.count(), 1)
        submit.assert_called_once()

    def test_production_route_job_returns_finished_hypothesis(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user,
            position_name="Блокнот А5",
            current_hypothesis={"stage": "training_dialogue", "route": {"name": "Готово"}},
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("tender_production_route_status", args=[session.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"]["name"], "Готово")
        self.assertEqual(response.json()["session_id"], session.pk)

    def test_manager_cannot_start_ai_calculation(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_production_route_preview"), {"line_json": json.dumps({"name": "Блокнот А5", "quantity": 300, "requirements": {}})})

        self.assertEqual(response.status_code, 403)

    def test_page_exposes_ai_only_for_admin(self):
        self.client.force_login(self.user)
        manager_page = self.client.get(reverse("tender_home"))
        self.assertContains(manager_page, "aiEnabled=false")

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        admin_page = self.client.get(reverse("tender_home"))
        self.assertContains(admin_page, "aiEnabled=true")

    @patch("tenders.views.build_training_hypothesis")
    def test_admin_feedback_creates_structured_turn_and_updates_session(self, build):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user,
            position_name="Папка",
            requirements={"requirements": []},
            current_hypothesis={"route": {"name": "Старый маршрут"}},
        )
        build.return_value = {
            "stage": "training_dialogue",
            "product_type": "binding_special",
            "route": {"name": "Подрядчик под ключ", "steps": ["Изготовление под ключ"]},
            "costs": [{"category": "application", "name": "Изготовление", "amount_total": "10000.00"}],
            "totals": {"application_unit": "100.00", "cost_total": "10000.00"},
            "understood_changes": ["Выбран подрядчик под ключ"],
        }
        self.client.force_login(self.user)
        payload = {"session_id": session.pk, "line": {"name": "Папка", "quantity": 100, "requirements": {}}, "feedback": "Считать у подрядчика под ключ"}

        with patch.object(tender_views._ASSISTANT_EXECUTOR, "submit", new=lambda fn, *args: fn(*args)):
            response = self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps(payload)})

        self.assertEqual(response.status_code, 202)
        session.refresh_from_db()
        self.assertEqual(session.current_hypothesis["route"]["name"], "Подрядчик под ключ")
        turn = ProductionTrainingTurn.objects.get(session=session)
        self.assertEqual(turn.feedback, "Считать у подрядчика под ключ")
        self.assertEqual(turn.understood_changes, ["Выбран подрядчик под ключ"])

    @patch("tenders.views.build_training_hypothesis")
    def test_revise_forwards_the_block_scope_to_the_builder(self, build):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Поло", requirements={"requirements": []},
            current_hypothesis={"stage": "training_dialogue", "route": {"name": "x"}},
        )
        build.return_value = {"stage": "training_dialogue", "route": {"name": "x"}, "understood_changes": []}
        self.client.force_login(self.user)
        payload = {"session_id": session.pk, "line": {"name": "Поло", "quantity": 50}, "feedback": "исключи детские", "scope": "catalog"}

        with patch.object(tender_views._ASSISTANT_EXECUTOR, "submit", new=lambda fn, *args: fn(*args)):
            self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps(payload)})

        self.assertEqual(build.call_args.kwargs["recompute"], "catalog")

    @patch("tenders.views.build_training_hypothesis")
    def test_revise_defaults_to_a_full_rebuild_when_no_scope_is_given(self, build):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Поло", requirements={"requirements": []},
            current_hypothesis={"stage": "training_dialogue", "route": {"name": "x"}},
        )
        build.return_value = {"stage": "training_dialogue", "route": {"name": "x"}, "understood_changes": []}
        self.client.force_login(self.user)
        payload = {"session_id": session.pk, "line": {"name": "Поло", "quantity": 50}, "feedback": "это своё производство", "scope": "route"}

        with patch.object(tender_views._ASSISTANT_EXECUTOR, "submit", new=lambda fn, *args: fn(*args)):
            self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps(payload)})

        self.assertEqual(build.call_args.kwargs["recompute"], "all")

    def test_finalize_accepts_a_run_that_has_nothing_to_learn(self):
        # "Принять и обучить" doubles as plain "accept" — a run where a
        # product was picked but no search rule was written still closes the
        # session cleanly instead of erroring "нет правил для сохранения".
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Поло", requirements={"requirements": []},
            current_hypothesis={
                "stage": "training_dialogue",
                "route": {"steps": ["Закупка готового изделия", "Нанесение"]},
            },
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_confirm_production_type"), {
            "payload": json.dumps({"session_id": session.pk, "line": {"name": "Поло", "quantity": 50}})
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rules_saved"], 0)
        session.refresh_from_db()
        self.assertTrue(session.is_confirmed)

    def test_finalize_learns_the_unchecked_tz_rows(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Жилет", requirements={"requirements": []},
            current_hypothesis={
                "stage": "training_dialogue",
                "route": {"steps": ["Закупка готового изделия", "Нанесение"]},
                "requirement_selection": [
                    {"label": "Цвет", "value": "синий", "selected": True},
                    {"label": "Маркировка", "value": "Честный Знак", "selected": False},
                    {"label": "Швы", "value": "оверлок", "selected": False},
                ],
            },
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_confirm_production_type"), {
            "payload": json.dumps({"session_id": session.pk, "line": {"name": "Жилет", "quantity": 50}})
        })

        self.assertEqual(response.json()["requirement_skips_saved"], 2)
        self.assertEqual(
            set(RequirementSkipRule.objects.filter(is_active=True).values_list("label_normalized", flat=True)),
            {"маркировка", "швы"},
        )

        # The drop endpoint puts a label back into the подбор.
        response = self.client.post(reverse("tender_drop_requirement_skip_rule"), {
            "payload": json.dumps({"label": "Швы"})
        })
        self.assertTrue(response.json()["dropped"])
        self.assertFalse(RequirementSkipRule.objects.get(label_normalized="швы").is_active)

    @patch("tenders.views.build_training_hypothesis")
    def test_revise_forwards_requirements_scope_and_needs_no_feedback(self, build):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Жилет", requirements={"requirements": []},
            current_hypothesis={"stage": "training_dialogue", "route": {"name": "x"}},
        )
        build.return_value = {"stage": "training_dialogue", "route": {"name": "x"}, "understood_changes": []}
        self.client.force_login(self.user)
        payload = {"session_id": session.pk, "line": {"name": "Жилет", "quantity": 50}, "scope": "requirements"}

        with patch.object(tender_views._ASSISTANT_EXECUTOR, "submit", new=lambda fn, *args: fn(*args)):
            response = self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps(payload)})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(build.call_args.kwargs["recompute"], "catalog")

    def test_unverified_tz_price_is_blocked_from_learning(self):
        production_type = ProductionType.objects.get(code="binding_special")
        raw = {
            "product_type": production_type.code,
            "route": {"reason": "Под ключ", "processes": [{"name": "Универсальная типография"}]},
            "costs": [{
                "category": "logistics", "name": "Логистика", "amount_total": "3000",
                "source": "дано в ТЗ", "source_type": "manager",
                "recipe": {"method": "fixed", "inputs": {"fixed_amount": "3000"}},
            }],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 1000, "requirements": {"requirements": []}}, [production_type], [])

        self.assertEqual(result["costs"][0]["source"], "Источник цены не подтверждён")
        self.assertTrue(result["learning_warnings"])

    def test_only_admin_can_confirm_training_example(self):
        production_type = ProductionType.objects.get(code="digital_sheet")
        payload = {"line": {"name": "Открытка", "requirements": {"requirements": []}}, "production_type": production_type.code, "features": ["тираж 100"], "routes": [{"name": "Под ключ", "processes": [{"role": "production", "name": "Цифровая листовая печать"}]}]}
        self.client.force_login(self.user)
        denied = self.client.post(reverse("tender_confirm_production_type"), {"payload": json.dumps(payload)})
        self.assertEqual(denied.status_code, 403)

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        saved = self.client.post(reverse("tender_confirm_production_type"), {"payload": json.dumps(payload)})
        self.assertEqual(saved.status_code, 200)
        example = ProductionTrainingExample.objects.get(position_name="Открытка", production_type=production_type)
        self.assertEqual(example.routes[0]["processes"][0]["name"], "Цифровая листовая печать")

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_multiline_nmck_xlsx_is_parsed_locally_with_final_totals(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Обоснование начальной (максимальной) цены контракта"])
        sheet.append(["№", "Наименование услуги", "", "Количество закупаемых позиций", "", "", "", "Цена Исполнителя", "", "", "", "", "", "Средняя арифметическая цена", "", "", "Начальная (максимальная) цена"])
        sheet.append([None, None, None, None, None, None, None, "Исполнитель 1"])
        sheet.append([None] * 13 + ["Средняя цена за единицу"] + [None, None, "НМЦК позиции"])
        sheet.append([1, "Услуги по изготовлению и поставке подарочной продукции (футболка подарочная № 1)", None, 20, None, None, None, 4900, 3950, 4800, None, None, None, 3870.67, None, None, 77413.40])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "Обоснование НМЦК.xlsx"

        self.assertEqual(detect_tender_document_type(stream), "nmck")
        result = recognize_tender_items(stream)

        self.assertTrue(result["local_parse"])
        self.assertEqual(result["items"][0]["name"], "футболка подарочная № 1")
        self.assertEqual(result["items"][0]["nmck_unit"], "3870.67")
        self.assertEqual(result["items"][0]["nmck_total"], "77413.40")

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_nmck_xlsx_accepts_volume_and_abbreviated_quantity_headers(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "МинЦена"
        sheet.cell(3, 4, "Обоснование начальной (максимальной) цены Контракта.")
        sheet.cell(8, 1, "Начальная (максимальная) цена контракта")
        sheet.cell(8, 13, "Минимальная цена выбранная Заказчиком за единицу товара *")
        sheet.cell(8, 14, "Сумма начальной (максимальной) цены контракта")
        sheet.cell(9, 2, "Наименование товара, работ, услуг")
        sheet.cell(9, 3, "Объем")
        sheet.cell(10, 3, "Ед.изм.")
        sheet.cell(10, 4, "Кол-во")
        sheet.cell(11, 2, "Карта клиента")
        sheet.cell(11, 4, 2000)
        sheet.cell(11, 8, 24.45)
        sheet.cell(11, 13, 18.90)
        sheet.cell(11, 14, 37800)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "Обоснование НМЦК.xlsx"

        self.assertEqual(detect_tender_document_type(stream), "nmck")
        result = recognize_tender_items(stream)

        self.assertTrue(result["local_parse"])
        self.assertEqual(result["items"][0]["quantity"], "2000")
        self.assertEqual(result["items"][0]["nmck_unit"], "18.90")
        self.assertEqual(result["items"][0]["nmck_total"], "37800.00")

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_nmck_xlsx_accepts_cost_wording_without_price_word(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Наименование", "Количество", "Среднеарифметическая стоимость за единицу", "Среднеарифметическая стоимость за все кол-во товара"])
        sheet.append(["Папка картонная", 889, 1458.5, 1296606.5])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "НМЦК.xlsx"

        result = recognize_tender_items(stream)

        self.assertTrue(result["local_parse"])
        self.assertEqual(result["items"][0]["quantity"], "889")
        self.assertEqual(result["items"][0]["nmck_unit"], "1458.50")
        self.assertEqual(result["items"][0]["nmck_total"], "1296606.50")

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_nmck_table_accepts_characteristics_as_name_and_yo_in_volume(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Основные характеристики объекта закупки", "Объём", "Среднее ценовое значение", "Начальная (максимальная) цена контракта"])
        sheet.append(["Чехол на чемодан с логотипом", 200, 1713, 342600])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "Обоснование НМЦК.xlsx"

        result = recognize_tender_items(stream)

        self.assertTrue(result["local_parse"])
        self.assertEqual(result["items"][0]["name"], "Чехол на чемодан с логотипом")
        self.assertEqual(result["items"][0]["quantity"], "200")
        self.assertEqual(result["items"][0]["nmck_total"], "342600.00")

    @patch("tenders.services.extract_tender_source", return_value=("", False))
    def test_structured_nmck_xlsx_is_detected_when_text_classification_fails(self, _extract):
        workbook = Workbook()
        sheet = workbook.active
        sheet.cell(8, 13, "Минимальная цена выбранная Заказчиком за единицу товара")
        sheet.cell(8, 14, "Сумма начальной (максимальной) цены контракта")
        sheet.cell(9, 2, "Наименование товара, работ, услуг")
        sheet.cell(9, 4, "Кол-во")
        sheet.cell(11, 2, "Карта клиента")
        sheet.cell(11, 4, 2000)
        sheet.cell(11, 13, 18.90)
        sheet.cell(11, 14, 37800)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "Обоснование НМЦК.xlsx"

        self.assertEqual(detect_tender_document_type(stream), "nmck")

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": ""})
    def test_smart_document_endpoint_imports_structured_nmck_xlsx(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.cell(3, 4, "Обоснование начальной (максимальной) цены Контракта")
        sheet.cell(8, 13, "Минимальная цена выбранная Заказчиком за единицу товара")
        sheet.cell(8, 14, "Сумма начальной (максимальной) цены контракта")
        sheet.cell(9, 2, "Наименование товара, работ, услуг")
        sheet.cell(9, 4, "Кол-во")
        sheet.cell(11, 2, "Карта клиента")
        sheet.cell(11, 4, 2000)
        sheet.cell(11, 13, 18.90)
        sheet.cell(11, 14, 37800)
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        stream.name = "Обоснование НМЦК.xlsx"
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_document_preview"), {"file": stream, "lines_json": "[]"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document_type"], "nmck")
        self.assertEqual(response.json()["nmck"]["items"][0]["nmck_total"], "37800.00")

    def test_repeated_document_prefix_is_removed_from_item_names(self):
        items = [
            {"name": "полиграфической продукции: Карта «Саранск-Мордовия»"},
            {"name": "полиграфической продукции: Лифлет «Мордовия заповедная»"},
            {"name": "полиграфической продукции: Блокнот № 1"},
        ]

        names = _strip_shared_item_boilerplate(items)

        self.assertEqual(names, ["Карта «Саранск-Мордовия»", "Лифлет «Мордовия заповедная»", "Блокнот № 1"])

    @patch.dict("os.environ", {"TIMEWEB_AI_API_KEY": "test-key"})
    @patch("tenders.services._ai_gateway_json")
    def test_smart_excel_uses_one_ai_call_to_normalize_all_names(self, gateway):
        gateway.return_value = ({"items": [
            {"index": 0, "name": "Карта «Саранск-Мордовия»"},
            {"index": 1, "name": "Лифлет «Мордовия заповедная»"},
        ]}, {"prompt_tokens": 80, "completion_tokens": 30})
        items = [
            {"name": "полиграфической продукции: Карта «Саранск-Мордовия»"},
            {"name": "полиграфической продукции: Лифлет «Мордовия заповедная»"},
        ]

        normalized, usage, warning = _shorten_structured_item_names(items)

        self.assertEqual([value["name"] for value in normalized], ["Карта «Саранск-Мордовия»", "Лифлет «Мордовия заповедная»"])
        self.assertEqual(usage["prompt_tokens"], 80)
        self.assertIsNone(warning)
        gateway.assert_called_once()

    def test_large_technical_table_is_split_only_between_product_rows(self):
        header = "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ\n№ | Наименование | Характеристики"
        rows = [f"{index} | Товар {index} | " + ("характеристика " * 70) for index in range(1, 13)]

        chunks = _technical_source_chunks("\n".join([header, *rows]), max_chars=2400)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all("ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ" in value for value in chunks))
        combined = "\n".join(chunks)
        self.assertTrue(all(f"{index} | Товар {index} |" in combined for index in range(1, 13)))

    def test_large_technical_table_keeps_repeated_characteristics_of_product_together(self):
        header = "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ\n№ | Наименование | Параметр | Значение"
        rows = [
            *[f"1 | Шнурок | Параметр {index} | " + ("значение " * 12) for index in range(5)],
            *[f"2 | 3D-стикер | Параметр {index} | " + ("значение " * 12) for index in range(5)],
        ]

        chunks = _technical_source_chunks("\n".join([header, *rows]), max_chars=700)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(sum("1 | Шнурок |" in value for value in chunks), 1)
        self.assertEqual(sum("2 | 3D-стикер |" in value for value in chunks), 1)
        self.assertTrue(any(value.count("1 | Шнурок |") == 5 for value in chunks))
        self.assertTrue(any(value.count("2 | 3D-стикер |") == 5 for value in chunks))

    @patch("tenders.services._technical_source_chunks", return_value=["первая часть", "вторая часть"])
    @patch("tenders.services.extract_tender_source", return_value=("длинный документ", False))
    @patch("tenders.services._ai_gateway_json")
    def test_partial_technical_answers_for_one_product_are_merged(self, gateway, extract, chunks):
        usage = {"prompt_tokens": 20, "completion_tokens": 20}
        gateway.side_effect = [
            ({
                "items": [{
                    "line_index": 0, "source_name": "3D стикеры", "quantity": 5000,
                    "requirements": [{"label": "Вид продукции", "value": "3D-стикер", "source": "таблица 1"}],
                    "missing": ["Материал", "Размеры"],
                    "questions": ["Какой материал используется?"], "confidence": .8,
                }],
                "global_requirements": [], "warnings": [], "document_summary": "",
            }, usage),
            ({
                "items": [{
                    "line_index": 0, "source_name": "3D стикеры", "quantity": 5000,
                    "requirements": [
                        {"label": "Материал", "value": "Полимерная смола", "source": "таблица 1"},
                        {"label": "Ширина", "value": "50 мм", "source": "таблица 1"},
                        {"label": "Высота", "value": "50 мм", "source": "таблица 1"},
                    ],
                    "missing": [], "questions": [], "confidence": .9,
                }],
                "global_requirements": [], "warnings": [], "document_summary": "",
            }, usage),
        ]

        result = analyze_tender_requirements(
            type("Upload", (), {"name": "test.docx"})(),
            [{"name": "Изготовление 3D стикеров", "quantity": 5000}],
        )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["line_index"], 0)
        self.assertEqual(len(result["items"][0]["requirements"]), 4)
        self.assertEqual(result["items"][0]["missing"], [])
        self.assertEqual(result["items"][0]["questions"], [])

    @patch("tenders.services.extract_tender_source")
    @patch("tenders.services._ai_gateway_json")
    def test_empty_context_echoes_do_not_occupy_technical_matches(self, gateway, extract):
        extract.return_value = ("Описание объекта закупки", False)
        gateway.return_value = ({
            "items": [
                {"line_index": 0, "source_name": "Первый товар", "quantity": 10, "requirements": [], "missing": [], "questions": [], "confidence": .9},
                {"line_index": 1, "source_name": "Второй товар", "quantity": 20, "requirements": [{"label": "Материал", "value": "Бумага", "source": "таблица 1"}], "missing": [], "questions": [], "confidence": .9},
            ],
            "global_requirements": [], "warnings": [], "document_summary": "",
        }, {"prompt_tokens": 20, "completion_tokens": 20})
        lines = [{"name": "Первый товар", "quantity": 10}, {"name": "Второй товар", "quantity": 20}]

        result = analyze_tender_requirements(type("Upload", (), {"name": "test.docx"})(), lines)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["line_index"], 1)
        self.assertIn("Первый товар", result["warnings"][0])
        self.assertIn("1 строку", result["warnings"][0])

    @patch("tenders.services.extract_tender_source")
    @patch("tenders.services._ai_gateway_json")
    def test_requirements_warn_about_every_nmck_line_missing_from_technical_document(self, gateway, extract):
        extract.return_value = ("Описание объекта закупки", False)
        gateway.return_value = ({
            "items": [{
                "line_index": 1,
                "source_name": "Карта с картонной обложкой",
                "quantity": 5000,
                "requirements": [{"label": "Материал", "value": "Картон", "source": "таблица 1"}],
                "missing": [],
                "questions": [],
                "confidence": .95,
            }],
            "global_requirements": [], "warnings": [], "document_summary": "Карты",
        }, {"prompt_tokens": 20, "completion_tokens": 20})
        lines = [
            {"name": "Карта «Саранск-Мордовия»", "quantity": 2000},
            {"name": "Карта «Саранск-Мордовия» с картонными обложками", "quantity": 5000},
        ]

        result = analyze_tender_requirements(type("Upload", (), {"name": "test.docx"})(), lines)

        self.assertEqual(result["items"][0]["line_index"], 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("ООЗ/ТЗ не покрывает 1 строку НМЦК", result["warnings"][0])
        self.assertIn("Карта «Саранск-Мордовия»", result["warnings"][0])

    def test_local_batch_index_does_not_override_a_better_name_match(self):
        lines = [
            {"name": "Лифлет Мордовия", "quantity": 3000},
            {"name": "Стикерпак", "quantity": 500},
        ]

        index, confidence, _ = _resolve_line_match(0, "Стикерпак", 500, lines, set())

        self.assertEqual(index, 1)
        self.assertGreaterEqual(confidence, .66)

    def test_training_cost_keeps_detailed_calculation_trace(self):
        production_type = ProductionType.objects.create(code="trace-test", name="Тест")
        raw = {
            "product_type": production_type.code,
            "route": {"name": "Под ключ", "steps": ["Изготовление"]},
            "costs": [{
                "category": "material", "name": "Majestic SRA3", "amount_total": 9500,
                "source": "Калькулятор PSODIN", "source_type": "calculator", "source_date": "25.08.2026",
                "basis": "25 листов × 380 ₽", "calculation_steps": ["4 изделия с листа", "100 / 4 = 25 листов"],
                "adaptation": "Рассчитано для тиража 100 шт.", "confirmed": True,
            }],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 100}, [production_type], [1])
        cost = result["costs"][0]

        self.assertEqual(cost["calculation_steps"], ["4 изделия с листа", "100 / 4 = 25 листов"])
        self.assertEqual(cost["adaptation"], "Рассчитано для тиража 100 шт.")
        self.assertEqual(cost["source_type"], "calculator")

    def test_recipe_recalculates_current_quantity_instead_of_copying_history_total(self):
        total, steps = _evaluate_cost_recipe({"method": "sheet_yield", "inputs": {"unit_price": 380, "units_per_sheet": 4, "waste_percent": 5}}, Decimal("1000"))

        self.assertEqual(total, Decimal("99940.00"))
        self.assertIn("263 листов", " ".join(steps))

    def test_recipe_applies_discount_to_cost_not_to_quantity(self):
        total, steps = _evaluate_cost_recipe({
            "method": "unit_rate",
            "inputs": {"unit_rate": 1050},
            "modifiers": [{"type": "discount_percent", "value": 15}],
        }, Decimal("5"))

        self.assertEqual(total, Decimal("4462.50"))
        self.assertIn("5250.00 ₽", steps[-1])
        self.assertIn("Скидка 15%", steps[-1])

    def test_recipe_applies_ordered_discount_and_fixed_cost_on_backend(self):
        total, steps = _evaluate_cost_recipe({
            "method": "fixed",
            "inputs": {"fixed_amount": 10000},
            "modifiers": [
                {"type": "discount_percent", "value": 10},
                {"type": "add_fixed", "value": 500},
            ],
        }, Decimal("1"))

        self.assertEqual(total, Decimal("9500.00"))
        self.assertEqual(len(steps), 3)

    def test_recipe_rejects_invalid_discount_instead_of_showing_unreliable_total(self):
        total, steps = _evaluate_cost_recipe({
            "method": "fixed",
            "inputs": {"fixed_amount": 10000},
            "modifiers": [{"type": "discount_percent", "value": 120}],
        }, Decimal("1"))

        self.assertIsNone(total)
        self.assertEqual(steps, [])

    def test_gifts_parser_filters_category_and_maps_image_url(self):
        product_xml = StringIO("""<doct><product product_id=\"v1\"><code>V-1</code><name>Жилет утеплённый</name><product_size>М-L</product_size><matherial>Полиэстер</matherial><color>фиолетовый</color><brand>Brand</brand><content>Описание</content><price><price>1200</price></price><small_image src=\"reviewer/webp/test.webp\"/><ondemand>false</ondemand></product><product product_id=\"m1\"><code>M-1</code><name>Магнит</name></product></doct>""")
        tree_xml = StringIO("""<doct><page page_id=\"10\" name=\"Одежда / Жилеты\"><product product=\"v1\" page=\"10\"/></page><page page_id=\"20\" name=\"Сувениры\"><product product=\"m1\" page=\"20\"/></page></doct>""")
        stock_xml = StringIO("""<doct><stock product_id=\"v1\"><free>7</free><dealerprice>999</dealerprice></stock></doct>""")

        result = parse_gifts_catalog(product_xml, tree_xml, stock_xml, category="жилеты")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["external_id"], "v1")
        self.assertEqual(result[0]["article"], "V-1")
        self.assertEqual(result[0]["total_stock"], 7)
        self.assertEqual(result[0]["discount_price"], Decimal("999.00"))
        self.assertEqual(result[0]["colors"], ["фиолетовый"])
        self.assertEqual(result[0]["image_url"], "https://files.gifts.ru/reviewer/webp/test.webp")

    def test_gifts_parser_keeps_category_map_without_category_filter(self):
        product_xml = StringIO("""<doct><product product_id="v1"><name>Лонгслив</name><code>LS-1</code></product></doct>""")
        tree_xml = StringIO("""<doct><page page_id="10" name="Одежда / Футболки с длинным рукавом"><product product="v1" page="10"/></page></doct>""")

        rows, categories = parse_gifts_catalog(product_xml, tree_xml, include_categories=True)

        self.assertEqual(rows[0]["category_ids"], ["10"])
        self.assertEqual(rows[0]["category_names"], ["Одежда / Футболки с длинным рукавом"])
        self.assertEqual(categories, [{
            "external_id": "10", "parent_external_id": "",
            "name": "Одежда / Футболки с длинным рукавом",
            "path": "Одежда / Футболки с длинным рукавом",
        }])

    def test_gifts_parser_reads_flat_product_page_links(self):
        product_xml = StringIO("""<doct><product><product_id>111501</product_id><code>PU422001</code><name>Рубашка поло</name></product></doct>""")
        tree_xml = StringIO("""
            <doct><page>
                <page><page_id>1105688</page_id><name>Футболки поло</name></page>
                <product><page>1105688</page><product>111501</product></product>
            </page></doct>
        """)

        rows = parse_gifts_catalog(product_xml, tree_xml)

        self.assertEqual(rows[0]["category_ids"], ["1105688"])

    def test_gifts_parser_uses_primary_catalog_image(self):
        product_xml = StringIO("""<doct><product product_id="93294"><code>26728.60</code><name>Жилет Kama, белый</name><super_big_image src="reviewer/webp/26/6728.60_1_500.webp?v=2"/></product></doct>""")
        tree_xml = StringIO("<doct/>")

        result = parse_gifts_catalog(product_xml, tree_xml)

        self.assertEqual(result[0]["image_url"], "https://files.gifts.ru/reviewer/webp/26/6728.60_1_500.webp?v=2")
        self.assertEqual(result[0]["product_url"], "https://gifts.ru/id/93294")

    def test_gifts_parser_maps_xml_thumbnail_to_public_reviewer_image(self):
        product_xml = StringIO("""<doct><product product_id="16224"><code>1376.89</code><name>Футболка унисекс Regent 150, лайм</name><small_image src="thumbnails/7/1376.89_648_200x200.jpg"/></product></doct>""")

        result = parse_gifts_catalog(product_xml, StringIO("<doct/>"))

        self.assertEqual(result[0]["image_url"], "https://files.gifts.ru/reviewer/thumbnails/7/1376.89_648_200x200.jpg")

    def test_gifts_parser_normalizes_protocol_relative_image_url(self):
        product_xml = StringIO("""<doct><product product_id=\"184880\"><code>03564102</code><name>Футболка унисекс Epic, белая</name><small_image src=\"//files.gifts.ru/reviewer/webp/8/03564102_2_200x200.webp?v=2\"/></product></doct>""")
        result = parse_gifts_catalog(product_xml, StringIO("<doct/>"))

        self.assertEqual(result[0]["image_url"], "https://files.gifts.ru/reviewer/webp/8/03564102_2_200x200.webp?v=2")

    def test_gifts_parser_keeps_name_color_out_of_catalog_color_field(self):
        product_xml = StringIO("""<doct><product product_id=\"lime\"><code>03564102</code><name>Футболка унисекс Regent 150, лайм</name></product><product product_id=\"khaki\"><code>03564103</code><name>Футболка унисекс Regent 150, хаки</name></product></doct>""")
        result = parse_gifts_catalog(product_xml, StringIO("<doct/>"))

        self.assertEqual(result[0]["colors"], [])
        self.assertEqual(result[0]["raw_data"]["name_colors"], ["лайм"])
        self.assertEqual(result[1]["colors"], [])
        self.assertEqual(result[1]["raw_data"]["name_colors"], ["хаки"])

    def test_gifts_parser_reads_color_from_filters_catalog(self):
        product_xml = StringIO("""<doct><product product_id=\"v1\"><code>V-1</code><name>Жилет Kama, фиолетовый</name><filters><filter><filtertypeid>21</filtertypeid><filterid>77</filterid></filter></filters></product></doct>""")
        filters_xml = StringIO("""<root><filtertypes><filtertype><filtertypeid>21</filtertypeid><filtertypename>Цвет</filtertypename><filters><filter><filterid>77</filterid><filtername>фиолетовый</filtername></filter></filters></filtertype><filtertype><filtertypeid>99</filtertypeid><filtertypename>Цвет упаковки</filtertypename><filters><filter><filterid>77</filterid><filtername>зеленый</filtername></filter></filters></filtertype></filtertypes></root>""")

        result = parse_gifts_catalog(product_xml, StringIO("<doct/>"), filters_xml=filters_xml)

        self.assertEqual(result[0]["colors"], ["фиолетовый"])

    def test_gifts_parser_finds_image_url_in_unknown_nested_xml_node(self):
        product_xml = StringIO("""<doct><product product_id=\"x\"><code>X</code><name>Товар</name><media><preview data-url=\"//files.gifts.ru/reviewer/webp/x.webp\"/></media></product></doct>""")
        result = parse_gifts_catalog(product_xml, StringIO("<doct/>"))

        self.assertEqual(result[0]["image_url"], "https://files.gifts.ru/reviewer/webp/x.webp")

    @patch.dict("os.environ", {"GIFTS_XML_USERNAME": "user", "GIFTS_XML_PASSWORD": "pass"})
    def test_gifts_client_requires_server_side_credentials(self):
        client = GiftsXmlClient()
        self.assertEqual(client.base_url, "https://api2.gifts.ru/export/v2")


    @patch.dict("os.environ", {"OASIS_API_KEY": ""})
    def test_oasis_client_requires_server_side_api_key(self):
        with self.assertRaisesMessage(CatalogSyncError, "OASIS_API_KEY не настроен"):
            OasisClient()

    @patch("tenders.catalog.urlopen")
    def test_oasis_client_retries_same_request_after_connection_reset(self, urlopen_mock):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"items": []}'

        urlopen_mock.side_effect = [ConnectionResetError("temporary"), Response()]

        result = OasisClient(api_key="test", min_interval=0, max_attempts=2).get("/v4/products")

        self.assertEqual(result, {"items": []})
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_oasis_sync_stores_compact_searchable_catalog_and_dealer_stock(self):
        class FakeClient:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                self.assert_path = path
                return [{"id": 3071, "parent_id": 10, "name": "Футболки", "path": "odezhda/futbolki"}]

            def pages(self, path, params=None, limit=100):
                if path == "/v4/products":
                    yield [{
                        "id": "1-000032048", "article": "3103101S", "article_base": "3103101",
                        "group_id": "100032034", "name": "Футболка Club мужская",
                        "full_name": "Футболка Club мужская, белая, S", "categories": [3071],
                        "materials": ["хлопок"], "colors": [{"name": "белый"}],
                        "attributes": [{"name": "Плотность", "value": "150 г/м²"}],
                        "branding": "Вышивка,DTF", "price": "209.00", "dealerPrice": "190.00",
                        "images": [{"small": "https://s.a-5.ru/test-small.jpg"}], "total_stock": 20,
                        "is_deleted": "0", "is_stopped": "0",
                    }]
                else:
                    yield [{
                        "id": "1-000032048", "article": "3103101S", "stock": 50,
                        "stock-remote": 10, "stock-transit": 100, "price": "209.00",
                        "price-discount": "180.00",
                    }]

        run = sync_oasis_catalog(FakeClient())
        product = CatalogProduct.objects.get()

        self.assertEqual(run.status, "success")
        self.assertEqual(run.created_count, 1)
        self.assertEqual(product.discount_price, Decimal("180.00"))
        self.assertEqual(product.total_stock, 60)
        self.assertEqual(product.category_names, ["odezhda/futbolki"])
        self.assertIn("плотность 150 г/м²", product.search_text)
        self.assertEqual(product.image_url, "https://s.a-5.ru/test-small.jpg")
        self.assertEqual(product.product_url, "https://www.oasiscatalog.com/item/1-000032048")
        self.assertTrue(product.is_active)
        self.assertEqual(product.raw_data, {"discount_group_id": None, "included_branding": None})

    def test_failed_oasis_sync_does_not_deactivate_previous_catalog(self):
        supplier = CatalogSupplier.objects.create(code="oasis", name="Oasis", base_url="https://api.oasiscatalog.com")
        product = CatalogProduct.objects.create(supplier=supplier, external_id="old", article="OLD", name="Старый товар", is_active=True, sync_marker="previous")

        class FailingClient:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                return []

            def pages(self, path, params=None, limit=100):
                if path == "/v4/products":
                    return
                    yield
                raise CatalogSyncError("Остатки временно недоступны")

        with self.assertRaises(CatalogSyncError):
            sync_oasis_catalog(FailingClient())

        product.refresh_from_db()
        supplier.refresh_from_db()
        self.assertTrue(product.is_active)
        self.assertEqual(supplier.sync_status, "failed")
        self.assertEqual(CatalogSyncRun.objects.get().status, "failed")

    def test_catalog_search_enforces_material_density_branding_and_stock(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"
            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки поло", "path": "categories/tekstil/polo"}]
                return [
                    {"id": "exact", "article": "POLO-190", "group_id": "polo-exact", "name": "Футболка поло", "full_name": "Футболка поло тёмно-синяя", "materials": ["хлопок"], "colors": ["темно-синий"], "branding": ["Вышивка", "DTF"], "attributes": [{"name": "Плотность материала", "value": "190 г/м²"}], "price": 700, "discount_price": 650, "total_stock": 500, "categories": [10]},
                    {"id": "thin", "article": "POLO-160", "group_id": "polo-thin", "name": "Футболка поло", "full_name": "Футболка поло тёмно-синяя эконом", "materials": ["хлопок"], "colors": ["темно-синий"], "branding": ["Вышивка"], "attributes": [{"name": "Плотность материала", "value": "160 г/м²"}], "price": 400, "total_stock": 1000, "categories": [10]},
                ]
        line = {
            "name": "Футболка поло", "quantity": "300",
            "requirements": {"requirements": [
                {"label": "Материал", "value": "хлопок 100%"},
                {"label": "Цвет", "value": "темно-синий"},
                {"label": "Плотность", "value": "не менее 190 г/м²"},
                {"label": "Нанесение", "value": "вышивка"},
            ]},
        }

        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "поло"}, client=Client())

        self.assertEqual(candidates[0]["external_id"], "exact")
        self.assertEqual(candidates[0]["fit"], "exact")
        self.assertEqual(candidates[0]["price"], "650.00")
        self.assertEqual(candidates[0]["cost_total"], "195000.00")
        thin = next(value for value in candidates if value["external_id"] == "thin")
        self.assertEqual(thin["fit"], "partial")
        self.assertTrue(any("требуется не менее 190" in value for value in thin["mismatches"]))

    def test_category_candidates_rank_specific_product_above_broad_exact_category(self):
        candidates = _category_candidates({"oasis": [
            {"id": "vip", "name": "Одежда", "path": "vip/odezhda"},
            {"id": "shirts", "name": "Футболки", "path": "odezhda/futbolki"},
            {"id": "polo", "name": "Рубашки поло", "path": "odezhda/rubashki-polo"},
        ]}, {
            "name": "Футболка поло унисекс, цвет – белый",
        }, {
            "item": "рубашка поло унисекс",
            "product_class": "футболка",
            "categories": ["одежда", "футболки"],
            "synonyms": ["футболка поло", "поло унисекс"],
        })

        self.assertEqual(candidates[0]["category_id"], "polo")
        self.assertGreater(candidates[0]["specificity"], next(
            value["specificity"] for value in candidates if value["category_id"] == "vip"
        ))

    def test_catalog_search_prefers_the_specific_keyword_matched_category(self):
        requested_categories = []

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [
                        {"id": "vip", "name": "Одежда", "path": "vip/odezhda"},
                        {"id": "polo", "name": "Рубашки поло", "path": "odezhda/rubashki-polo"},
                    ]
                requested_categories.append(params.get("category"))
                if params.get("category") == "polo":
                    return [{
                        "id": "right", "article": "POLO-1", "group_id": "polo-1",
                        "name": "Рубашка поло мужская", "full_name": "Рубашка поло мужская, белая",
                        "categories": ["polo"], "colors": ["белый"], "materials": ["хлопок"],
                        "attributes": [{"name": "Плотность", "value": "180 г/м2"}],
                        "total_stock": 100, "price": 500,
                    }]
                return [{
                    "id": "wrong", "article": "MITTEN-1", "group_id": "mitten-1",
                    "name": "Варежки", "categories": ["vip"], "total_stock": 100, "price": 100,
                }]

        result = catalog_candidates_for_line(
            {"name": "Футболка поло унисекс", "quantity": 10, "requirements": {"requirements": []}},
            limit=3,
            intent={
                "item": "рубашка поло", "product_class": "футболка",
                "categories": ["одежда", "футболки"], "synonyms": ["футболка поло"],
            },
            client=Client(), include_diagnostics=True,
        )

        self.assertEqual(requested_categories[0], "polo")
        self.assertEqual(result["candidates"][0]["external_id"], "right")
        self.assertEqual(result["attempts"][0]["category_tasks"][0]["category_id"], "polo")

    def test_catalog_search_returns_nearest_candidate_when_required_density_differs(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": "long", "name": "Лонгсливы", "path": "odezhda/longslivy"}]
                return [{
                    "id": "near", "article": "LS-1", "group_id": "long-1",
                    "name": "Футболка с длинным рукавом", "full_name": "Футболка с длинным рукавом белая",
                    "categories": ["long"], "colors": ["белый"],
                    "materials": ["хлопок 100%, плотность 190 г/м2"],
                    "attributes": [{"name": "Плотность", "value": "190 г/м²"}],
                    "total_stock": 100, "price": 700,
                }]

        result = catalog_candidates_for_line(
            {"name": "Лонгслив", "quantity": 50, "requirements": {"requirements": [
                {"label": "Цвет", "value": "белый"},
                {"label": "Плотность", "value": "141 г/м2"},
            ]}},
            intent={
                "item": "лонгслив", "synonyms": ["футболка с длинным рукавом"],
                "required": [{"label": "Плотность", "value": "141 г/м2", "weight": 1}],
                "constraints": [{
                    "field": "density", "operator": "gte", "values": ["141"],
                    "level": "required", "weight": 1, "missing_policy": "reject",
                }],
            },
            client=Client(),
        )

        self.assertEqual(result[0]["external_id"], "near")
        self.assertEqual(result[0]["fit"], "partial")
        self.assertEqual(result[0]["eligibility"], "partial_eligible")
        self.assertTrue(any("Плотность" in value for value in result[0]["mismatches"]))

    def test_catalog_search_picks_the_category_without_any_llm_call(self):
        requested_categories = []

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [
                        {"id": "vip", "name": "Одежда", "path": "vip/odezhda"},
                        {"id": "polo", "name": "Рубашки поло", "path": "odezhda/rubashki-polo"},
                    ]
                requested_categories.append(params.get("category"))
                return [{
                    "id": "right", "article": "POLO-1", "group_id": "polo-1",
                    "name": "Рубашка поло мужская", "full_name": "Рубашка поло мужская, белая",
                    "categories": ["polo"], "colors": ["белый"], "materials": ["хлопок"],
                    "total_stock": 100, "price": 500,
                }]

        result = catalog_candidates_for_line(
            {"name": "Футболка поло унисекс", "quantity": 10, "requirements": {"requirements": []}},
            intent={
                "item": "рубашка поло", "product_class": "футболка",
                "categories": ["одежда"], "synonyms": ["футболка поло"],
            },
            client=Client(), include_diagnostics=True,
        )

        self.assertEqual(requested_categories[0], "polo")
        self.assertEqual(result["candidates"][0]["external_id"], "right")
        self.assertEqual(result["category_errors"], [])

    def test_selected_gifts_category_does_not_depend_on_cached_search_text(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://gifts.ru")
        CatalogCategory.objects.create(
            supplier=gifts, external_id="vacuum", name="Термокружки", path="Посуда / Термокружки",
        )
        CatalogProduct.objects.create(
            supplier=gifts, external_id="travel-mug", article="G-1", name="Термокружка Voyager",
            full_name="Термокружка Voyager, 500 мл", category_ids=["vacuum"],
            total_stock=100, discount_price=500, search_text="",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                return []

        result = catalog_candidates_for_line(
            {"name": "Термокружка", "quantity": 10},
            intent={"item": "термокружка", "categories": ["термокружки"]},
            client=Client(),
        )

        self.assertEqual([value["external_id"] for value in result], ["travel-mug"])


    @staticmethod
    def _shortlist_card(**overrides):
        card = {
            "id": "1", "name": "Товар", "article": "A-1", "price": "100",
            "materials": [], "colors": [], "attributes": [],
            "matches": [], "mismatches": [], "unknown": [],
            "mismatch_count": 0, "unknown_count": 0, "priority": 1, "fit": "exact",
        }
        card.update(overrides)
        return card

    def test_shortlist_pass_is_skipped_when_there_is_nothing_to_apply(self):
        cards = [self._shortlist_card()]
        with patch("tenders.services._ai_gateway_json") as gateway:
            result = _run_shortlist_pass("Поло", [], cards, [])
        gateway.assert_not_called()
        self.assertEqual(result["instructions"], [])
        self.assertEqual(result["ranking"], {})

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_adds_a_mismatch_and_flips_the_card_to_partial(self, gateway):
        gateway.return_value = (
            {"cards": {"1": {"set": [{"point": "Пол", "verdict": "mismatch", "note": "нужен мужской"}]}},
             "instructions": [{"n": 1, "applied": True, "note": "женским проставлен пол"}]},
            {"prompt_tokens": 200, "completion_tokens": 30},
        )
        cards = [self._shortlist_card(matches=["Тип товара: поло"])]

        result = _run_shortlist_pass("Поло", [], cards, [{"text": "нужны мужские", "origin": "session"}])

        self.assertIn("Пол: нужен мужской", cards[0]["mismatches"])
        self.assertEqual(cards[0]["mismatch_count"], 1)
        self.assertEqual(cards[0]["fit"], "partial")
        self.assertTrue(cards[0]["_ai_touched"])
        self.assertTrue(result["instructions"][0]["applied"])
        self.assertEqual(result["outcome"]["touched"], [{"article": "A-1", "name": "Товар"}])

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_raises_priority_only_when_asked(self, gateway):
        gateway.return_value = ({"cards": {"1": {"priority": 0}}}, {})
        cards = [self._shortlist_card()]

        _run_shortlist_pass("Поло", [], cards, [{"text": "подними мужские в первую очередь", "origin": "session"}])

        self.assertEqual(cards[0]["priority"], 0)

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_marks_a_card_removed_with_a_reason(self, gateway):
        gateway.return_value = ({"cards": {"1": {"remove": True, "remove_reason": "детская модель"}}}, {})
        cards = [self._shortlist_card()]

        result = _run_shortlist_pass("Поло", [], cards, [{"text": "убери детские", "origin": "session"}])

        self.assertTrue(cards[0]["_removed"])
        self.assertEqual(cards[0]["_removed_reason"], "детская модель")
        self.assertEqual(result["outcome"]["removed"][0]["reason"], "детская модель")
        self.assertEqual(result["outcome"]["touched"], [])

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_drops_a_verdict_when_told_to_ignore_a_point(self, gateway):
        gateway.return_value = ({"cards": {"1": {"set": [{"point": "Маркировка", "verdict": "none"}]}}}, {})
        cards = [self._shortlist_card(unknown=["Маркировка не указана в каталоге"], unknown_count=1, fit="partial")]

        _run_shortlist_pass("Поло", [], cards, [{"text": "маркировку не учитывай", "origin": "session"}])

        self.assertEqual(cards[0]["unknown"], [])
        self.assertEqual(cards[0]["unknown_count"], 0)
        self.assertEqual(cards[0]["fit"], "exact")

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_fixes_an_existing_mismatch(self, gateway):
        gateway.return_value = (
            {"cards": {"1": {"set": [{"point": "Плотность", "verdict": "match", "note": "220 г считаем нормой"}]}}}, {},
        )
        cards = [self._shortlist_card(
            mismatches=["Плотность 220 г/м²; требуется не менее 250 г/м²"], mismatch_count=1, fit="partial",
        )]

        _run_shortlist_pass("Поло", [], cards, [{"text": "220 г это норм", "origin": "session"}])

        self.assertEqual(cards[0]["mismatches"], [])
        self.assertIn("Плотность: 220 г считаем нормой", cards[0]["matches"])
        self.assertEqual(cards[0]["fit"], "exact")

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_returns_a_session_only_ranking_flip(self, gateway):
        gateway.return_value = ({"ranking": {"price": "desc"}}, {})
        cards = [self._shortlist_card()]

        result = _run_shortlist_pass("Поло", [], cards, [{"text": "сначала показывай дорогие", "origin": "session"}])

        self.assertEqual(result["ranking"], {"price": "desc"})
        self.assertNotIn("_ai_touched", cards[0])

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_survives_a_broken_model_reply(self, gateway):
        gateway.return_value = ("не json", {})
        cards = [self._shortlist_card(mismatches=["x"], mismatch_count=1)]

        result = _run_shortlist_pass("Поло", [], cards, [{"text": "убери детские", "origin": "session"}])

        self.assertEqual(result["error"], "")
        self.assertEqual(cards[0]["mismatches"], ["x"])

    @patch("tenders.services._ai_gateway_json")
    def test_shortlist_pass_reports_a_failed_call_and_keeps_the_order(self, gateway):
        gateway.side_effect = TenderAIError("AI Gateway не ответил")
        cards = [self._shortlist_card()]

        result = _run_shortlist_pass("Поло", [], cards, [{"text": "убери детские", "origin": "session"}])

        self.assertIn("не ответил", result["error"])
        self.assertNotIn("_removed", cards[0])

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_fresh_position_without_feedback_or_lessons_runs_no_shortlist_pass(self, gateway, catalog_search):
        catalog_search.return_value = {"candidates": [
            {"id": "a", "name": "Поло синее", "article": "A", "price": "500", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "mismatches": [], "unknown": []},
        ], "sources": {}, "attempts": []}
        gateway.side_effect = [({"item": "поло", "queries": ["поло"]}, {})]

        result = build_training_hypothesis({"name": "Рубашка поло", "quantity": 10, "requirements": {"requirements": []}})

        self.assertEqual(gateway.call_count, 1)  # search plan only
        self.assertEqual(result["shortlist_instructions"], [])
        self.assertEqual(catalog_search.call_args.kwargs.get("shortlist_limit"), None)

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_feedback_runs_the_pass_and_the_fixed_math_reorders(self, gateway, catalog_search):
        catalog_search.return_value = {"candidates": [
            {"id": "w", "name": "Поло женское", "article": "W", "price": "500", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
            {"id": "m", "name": "Поло мужское", "article": "M", "price": "500", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
        ], "sources": {}, "attempts": []}
        gateway.side_effect = [
            ({"item": "поло", "queries": ["поло"]}, {}),
            ({"cards": {"m": {"priority": 0}}, "instructions": [{"n": 1, "applied": True, "note": "мужское поднято"}]}, {}),
        ]

        result = build_training_hypothesis(
            {"name": "Рубашка поло", "quantity": 10, "requirements": {"requirements": []}},
            feedback="сначала показывай мужские", recompute="catalog",
        )

        self.assertEqual(gateway.call_count, 2)
        self.assertEqual(catalog_search.call_args.kwargs.get("shortlist_limit"), 40)
        self.assertEqual([card["id"] for card in result["catalog_candidates"]], ["m", "w"])
        self.assertTrue(result["shortlist_instructions"][0]["applied"])

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_a_matching_lesson_is_fed_into_the_pass_without_new_feedback(self, gateway, catalog_search):
        Lesson.objects.create(
            scope="catalog", admin_text="всегда убирай детские", summary="убрать детские модели",
            item_word="поло", created_by=self.user,
        )
        catalog_search.return_value = {"candidates": [
            {"id": "k", "name": "Поло детское", "article": "K", "price": "300", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
            {"id": "a", "name": "Поло мужское", "article": "A", "price": "500", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
        ], "sources": {}, "attempts": []}
        gateway.side_effect = [
            ({"item": "поло", "queries": ["поло"]}, {}),
            ({"cards": {"k": {"remove": True, "remove_reason": "детская модель"}},
              "instructions": [{"n": 1, "applied": True}]}, {}),
        ]

        result = build_training_hypothesis({"name": "Рубашка поло", "quantity": 10, "requirements": {"requirements": []}})

        self.assertEqual(gateway.call_count, 2)  # lesson triggers the pass with no feedback
        pass_prompt = gateway.call_args.args[0]
        self.assertIn("убрать детские модели", pass_prompt)
        self.assertEqual([card["id"] for card in result["catalog_candidates"]], ["a"])
        self.assertEqual(result["shortlist_removed"][0]["reason"], "детская модель")
        self.assertEqual(result["shortlist_instructions"][0]["origin"], "lesson")

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_session_ranking_flip_is_carried_between_turns(self, gateway, catalog_search):
        catalog_search.return_value = {"candidates": [
            {"id": "cheap", "name": "Поло A", "article": "A", "price": "300", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
            {"id": "dear", "name": "Поло B", "article": "B", "price": "900", "fit": "exact",
             "priority": 1, "mismatch_count": 0, "unknown_count": 0, "matches": [], "mismatches": [], "unknown": []},
        ], "sources": {}, "attempts": []}
        gateway.side_effect = [
            ({"item": "поло", "queries": ["поло"]}, {}),
            ({"ranking": {"price": "desc"}, "instructions": [{"n": 1, "applied": True, "ranking_only": True}]}, {}),
        ]

        first = build_training_hypothesis(
            {"name": "Поло", "quantity": 10, "requirements": {"requirements": []}},
            feedback="сначала показывай дорогие", recompute="catalog",
        )

        self.assertEqual(first["ranking_override"], {"price": "desc"})
        self.assertEqual([card["id"] for card in first["catalog_candidates"]], ["dear", "cheap"])

        # The one-shot sort instruction is retagged, so the next plain
        # recompute keeps the flip and makes no pass call.
        gateway.side_effect = []
        second = build_training_hypothesis(
            {"name": "Поло", "quantity": 10, "requirements": {"requirements": []}},
            current=first, recompute="catalog",
        )
        self.assertEqual(second["ranking_override"], {"price": "desc"})
        self.assertEqual(second["catalog_intent"]["ranking_override"], {"price": "desc"})
        self.assertEqual(gateway.call_count, 2)  # no third call

    def test_selected_gifts_category_is_filtered_before_candidate_limit(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://gifts.ru")
        CatalogCategory.objects.create(
            supplier=gifts, external_id="long-sleeve", name="Лонгсливы",
            path="Одежда / Футболки с длинным рукавом",
        )
        CatalogProduct.objects.bulk_create([
            CatalogProduct(
                supplier=gifts, external_id=f"other-{index}", name="Кружка",
                category_ids=["mugs"], total_stock=10, search_text="кружка",
            )
            for index in range(1501)
        ])
        CatalogProduct.objects.create(
            supplier=gifts, external_id="wanted-long-sleeve", article="LS-1",
            name="Лонгслив унисекс", category_ids=["long-sleeve"],
            total_stock=100, discount_price=700, search_text="лонгслив унисекс",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                return []

        result = catalog_candidates_for_line(
            {"name": "Лонгслив", "quantity": 10},
            intent={"item": "лонгслив", "synonyms": ["футболка с длинным рукавом"]},
            client=Client(),
        )

        self.assertEqual([value["external_id"] for value in result], ["wanted-long-sleeve"])

    def test_catalog_search_combines_cached_gifts_with_oasis_by_relevance(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="gifts-vest", article="G-1", name="Жилет утеплённый",
            full_name="Жилет утеплённый чёрный", materials=["полиэстер"], colors=["черный"],
            total_stock=20, discount_price=1200, search_text="жилет утепленный черный полиэстер одежда",
            product_url="https://gifts.ru/catalog/G-1",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Жилеты", "path": "categories/odezhda/zhilety"}]
                return [{"id": "oasis-vest", "article": "O-1", "name": "Жилет для работы", "full_name": "Жилет для работы спецодежда", "materials": ["полиэстер"], "colors": ["черный"], "total_stock": 100, "categories": [10]}]

        line = {
            "name": "Жилет", "quantity": "10",
            "requirements": {"requirements": [{"label": "Материал", "value": "полиэстер"}, {"label": "Цвет", "value": "черный"}]},
        }

        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "жилет"}, client=Client())

        self.assertEqual({value["supplier_code"] for value in candidates}, {"oasis", "gifts"})
        self.assertEqual(candidates[0]["supplier_code"], "gifts")

    def test_purple_vest_from_gifts_is_ranked_before_nonmatching_oasis_vest(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="249789", article="26728.78", name="Жилет детский Kama Kids, фиолетовый",
            full_name="Жилет детский Kama Kids, фиолетовый", materials=["полиэстер 100%"], colors=["фиолетовый"],
            total_stock=45, discount_price=2600, search_text="жилет детский kama kids фиолетовый полиэстер одежда",
            product_url="https://gifts.ru/id/249789",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Жилеты", "path": "categories/odezhda/zhilety"}]
                return [{"id": "oasis-vest", "article": "O-1", "name": "Жилет", "full_name": "Жилет чёрный", "colors": ["черный"], "total_stock": 100, "categories": [10]}]

        line = {"name": "Жилет", "quantity": "10", "requirements": {"requirements": [{"label": "Цвет", "value": "фиолетовый"}]}}
        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "жилет"}, client=Client())

        self.assertEqual(candidates[0]["external_id"], "249789")
        self.assertEqual(candidates[0]["supplier_code"], "gifts")
        self.assertEqual(candidates[0]["fit"], "exact")

    def test_gifts_sync_persists_filtered_xml_rows_without_images(self):
        class Client:
            base_url = "https://api2.gifts.ru/export/v2"

            def open(self, path):
                payloads = {
                    "catalogue/product.xml": "<doct><product product_id='v1'><code>V-1</code><name>Жилет фиолетовый</name><matherial>полиэстер</matherial><small_image src='reviewer/v.webp'/></product></doct>",
                    "catalogue/tree.xml": "<doct><page page_id='1' name='Жилеты'><product product='v1'/></page></doct>",
                    "catalogue/stock.xml": "<doct><stock product_id='v1'><free>12</free><dealerprice>1000</dealerprice></stock></doct>",
                    "catalogue/filters.xml": "<root><filtertypes/></root>",
                }
                return StringIO(payloads[path])

        run = sync_gifts_catalog(Client(), category="жилеты")
        product = CatalogProduct.objects.get(supplier__code="gifts", external_id="v1")

        self.assertEqual(run.status, "success")
        self.assertEqual(product.total_stock, 12)
        self.assertEqual(product.image_url, "https://files.gifts.ru/reviewer/v.webp")

    def test_gifts_category_sync_uses_category_only_xml_and_preserves_hierarchy(self):
        class Client:
            base_url = "https://api2.gifts.ru/export/v2"
            opened = []
            xml = """<doct><page page_id='10' name='Одежда'><page page_id='20' name='Поло'><page page_id='30' name='Мужские поло'/></page></page></doct>"""

            def open(self, path):
                self.opened.append(path)
                return StringIO(self.xml)

        client = Client()
        categories = sync_gifts_categories(client)

        self.assertEqual(client.opened, ["catalogue/treeWithoutProducts.xml"])
        self.assertEqual(categories["20"], "Одежда > Поло")
        polo = CatalogCategory.objects.get(supplier__code="gifts", external_id="20")
        self.assertEqual(polo.parent_external_id, "10")
        self.assertEqual(polo.path, "Одежда > Поло")

        client.xml = """<doct><page page_id='10' name='Текстиль'><page page_id='20' name='Поло'/></page></doct>"""
        categories = sync_gifts_categories(client)

        self.assertEqual(categories["20"], "Текстиль > Поло")
        self.assertEqual(CatalogCategory.objects.filter(supplier__code="gifts").count(), 3)
        self.assertFalse(CatalogCategory.objects.get(supplier__code="gifts", external_id="30").is_active)

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_mixed_route_with_finished_good_still_searches_the_catalogue(self, gateway, catalog_search):
        gateway.return_value = ({
            "product_type": "textile_merch", "summary": "Поло", "confidence": .6, "facts": [],
            "route": {"reason": "Закупка готового изделия, нанесение у подрядчика, упаковка в типографии",
                      "processes": [
                          {"name": "Закупка готового изделия"},
                          {"name": "Нанесение"},
                          {"name": "Цифровая типография"},
                      ]},
            "costs": [], "questions": [], "assumptions": [], "matched_example_ids": [], "understood_changes": [],
            "catalog_intent": {"item": "рубашка поло"},
        }, {})
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}

        result = build_training_hypothesis({"name": "Поло", "quantity": 50, "requirements": {"requirements": []}})

        catalog_search.assert_called()
        self.assertNotIn("catalog_skipped", result)

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_frozen_route_carries_both_steps_as_a_process_list(self, gateway, catalog_search):
        # The frozen route must define its processes too, not only the
        # display list — otherwise anything that rebuilds the route from
        # processes (picking a supplier product) silently loses "Нанесение".
        gateway.return_value = ({"item": "поло", "queries": ["поло"]}, {})
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}

        result = build_training_hypothesis({"name": "Поло", "quantity": 50, "requirements": {"requirements": []}})

        self.assertEqual(result["route"]["steps"], ["Закупка готового изделия", "Нанесение"])
        self.assertEqual([p["name"] for p in result["route"]["processes"]], ["Закупка готового изделия", "Нанесение"])

    def test_repeated_tz_rows_collapse_to_one_per_characteristic(self):
        # The ТЗ restates the same spec across several tables with tiny
        # wording differences — the panel must not be a wall of near-dupes.
        collapsed = _collapse_requirements([
            {"label": "Материал ткани", "value": "трикотаж"},
            {"label": "Плотность", "value": "не менее 250 г/м²"},
            {"label": "Материал ткани", "value": "трикотажное полотно 90% хлопок, 10% полиэстер"},
            {"label": "Фактура ткани", "value": "ромбы 3×3 см под углом 90°"},
            {"label": "материал ткани", "value": "трикотаж"},
        ])
        self.assertEqual([r["label"] for r in collapsed], ["Материал ткани", "Плотность", "Фактура ткани"])
        self.assertEqual(collapsed[0]["value"], "трикотажное полотно 90% хлопок, 10% полиэстер")

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_requirement_rows_are_tagged_for_selection(self, gateway, catalog_search):
        # The plan LLM flags rows that are not product criteria; the
        # Честный Знак compliance regex is a free default even without it;
        # a saved skip rule wins over both.
        RequirementSkipRule.objects.create(label="Дизайн", label_normalized="дизайн")
        gateway.return_value = ({"item": "жилет", "queries": ["жилет"], "skip_labels": ["Швы"]}, {})
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}
        line = {"name": "Жилет", "quantity": 60, "requirements": {"requirements": [
            {"label": "Цвет", "value": "темно-синий"},
            {"label": "Маркировка", "value": "маркировка Честного Знака (ЦРПТ)"},
            {"label": "Швы", "value": "четырёхниточный оверлок"},
            {"label": "Дизайн", "value": "макет в трёх вариантах"},
        ]}}

        result = build_training_hypothesis(line)

        flags = {row["label"]: row["selected"] for row in result["requirement_selection"]}
        self.assertEqual(flags, {"Цвет": True, "Маркировка": False, "Швы": False, "Дизайн": False})

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_a_client_supplied_selected_flag_is_never_overwritten(self, gateway, catalog_search):
        gateway.return_value = ({"item": "жилет", "queries": ["жилет"], "skip_labels": ["Цвет"]}, {})
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}
        line = {"name": "Жилет", "quantity": 60, "requirements": {"requirements": [
            {"label": "Цвет", "value": "синий", "selected": True},  # admin re-checked it
            {"label": "Швы", "value": "оверлок", "selected": True},
        ]}}

        result = build_training_hypothesis(line)

        flags = {row["label"]: row["selected"] for row in result["requirement_selection"]}
        self.assertEqual(flags, {"Цвет": True, "Швы": True})

    def test_an_unchecked_row_is_invisible_to_the_matcher(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Жилеты", "path": "categories/tekstil/zhileti"}]
                return [{
                    "id": "v", "article": "V", "group_id": "v", "name": "Жилет",
                    "full_name": "Жилет тёмно-синий", "colors": ["темно-синий"], "categories": [10],
                    "total_stock": 200, "price": 900,
                    "attributes": [{"name": "Плотность", "value": "150 г/м²"}],
                }]

        base = {"name": "Жилет", "quantity": 10, "requirements": {"requirements": [
            {"label": "Цвет", "value": "темно-синий"},
            {"label": "Плотность", "value": "не менее 300 г/м²"},
        ]}}
        checked = catalog_candidates_for_line(base, limit=3, intent={"item": "жилет"}, client=Client())[0]
        self.assertTrue(any("Плотность" in m for m in checked["mismatches"]))

        base["requirements"]["requirements"][1]["selected"] = False
        unchecked = catalog_candidates_for_line(base, limit=3, intent={"item": "жилет"}, client=Client())[0]
        self.assertFalse(any("Плотность" in v for v in unchecked["mismatches"] + unchecked["unknown"]))

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_understood_changes_accumulates_across_feedback_turns(self, gateway, catalog_search):
        # Every piece of catalog feedback stays in "Ваши корректировки" — a
        # second comment must never hide the first.
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}
        line = {"name": "Рубашка поло", "quantity": 50, "requirements": {"requirements": []}}

        gateway.side_effect = [({"item": "рубашка поло", "queries": ["поло"]}, {})]
        first = build_training_hypothesis(line, feedback="исключи детские", recompute="catalog")

        second = build_training_hypothesis(line, current=first, feedback="подними мужских", recompute="catalog")

        self.assertEqual(second["understood_changes"], ["исключи детские", "подними мужских"])
        self.assertEqual([v["text"] for v in second["session_instructions"]], ["исключи детские", "подними мужских"])

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_catalog_scoped_recompute_keeps_route_and_reuses_search_plan(self, gateway, catalog_search):
        # A comment in the catalog step's feedback box changes only the
        # product list: route kept verbatim, search plan not re-derived (the
        # position name has not changed). With an empty shortlist the AI
        # pass does not run either — so no LLM call at all on the recompute.
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}
        line = {"name": "Рубашка поло", "quantity": 50, "requirements": {"requirements": []}}

        gateway.side_effect = [({"item": "рубашка поло", "queries": ["рубашка поло", "поло"]}, {})]
        first = build_training_hypothesis(line)
        self.assertEqual(gateway.call_count, 1)

        second = build_training_hypothesis(line, current=first, feedback="исключи детские", recompute="catalog")

        self.assertEqual(gateway.call_count, 1)
        self.assertEqual(second["route"], first["route"])
        self.assertEqual(second["search_plan"]["item"], "рубашка поло")
        self.assertEqual(second["understood_changes"], ["исключи детские"])

    @patch("tenders.catalog.catalog_candidates_for_line")
    @patch("tenders.services._ai_gateway_json")
    def test_full_recompute_rebuilds_the_search_plan(self, gateway, catalog_search):
        catalog_search.return_value = {"candidates": [], "sources": {}, "attempts": []}
        line = {"name": "Рубашка поло", "quantity": 50, "requirements": {"requirements": []}}

        gateway.side_effect = [({"item": "рубашка поло", "queries": ["поло"]}, {})]
        first = build_training_hypothesis(line)

        gateway.side_effect = [({"item": "рубашка поло классическая", "queries": ["поло"]}, {})]
        second = build_training_hypothesis(line, current=first, feedback="уточни, что классическая", recompute="all")

        self.assertEqual(gateway.call_count, 2)  # plan, then plan again on the full rebuild
        self.assertEqual(second["search_plan"]["item"], "рубашка поло классическая")

    def test_catalog_search_does_not_repeat_color_variants_as_alternatives(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"
            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                return [{"id": external_id, "article": external_id, "group_id": "same-shirt", "name": "Футболка", "full_name": f"Футболка {color}", "colors": [color], "materials": ["хлопок"], "price": 500, "total_stock": 100, "categories": [10]} for external_id, color in (("blue", "синий"), ("red", "красный"))]

        candidates = catalog_candidates_for_line({"name": "Футболка", "quantity": 10}, limit=3, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual(len(candidates), 1)

    def test_catalog_search_reads_later_pages_and_matches_lime_to_green_apple(self):
        target = {
            "id": "00000008300", "article": "3100868S", "group_id": "apple-shirt", "color_group_id": "00000008300",
            "name": "Футболка Super Heavy Super Club мужская",
            "full_name": "Футболка Super Heavy Super Club мужская, зеленое яблоко",
            "colors": [{"name": "зеленое яблоко"}], "materials": ["хлопок"],
            "attributes": [{"name": "Плотность", "value": "180 г/м2"}],
            "branding": ["DTF (Полноцвет)"], "discount_price": "510.60",
            "total_stock": 200, "categories": [10],
        }

        class Client:
            base_url = "https://api.oasiscatalog.com"
            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                if params.get("offset") == 0:
                    return [{
                        "id": f"dummy-{index}", "article": f"D-{index}", "group_id": f"dummy-{index}",
                        "name": "Футболка", "full_name": "Футболка зеленая",
                        "colors": [{"name": "зеленый"}], "materials": ["хлопок"],
                        "attributes": [{"name": "Плотность", "value": "180 г/м2"}],
                        "branding": ["DTF"], "price": "500", "total_stock": 200, "categories": [10],
                    } for index in range(500)]
                return [target]

        line = {"name": "Майка брендированная", "quantity": 160, "requirements": {"requirements": [
            {"label": "Материал", "value": "хлопок"},
            {"label": "Цвет", "value": "лаймово-зелёный"},
            {"label": "Плотность", "value": "не менее 180 г/м²"},
            {"label": "Нанесение", "value": "DTF"},
        ]}}

        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual(candidates[0]["external_id"], "00000008300")
        self.assertEqual(candidates[0]["fit"], "exact")
        self.assertEqual(candidates[0]["price"], "510.60")
        self.assertEqual(candidates[0]["supplier_name"], "Oasis")
        self.assertEqual(candidates[0]["supplier_site"], "oasiscatalog.com")
        self.assertTrue(any("семейство: lime" in value for value in candidates[0]["matches"]))

    def test_catalog_search_reads_oasis_pages_past_first_thousand_products(self):
        target = {
            "id": "target", "article": "POLO-TARGET", "group_id": "target", "color_group_id": "target",
            "name": "Футболка поло", "full_name": "Футболка поло",
            "price": "500", "total_stock": 100, "categories": [3072],
        }

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def __init__(self):
                self.offsets = []

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 3072, "name": "Футболки поло оптом", "path": "categories/tekstil/polo/polo"}]
                self.offsets.append(params["offset"])
                if params["offset"] < 1000:
                    return [{
                        "id": f"dummy-{params['offset'] + index}",
                        "article": f"D-{params['offset'] + index}",
                        "group_id": f"dummy-{params['offset'] + index}",
                        "name": "Футболка поло", "full_name": "Футболка поло",
                        "price": "500", "total_stock": 100, "categories": [3072],
                    } for index in range(500)]
                if params["offset"] == 1000:
                    return [target]
                return []

        client = Client()

        outcome = catalog_candidates_for_line(
            {"name": "Футболка поло", "quantity": 10},
            limit=3, intent={"item": "поло"}, client=client, include_diagnostics=True,
        )

        self.assertEqual(client.offsets, [0, 500, 1000])
        self.assertEqual(outcome["sources"]["oasis"]["received"], 1001)

    def test_catalog_comparison_normalizes_and_deduplicates_volume_requirements(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 20, "name": "Кружки", "path": "categories/posuda/kruzhki"}]
                return [{
                    "id": "mug", "article": "MUG-400", "group_id": "mug", "name": "Кружка Depansar",
                    "full_name": "Кружка Depansar с пробковым дном, черная", "materials": ["керамика", "пробка"],
                    "colors": ["черный"], "attributes": [{"name": "Объем, мл", "value": "400"}],
                    "price": "500", "total_stock": 100, "categories": [20],
                }]

        line = {"name": "Кружка", "quantity": 10, "requirements": {"requirements": [
            {"label": "Объём", "value": "400 мл"},
            {"label": "Объем", "value": "400 см³"},
            {"label": "Материал", "value": "керамика"},
            {"label": "Цвет", "value": "черный"},
            {"label": "Индивидуальная упаковка: плотность", "value": "не менее 300 г/м²"},
        ]}}
        result = catalog_candidates_for_line(
            line, limit=1,
            intent={
                "item": "кружка",
                "required": [{"label": "Плотность", "value": "190 г/м²", "weight": 1}],
                "constraints": [
                    {"field": "volume", "operator": "eq", "values": ["400 ml"], "level": "required"},
                    {"field": "volume", "operator": "eq", "values": ["400 куб. см"], "level": "required"},
                ],
            },
            client=Client(),
        )[0]

        volume_requirements = [value for value in result["normalized_requirements"] if value["field"] == "volume"]
        volume_product_values = [value for value in result["normalized_product_values"] if value["field"] == "volume"]
        self.assertEqual(volume_requirements, [{"field": "volume", "operator": "eq", "value": "400", "unit": "ml"}])
        self.assertEqual(volume_product_values, [{"field": "volume", "value": "400", "unit": "ml"}])
        self.assertEqual(sum(value.startswith("Объём:") for value in result["matches"]), 1)
        self.assertFalse(any("Объём" in value for value in result["mismatches"] + result["unknown"]))
        self.assertFalse(any("Плотность" in value for value in result["matches"] + result["mismatches"] + result["unknown"]))
        self.assertFalse(any(value["field"] == "density" for value in result["normalized_requirements"]))
        self.assertTrue(any(value.startswith("Материал:") for value in result["matches"]))
        self.assertTrue(any(value.startswith("Цвет:") for value in result["matches"]))
        self.assertEqual(result["eligibility"], "exact_eligible")

    def test_real_white_polo_group_is_partial_when_one_requested_size_is_short(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 3072, "name": "Футболки поло оптом", "path": "categories/tekstil/polo/polo"}]
                return [
                    {
                        "id": f"1-000042293-{size}", "article": f"873106{size}",
                        "group_id": "1-000042293-model", "color_group_id": "1-000042293",
                        "name": "Рубашка поло, белая", "full_name": "Рубашка поло, белая",
                        "size": size, "colors": ["белый"], "price": "1411.24",
                        "total_stock": stock, "categories": [3072],
                    }
                    for size, stock in (
                        ("XS", 423), ("S", 493), ("M", 1907), ("L", 1561), ("XL", 1334),
                        ("2XL", 916), ("3XL", 369), ("4XL", 268), ("5XL", 202),
                    )
                ]

        candidate = catalog_candidates_for_line(
            {"name": "Белое поло", "quantity": 600, "requirements": {"requirements": [
                {"label": "Цвет", "value": "белый"},
                {"label": "Размерная раскладка", "value": "S — 500; M — 100"},
            ]}},
            limit=1, intent={"item": "поло"}, client=Client(),
        )[0]

        self.assertEqual(candidate["eligibility"], "partial_eligible")
        self.assertIn("Размер S: доступно 493 из 500 шт.", candidate["eligibility_reasons"])
        self.assertTrue(any(value == "Остаток достаточен: 7473 шт." for value in candidate["matches"]))

    def test_color_group_preserves_variants_and_checks_explicit_size_quantities(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 3072, "name": "Футболки поло оптом", "path": "categories/tekstil/polo/polo"}]
                return [
                    {
                        "id": product_id, "article": article, "group_id": "helios", "color_group_id": "helios-white",
                        "name": "Мужское поло Helios", "full_name": "Мужское поло Helios, белое", "size": size,
                        "colors": ["белый"], "price": price, "total_stock": stock, "categories": [3072],
                    }
                    for product_id, article, size, stock, price in (
                        ("helios-s", "H-S", "S", 5, "500"),
                        ("helios-m", "H-M", "M", 4, "510"),
                        ("helios-l", "H-L", "L", 20, "520"),
                    )
                ]

        with_sizes = catalog_candidates_for_line(
            {"name": "Белое поло", "quantity": 10, "requirements": {"requirements": [
                {"label": "Цвет", "value": "белый"},
                {"label": "Размерный ряд", "value": "S — 5 шт.; M — 5 шт."},
            ]}},
            limit=1, intent={"item": "поло"}, client=Client(),
        )[0]

        self.assertEqual(with_sizes["color_group_id"], "helios-white")
        self.assertEqual(with_sizes["stock"], 29)
        self.assertEqual(with_sizes["variants"], [
            {"size": "S", "product_id": "helios-s", "article": "H-S", "stock": 5, "price": "500.00"},
            {"size": "M", "product_id": "helios-m", "article": "H-M", "stock": 4, "price": "510.00"},
            {"size": "L", "product_id": "helios-l", "article": "H-L", "stock": 20, "price": "520.00"},
        ])
        self.assertTrue(any("Размер S" in value and "5 шт." in value for value in with_sizes["matches"]))
        self.assertTrue(any("Размер M" in value and "4 из 5" in value for value in with_sizes["mismatches"]))
        self.assertEqual(with_sizes["eligibility"], "partial_eligible")
        self.assertTrue(any(value.startswith("Остаток достаточен") for value in with_sizes["matches"]))

        without_sizes = catalog_candidates_for_line(
            {"name": "Белое поло", "quantity": 29, "requirements": {"requirements": [
                {"label": "Цвет", "value": "белый"},
            ]}},
            limit=1, intent={"item": "поло"}, client=Client(),
        )[0]
        self.assertTrue(any(value == "Остаток достаточен: 29 шт." for value in without_sizes["matches"]))
        self.assertEqual(without_sizes["eligibility"], "exact_eligible")
        self.assertFalse(any("Размер " in value for value in without_sizes["matches"] + without_sizes["mismatches"] + without_sizes["unknown"]))

    def test_catalog_eligibility_rejects_insufficient_color_group_total_stock(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 3072, "name": "Поло", "path": "categories/tekstil/polo"}]
                return [
                    {
                        "id": f"white-{size}", "article": f"W-{size}", "group_id": "polo",
                        "color_group_id": "polo-white", "name": "Поло", "full_name": "Поло белое",
                        "size": size, "colors": ["белый"], "price": 500, "total_stock": stock,
                        "categories": [3072],
                    }
                    for size, stock in (("S", 5), ("M", 15))
                ]

        result = catalog_candidates_for_line(
            {"name": "Белое поло", "quantity": 50, "requirements": {"requirements": [
                {"label": "Цвет", "value": "белый"},
            ]}},
            limit=3, intent={"item": "поло"}, client=Client(), include_diagnostics=True,
        )

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["attempts"][0]["eligibility_counts"]["rejected"], 1)
        self.assertEqual(result["attempts"][0]["rejection_reasons"]["Недостаточный общий остаток"], 1)

    def test_catalog_eligibility_keeps_positive_required_mismatch_but_rejects_prohibition(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 3072, "name": "Поло", "path": "categories/tekstil/polo"}]
                return [
                    {
                        "id": "male-poly", "article": "MP", "group_id": "male-poly", "name": "Поло мужское",
                        "full_name": "Поло мужское, белое", "materials": ["полиэстер"], "colors": ["белый"],
                        "price": 400, "total_stock": 100, "categories": [3072],
                    },
                    {
                        "id": "female-cotton", "article": "FC", "group_id": "female-cotton", "name": "Поло женское",
                        "full_name": "Поло женское, белое", "materials": ["хлопок"], "colors": ["белый"],
                        "price": 400, "total_stock": 100, "categories": [3072],
                    },
                ]

        result = catalog_candidates_for_line(
            {"name": "Белое поло", "quantity": 20, "requirements": {"requirements": [
                {"label": "Материал", "value": "хлопок"},
                {"label": "Цвет", "value": "белый"},
            ]}},
            limit=3,
            intent={
                "item": "поло",
                "required": [{"label": "Материал", "value": "хлопок", "weight": 1}],
                "constraints": [{
                    "field": "gender", "operator": "not_in", "values": ["female"],
                    "level": "required", "missing_policy": "allow",
                }],
            },
            client=Client(), include_diagnostics=True,
        )

        self.assertEqual([value["external_id"] for value in result["candidates"]], ["male-poly"])
        self.assertEqual(result["candidates"][0]["eligibility"], "partial_eligible")
        self.assertTrue(any("Материал не совпадает" in value for value in result["candidates"][0]["eligibility_reasons"]))
        self.assertEqual(result["attempts"][0]["eligibility_counts"]["rejected"], 1)

    def test_catalog_eligibility_applies_missing_policy_without_penalty(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 20, "name": "Кружки", "path": "categories/posuda/kruzhki"}]
                return [{
                    "id": "mug", "article": "M", "group_id": "mug", "name": "Кружка",
                    "full_name": "Кружка белая", "colors": ["белый"], "price": 300,
                    "total_stock": 100, "categories": [20],
                }]

        def outcome(policy):
            return catalog_candidates_for_line(
                {"name": "Кружка", "quantity": 10, "requirements": {"requirements": [
                    {"label": "Плотность", "value": "не менее 180 г/м²"},
                ]}},
                limit=1,
                intent={"item": "кружка", "constraints": [{
                    "field": "density", "operator": "gte", "values": ["180"],
                    "level": "required", "missing_policy": policy,
                }]},
                client=Client(), include_diagnostics=True,
            )

        rejected = outcome("reject")
        allowed = outcome("allow")
        allowed_with_penalty = outcome("allow_with_penalty")

        # A missing required characteristic no longer removes the product:
        # "reject" now only surfaces the gap and ranks it as a partial match.
        self.assertEqual(rejected["candidates"][0]["eligibility"], "partial_eligible")
        self.assertEqual(allowed["candidates"][0]["eligibility"], "exact_eligible")
        self.assertEqual(allowed_with_penalty["candidates"][0]["eligibility"], "exact_eligible")
        self.assertTrue(allowed_with_penalty["candidates"][0]["unknown"])

    def test_catalog_eligibility_enforces_source_only_only_after_confirmed_operation(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 20, "name": "Кружки", "path": "categories/posuda/kruzhki"}]
                return [{
                    "id": "mug", "article": "M", "group_id": "mug", "name": "Кружка",
                    "full_name": "Кружка", "price": 300, "total_stock": 100, "categories": [20],
                }]

        line = {"name": "Кружка", "quantity": 10}
        llm_only = catalog_candidates_for_line(
            line, limit=1, intent={"item": "кружка", "allowed_sources": ["gifts"]}, client=Client(),
        )
        confirmed = catalog_candidates_for_line(
            line, limit=1,
            intent={"item": "кружка", "allowed_sources": ["gifts"], "_source_only_confirmed": True},
            client=Client(), include_diagnostics=True,
        )

        self.assertEqual(llm_only[0]["eligibility"], "exact_eligible")
        self.assertEqual(confirmed["candidates"], [])
        self.assertEqual(confirmed["attempts"][0]["rejections"]["source"], 1)

    def test_catalog_search_uses_name_shade_as_soft_hint_with_explicit_parent_color(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="lime-shirt", article="1376.89", name="Футболка унисекс Regent 150, лайм",
            full_name="Футболка унисекс Regent 150, лайм", materials=["хлопок"], colors=["зеленый"],
            raw_data={"name_colors": ["лайм"]}, total_stock=100, discount_price=404,
            search_text="футболка лайм зеленый хлопок", product_url="https://gifts.ru/id/16224",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                return []

        line = {"name": "Футболка", "quantity": "10", "requirements": {"requirements": [{"label": "Цвет", "value": "лайм"}]}}
        candidates = catalog_candidates_for_line(line, limit=1, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual(candidates[0]["external_id"], "lime-shirt")
        self.assertEqual(candidates[0]["fit"], "exact")
        self.assertTrue(any("Цвет: зеленый" in value for value in candidates[0]["matches"]))

    def test_catalog_search_uses_price_after_equal_relevance(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                return [
                    {"id": "expensive", "article": "E", "group_id": "expensive", "name": "Футболка", "full_name": "Футболка", "materials": ["хлопок"], "colors": ["белый"], "price": "700", "categories": [10], "total_stock": 100},
                    {"id": "cheap", "article": "C", "group_id": "cheap", "name": "Футболка", "full_name": "Футболка", "materials": ["хлопок"], "colors": ["белый"], "price": "500", "categories": [10], "total_stock": 100},
                ]

        line = {"name": "Футболка", "quantity": "10", "requirements": {"requirements": [{"label": "Материал", "value": "хлопок"}, {"label": "Цвет", "value": "белый"}]}}
        candidates = catalog_candidates_for_line(line, limit=2, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["cheap", "expensive"])

    def test_catalog_search_prioritizes_requirements_over_product_name_similarity(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Ручки", "path": "categories/office/pens"}]
                return [
                    {
                        "id": "popular", "article": "P", "group_id": "popular",
                        "name": "Ручка шариковая Popular", "full_name": "Ручка шариковая Popular, зеленая",
                        "materials": ["металл"], "colors": ["зеленый"], "attributes": [],
                        "categories": [10], "total_stock": 100, "price": "84",
                    },
                    {
                        "id": "gold", "article": "G", "group_id": "gold",
                        "name": "Ручка шариковая Euro Gold", "full_name": "Ручка шариковая Euro Gold, зеленая",
                        "materials": ["металл"], "colors": ["зеленый"],
                        "attributes": [{"name": "Чернила", "value": "синие"}, {"name": "Механизм", "value": "поворотный"}],
                        "categories": [10], "total_stock": 100, "price": "12.80",
                    },
                    {
                        "id": "chrome", "article": "C", "group_id": "chrome",
                        "name": "Ручка шариковая Euro Chrome", "full_name": "Ручка шариковая Euro Chrome, зеленая",
                        "materials": ["металл"], "colors": ["зеленый"],
                        "attributes": [{"name": "Чернила", "value": "синие"}, {"name": "Механизм", "value": "поворотный"}],
                        "categories": [10], "total_stock": 100, "price": "10.60",
                    },
                ]

        line = {"name": "Ручка, зелёная, материал – металл, чернила синие, механизм поворотный", "quantity": "10", "requirements": {"requirements": [
            {"label": "Материал", "value": "металл"},
            {"label": "Цвет", "value": "зелёная"},
            {"label": "Чернила", "value": "синие"},
            {"label": "Механизм", "value": "поворотный"},
            {"label": "Нанесение", "value": "гравировка 1+0"},
        ]}}

        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "ручка"}, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["chrome", "gold", "popular"])
        self.assertTrue(any("Чернила" in value for value in candidates[0]["matches"]))
        self.assertTrue(any("Механизм" in value for value in candidates[0]["matches"]))

    def test_catalog_search_uses_llm_specific_categories_instead_of_generic_class(self):
        products = [
            {"id": "shirt", "article": "S", "group_id": "shirt", "name": "Футболка", "full_name": "Футболка белая", "colors": ["белый"], "categories": [10], "total_stock": 100, "price": "500"},
            {"id": "polo", "article": "P", "group_id": "polo", "name": "Футболка поло", "full_name": "Футболка поло белая", "colors": ["белый"], "categories": [11], "total_stock": 100, "price": "600"},
        ]

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [
                        {"id": 10, "name": "Футболки", "path": "categories/textile/tshirts"},
                        {"id": 11, "name": "Поло", "path": "categories/textile/polo"},
                    ]
                category = (params or {}).get("category")
                return [
                    value for value in products
                    if category is None or str(category) in {str(item) for item in value["categories"]}
                ]

        line = {"name": "Футболка поло унисекс", "quantity": "10", "requirements": {"requirements": [{"label": "Цвет", "value": "белый"}]}}
        intent = {"product_class": "футболка", "item": "поло", "categories": ["поло", "рубашка поло"], "synonyms": [], "required": [], "preferred": [], "secondary": []}

        candidates = catalog_candidates_for_line(line, limit=3, intent=intent, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["polo"])

    def test_catalog_search_marks_product_that_breaks_positive_required_constraint_partial(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/textile/tshirts"}]
                return [
                    {
                        "id": "cotton", "article": "C", "group_id": "cotton", "name": "Футболка",
                        "full_name": "Футболка хлопковая белая", "materials": ["хлопок"], "colors": ["белый"],
                        "categories": [10], "total_stock": 100, "price": "900",
                    },
                    {
                        "id": "cheap-polyester", "article": "P", "group_id": "polyester", "name": "Футболка",
                        "full_name": "Футболка из полиэстера белая", "materials": ["полиэстер"], "colors": ["белый"],
                        "categories": [10], "total_stock": 100, "price": "100",
                    },
                ]

        line = {"name": "Футболка", "quantity": 10, "requirements": {"requirements": []}}
        intent = {
            "categories": ["футболка"],
            "required": [{"label": "Состав", "value": "хлопок", "weight": 1}],
            "ranking": [{"criterion": "цена", "weight": 1}],
        }

        candidates = catalog_candidates_for_line(line, limit=3, intent=intent, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["cotton", "cheap-polyester"])
        self.assertEqual([value["eligibility"] for value in candidates], ["exact_eligible", "partial_eligible"])

    def test_catalog_constraints_exclude_forbidden_value_and_read_fact_from_name_or_attribute(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Поло", "path": "categories/textile/polo"}]
                return [
                    {"id": "female-attribute", "article": "F1", "group_id": "f1", "name": "Поло Boston", "full_name": "Поло Boston белое", "attributes": [{"name": "Пол", "value": "женский"}], "colors": ["белый"], "categories": [10], "total_stock": 100, "price": 100},
                    {"id": "female-name", "article": "F2", "group_id": "f2", "name": "Поло Boston женское", "full_name": "Поло Boston женское, белое", "colors": ["белый"], "categories": [10], "total_stock": 100, "price": 90},
                    {"id": "male", "article": "M", "group_id": "m", "name": "Поло Laguna мужское", "full_name": "Поло Laguna мужское, белое", "attributes": [{"name": "Пол", "value": "мужской"}], "colors": ["белый"], "categories": [10], "total_stock": 100, "price": 200},
                    {"id": "unspecified", "article": "U", "group_id": "u", "name": "Поло Base", "full_name": "Поло Base, белое", "colors": ["белый"], "categories": [10], "total_stock": 100, "price": 80},
                ]

        intent = {
            "categories": ["поло"],
            "constraints": [
                {
                    "field": "gender", "operator": "in", "values": ["male", "unisex"],
                    "level": "required", "weight": 1, "missing_policy": "allow_with_penalty",
                },
                {
                    "field": "gender", "operator": "not_in", "values": ["female"],
                    "level": "required", "weight": 1, "missing_policy": "allow_with_penalty",
                },
            ],
        }

        candidates = catalog_candidates_for_line(
            {"name": "Поло унисекс", "quantity": 10, "requirements": {"requirements": []}},
            limit=10, intent=intent, client=Client(),
        )

        self.assertEqual([value["external_id"] for value in candidates], ["male", "unspecified"])
        self.assertTrue(any("Пол" in value for value in candidates[0]["matches"]))
        self.assertEqual(sum("Пол" in value for value in candidates[0]["matches"]), 1)
        self.assertTrue(any("Пол не указан" in value for value in candidates[1]["unknown"]))

    def test_chestny_znak_requirement_never_becomes_a_product_mismatch(self):
        # Live bug: every tender requires "Маркировка Честного Знака / ЦРПТ".
        # Oasis catalogues store an unrelated certification DATE under a field
        # also named "Маркировка", so the matcher called it a mismatch and
        # every Oasis product sank below every Gifts one (Gifts has no such
        # field → a harmless "unknown"). The compliance-marking requirement
        # must be ignored for BOTH.
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Жилеты", "path": "categories/tekstil/zhileti"}]
                return [
                    {
                        "id": "with-date", "article": "WD", "group_id": "wd", "name": "Жилет",
                        "full_name": "Жилет тёмно-синий", "colors": ["темно-синий"], "categories": [10],
                        "total_stock": 200, "price": 900,
                        "attributes": [{"name": "Маркировка", "value": "2024-04-01"}],
                    },
                    {
                        "id": "no-attr", "article": "NA", "group_id": "na", "name": "Жилет",
                        "full_name": "Жилет тёмно-синий", "colors": ["темно-синий"], "categories": [10],
                        "total_stock": 200, "price": 1100, "attributes": [],
                    },
                ]

        line = {"name": "Жилет", "quantity": 10, "requirements": {"requirements": [
            {"label": "Цвет", "value": "темно-синий"},
            {"label": "Маркировка", "value": "маркировка Честного Знака (уникальный цифровой код, выданный ЦРПТ)"},
        ]}}

        candidates = catalog_candidates_for_line(line, limit=10, intent={"item": "жилет"}, client=Client())

        self.assertEqual([c["external_id"] for c in candidates], ["with-date", "no-attr"])
        for c in candidates:
            self.assertFalse(any("Маркировк" in m for m in c["mismatches"]), c["mismatches"])
            self.assertFalse(any("Маркировк" in u for u in c["unknown"]), c["unknown"])

    def test_catalog_positive_numeric_constraints_keep_nearest_alternatives_partial(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Поло", "path": "categories/textile/polo"}]
                return [
                    {"id": "valid", "article": "V", "group_id": "v", "name": "Поло", "full_name": "Поло белое", "attributes": [{"name": "Плотность", "value": "190 г/м²"}], "categories": [10], "total_stock": 150, "price": 450},
                    {"id": "expensive", "article": "E", "group_id": "e", "name": "Поло", "full_name": "Поло белое", "attributes": [{"name": "Плотность", "value": "190 г/м²"}], "categories": [10], "total_stock": 150, "price": 700},
                    {"id": "thin", "article": "T", "group_id": "t", "name": "Поло", "full_name": "Поло белое", "attributes": [{"name": "Плотность", "value": "150 г/м²"}], "categories": [10], "total_stock": 150, "price": 300},
                ]

        intent = {"categories": ["поло"], "constraints": [
            {"field": "price", "operator": "lte", "values": ["500"], "level": "required", "weight": 1, "missing_policy": "reject"},
            {"field": "density", "operator": "between", "values": ["180", "220"], "level": "required", "weight": 1, "missing_policy": "reject"},
            {"field": "stock", "operator": "gte", "values": ["100"], "level": "required", "weight": 1, "missing_policy": "reject"},
        ]}

        candidates = catalog_candidates_for_line({"name": "Поло", "quantity": 100}, limit=10, intent=intent, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["valid", "thin", "expensive"])
        self.assertEqual(candidates[0]["eligibility"], "exact_eligible")
        self.assertTrue(all(value["eligibility"] == "partial_eligible" for value in candidates[1:]))

    def test_cheaper_product_does_not_outrank_a_closer_match(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/textile/tshirts"}]
                return [
                    {
                        "id": "cotton", "article": "C", "group_id": "cotton", "name": "Футболка",
                        "full_name": "Футболка хлопковая белая", "materials": ["хлопок"], "colors": ["белый"],
                        "categories": [10], "total_stock": 100, "price": "900",
                    },
                    {
                        "id": "cheap-polyester", "article": "P", "group_id": "polyester", "name": "Футболка",
                        "full_name": "Футболка из полиэстера белая", "materials": ["полиэстер"], "colors": ["белый"],
                        "categories": [10], "total_stock": 100, "price": "100",
                    },
                ]

        line = {"name": "Футболка", "quantity": 10, "requirements": {"requirements": [{"label": "Материал", "value": "хлопок"}]}}
        intent = {"categories": ["футболка"], "required": [{"label": "Материал", "value": "хлопок"}]}

        candidates = catalog_candidates_for_line(line, limit=2, intent=intent, client=Client())

        # The cotton shirt matches the material; the cheaper polyester one does
        # not, so price never lets it climb above.
        self.assertEqual([value["external_id"] for value in candidates], ["cotton", "cheap-polyester"])

    def test_catalog_search_returns_source_diagnostics_instead_of_hiding_oasis_failure(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="shirt", article="G", name="Футболка", full_name="Футболка белая",
            colors=["белый"], total_stock=100, discount_price=500, search_text="футболка белая",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                raise CatalogSyncError("Oasis временно недоступен")

        result = catalog_candidates_for_line(
            {"name": "Футболка", "quantity": 10},
            limit=3,
            intent={"categories": ["футболка"]},
            client=Client(),
            include_diagnostics=True,
        )

        self.assertEqual(result["candidates"][0]["supplier_code"], "gifts")
        self.assertEqual(result["sources"]["oasis"]["status"], "failed")
        self.assertIn("временно недоступен", result["sources"]["oasis"]["message"])
        self.assertEqual(result["sources"]["gifts"]["status"], "success")

    def test_gifts_retrieval_uses_product_entity_before_broad_characteristics(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.bulk_create([
            CatalogProduct(
                supplier=gifts, external_id=f"mug-{index}", article=f"M-{index}", name="Кружка синяя",
                full_name="Кружка синяя", colors=["синий"], total_stock=100, discount_price=100,
                search_text="кружка синяя",
            )
            for index in range(1500)
        ])
        CatalogProduct.objects.create(
            supplier=gifts, external_id="shirt", article="S", name="Футболка синяя",
            full_name="Футболка синяя", colors=["синий"], total_stock=100, discount_price=500,
            search_text="футболка синяя",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                raise CatalogSyncError("Oasis временно недоступен")

        candidates = catalog_candidates_for_line(
            {"name": "Футболка", "quantity": 10},
            limit=3,
            intent={
                "categories": ["футболка"],
                "required": [{"label": "Цвет", "value": "синий", "weight": 1}],
            },
            client=Client(),
        )

        self.assertEqual([value["external_id"] for value in candidates], ["shirt"])

    def test_catalog_entity_match_does_not_treat_polo_as_towel_prefix(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Текстиль", "path": "categories/textile"}]
                return [{
                    "id": "towel", "article": "T", "group_id": "towel", "name": "Полотенце",
                    "full_name": "Полотенце синее", "colors": ["синий"], "categories": [10],
                    "total_stock": 100, "price": 300,
                }]

        candidates = catalog_candidates_for_line(
            {"name": "Поло", "quantity": 10},
            limit=3,
            intent={"categories": ["поло"]},
            client=Client(),
        )

        self.assertEqual(candidates, [])

    def test_catalog_search_does_not_prefer_supplier_when_relevance_and_price_are_equal(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="gifts-shirt", article="G", name="Футболка", full_name="Футболка Б",
            materials=["хлопок"], colors=["белый"], discount_price=500, total_stock=100,
            search_text="футболка б хлопок белый",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                return [{"id": "oasis-shirt", "article": "O", "group_id": "oasis-shirt", "name": "Футболка", "full_name": "Футболка А", "materials": ["хлопок"], "colors": ["белый"], "price": "500", "categories": [10], "total_stock": 100}]

        line = {"name": "Футболка", "quantity": "10", "requirements": {"requirements": [{"label": "Материал", "value": "хлопок"}, {"label": "Цвет", "value": "белый"}]}}
        candidates = catalog_candidates_for_line(line, limit=2, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual([value["supplier_code"] for value in candidates], ["oasis", "gifts"])

    def test_catalog_search_uses_gifts_text_when_oasis_category_is_missing(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="longsleeve-white", article="LS-1",
            name="Лонгслив унисекс", full_name="Лонгслив унисекс, белый",
            colors=["белый"], total_stock=100, discount_price=500,
            search_text="лонгслив унисекс белый хлопок футболка с длинным рукавом",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Брелоки", "path": "categories/accessories"}]
                return []

        line = {"name": "Лонгслив, унисекс", "quantity": "10", "requirements": {"requirements": [{"label": "Цвет", "value": "белый"}]}}
        candidates = catalog_candidates_for_line(
            line,
            limit=3,
            intent={"item": "лонгслив", "categories": ["лонгслив"], "synonyms": ["long sleeve"]},
            client=Client(),
        )

        self.assertEqual(candidates[0]["external_id"], "longsleeve-white")
        self.assertEqual(candidates[0]["supplier_code"], "gifts")

    def test_catalog_search_keeps_gifts_when_oasis_is_unavailable(self):
        gifts = CatalogSupplier.objects.create(code="gifts", name="gifts.ru", base_url="https://api2.gifts.ru/export/v2")
        CatalogProduct.objects.create(
            supplier=gifts, external_id="shirt", article="S-1", name="Футболка",
            full_name="Футболка белая", colors=["белый"], total_stock=100,
            discount_price=500, search_text="футболка белая хлопок",
        )

        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                raise CatalogSyncError("Oasis недоступен")

        candidates = catalog_candidates_for_line(
            {"name": "Футболка", "quantity": "10", "requirements": {"requirements": [{"label": "Цвет", "value": "белый"}]}},
            limit=3, intent={"product_class": "футболка"}, client=Client(),
        )

        self.assertEqual(candidates[0]["external_id"], "shirt")
        self.assertEqual(candidates[0]["supplier_code"], "gifts")

    def test_catalog_search_rejects_zero_stock_and_total_stock_shortage(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Футболки", "path": "categories/tekstil/futbolki"}]
                return [
                    {"id": "available", "article": "A", "group_id": "available", "name": "Футболка", "full_name": "Футболка белая", "colors": ["белый"], "materials": ["хлопок"], "categories": [10], "total_stock": 100, "price": 900},
                    {"id": "shortage", "article": "S", "group_id": "shortage", "name": "Футболка", "full_name": "Футболка белая", "colors": ["белый"], "materials": ["полиэстер"], "categories": [10], "total_stock": 5, "price": 100},
                    {"id": "empty", "article": "E", "group_id": "empty", "name": "Футболка", "full_name": "Футболка белая", "colors": ["белый"], "materials": ["хлопок"], "categories": [10], "total_stock": 0, "price": 1},
                ]

        line = {"name": "Футболка", "quantity": "10", "requirements": {"requirements": [{"label": "Материал", "value": "хлопок"}, {"label": "Цвет", "value": "белый"}]}}
        candidates = catalog_candidates_for_line(line, limit=3, intent={"product_class": "футболка"}, client=Client())

        self.assertEqual([value["external_id"] for value in candidates], ["available"])
        self.assertEqual(candidates[0]["eligibility"], "exact_eligible")

    def test_catalog_search_does_not_call_a_shirt_with_long_sleeves_a_longsleeve(self):
        class Client:
            base_url = "https://api.oasiscatalog.com"

            def get(self, path, params=None):
                if path == "/v4/categories":
                    return [{"id": 10, "name": "Одежда", "path": "categories/odezhda"}]
                return [{
                    "id": "shirt", "article": "SH-1", "group_id": "shirt",
                    "name": "Рубашка женская", "full_name": "Рубашка женская с длинным рукавом",
                    "description": "Футболка с длинным рукавом в описании модели", "categories": [10], "total_stock": 100,
                    "price": 900,
                }]

        candidates = catalog_candidates_for_line(
            {"name": "Лонгслив", "quantity": "10"},
            limit=3, intent={"product_class": "лонгслив"}, client=Client(),
        )

        self.assertEqual(candidates, [])

    def test_selected_catalog_product_is_recalculated_on_backend_and_replaces_material_cost(self):
        production_type = ProductionType.objects.create(code="catalog-product", name="Каталожный товар")
        line = {
            "name": "Футболка поло", "quantity": "300",
            "requirements": {"requirements": [
                {"label": "Материал", "value": "хлопок"},
                {"label": "Цвет", "value": "темно-синий"},
                {"label": "Плотность", "value": "не менее 190 г/м²"},
                {"label": "Нанесение", "value": "вышивка"},
            ]},
        }
        hypothesis = {
            "product_type": production_type.code, "confidence": .8, "matched_example_ids": [1],
            "route": {"reason": "Нужен готовый товар и нанесение", "processes": [{"name": "Закупка материала"}, {"name": "Нанесение"}]},
            "questions": ["Какова цена закупки готовой футболки?", "Какова цена нанесения?"],
            "costs": [{
                "category": "material", "name": "Старая ручная цена", "amount_total": "99999",
                "source": "Введено администратором", "source_type": "manager", "process_name": "Закупка материала",
            }],
            "catalog_candidates": [{"id": "exact", "external_id": "exact", "supplier_code": "oasis", "article": "POLO-190", "name": "Футболка поло тёмно-синяя", "price": "650.00", "stock": 500, "url": "https://www.oasiscatalog.com/item/exact", "fit": "exact", "matches": ["Тип товара: поло"], "mismatches": [], "unknown": []}],
        }

        result = apply_catalog_candidate(hypothesis, line, "exact")

        self.assertEqual(result["catalog_selection"]["id"], "exact")
        self.assertIn({"code": production_type.code, "name": production_type.name}, result["production_types"])
        self.assertEqual(result["totals"]["material_unit"], "650.00")
        self.assertEqual(result["totals"]["cost_total"], "195000.00")
        self.assertEqual(result["costs"][0]["source_type"], "catalog")
        self.assertEqual(result["costs"][0]["calculation_steps"][-1], "300 шт. × 650.00 ₽/шт. = 195000.00 ₽")
        # Route stays frozen — picking a product keeps both steps, does not
        # drop "Нанесение", and does not surface a guessed type/confidence.
        self.assertEqual(result["route"]["steps"], ["Закупка готового изделия", "Нанесение"])
        self.assertEqual(result["product_type"], "")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["questions"], ["Какова цена нанесения?"])
        self.assertIn("поставщика Oasis", result["route"]["reason"])
        self.assertEqual(result["sources"][-1]["supplier_name"], "Oasis")
        self.assertEqual(result["sources"][-1]["price"], "650.00")

    def test_admin_can_use_partial_catalog_candidate_with_visible_mismatches(self):
        production_type = ProductionType.objects.create(code="catalog-partial", name="Каталожный товар")
        line = {"name": "Футболка", "quantity": "10", "requirements": {"requirements": []}}
        hypothesis = {
            "product_type": production_type.code, "confidence": .5,
            "route": {"reason": "Каталог", "processes": [{"name": "Закупка готового изделия"}]},
            "costs": [], "catalog_candidates": [{
                "id": "partial", "external_id": "partial", "supplier_code": "other",
                "supplier_name": "Другой поставщик", "supplier_site": "catalog.example",
                "article": "PART-1", "name": "Футболка зелёная", "price": "400.00",
                "stock": 100, "url": "https://www.oasiscatalog.com/item/partial",
                "fit": "partial", "matches": ["Тип товара: футболка"],
                "mismatches": ["Плотность ниже требования"], "unknown": [],
            }],
        }

        result = apply_catalog_candidate(hypothesis, line, "partial")

        self.assertEqual(result["totals"]["material_unit"], "400.00")
        self.assertEqual(result["totals"]["cost_total"], "4000.00")
        self.assertEqual(result["catalog_selection"]["selection_mode"], "manual")
        self.assertEqual(result["catalog_selection"]["accepted_mismatches"], ["Плотность ниже требования"])
        self.assertEqual(result["sources"][-1]["supplier_name"], "Другой поставщик")
        self.assertIn("поставщика Другой поставщик", result["route"]["reason"])
        # "Нанесение" survives even though the incoming route never listed it.
        self.assertEqual(result["route"]["steps"], ["Закупка готового изделия", "Нанесение"])

    def test_picking_a_lower_candidate_keeps_the_whole_shortlist_and_moves_it_first(self):
        # Bug: choosing the 3rd of 10 products left only 3 in the list (a
        # stray [:3] truncation) and the collapsed preview still showed the
        # 1st. The chosen product now leads the list and none are dropped.
        line = {"name": "Жилет", "quantity": "5", "requirements": {"requirements": []}}
        cands = [
            {
                "id": f"v{i}", "external_id": f"v{i}", "supplier_code": "gifts",
                "supplier_name": "Gifts", "article": f"A{i}", "name": f"Жилет {i}",
                "price": f"{100 + i}.00", "stock": 50, "url": f"http://x/{i}",
                "fit": "partial", "matches": [], "mismatches": ["размер"], "unknown": [],
            }
            for i in range(10)
        ]
        hypothesis = {"route": {"steps": ["Закупка готового изделия", "Нанесение"]}, "costs": [], "catalog_candidates": cands}

        result = apply_catalog_candidate(hypothesis, line, "v2")

        ids = [c["id"] for c in result["catalog_candidates"]]
        self.assertEqual(len(ids), 10)
        self.assertEqual(ids[0], "v2")
        self.assertEqual(set(ids), {f"v{i}" for i in range(10)})
        self.assertEqual(result["catalog_candidates"][0]["cost_total"], "510.00")

    def test_route_name_contains_only_universal_processes(self):
        production_type = ProductionType.objects.create(code="route-test", name="Тест маршрута")
        raw = {
            "product_type": production_type.code,
            "route": {"reason": "Тираж и отделка", "processes": [
                {"name": "Поставка дизайнерской бумаги Majestic для папки", "details": ["Majestic"]},
                {"name": "Изготовление папок с резкой, биговкой и тиснением в универсальной типографии", "details": ["резка", "биговка"]},
            ]},
            "costs": [],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 100}, [production_type], [1])

        self.assertEqual(result["route"]["name"], "Закупка материала → Универсальная типография")
        self.assertEqual(result["route"]["processes"][1]["details"], ["резка", "биговка"])

    def test_turnkey_manufacturing_is_not_labelled_as_material_purchase(self):
        production_type = ProductionType.objects.create(code="turnkey-test", name="Тест под ключ")
        raw = {
            "product_type": production_type.code,
            "route": {
                "reason": "Заказать изготовление под ключ в цифровой типографии: бумага, печать 4+4, выборочный УФ-лак и резка.",
                "processes": [{"name": "Закупка материала", "details": ["Типография предоставляет бумагу"]}],
            },
            "costs": [],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 700}, [production_type], [])

        self.assertEqual(result["route"]["name"], "Цифровая типография под ключ")
        self.assertTrue(result["route"]["is_turnkey"])

    def test_separate_material_purchase_remains_a_separate_route_process(self):
        production_type = ProductionType.objects.create(code="split-route-test", name="Раздельный маршрут")
        raw = {
            "product_type": production_type.code,
            "route": {
                "reason": "Бумагу покупаем сами и передаём типографии.",
                "processes": [
                    {"name": "Закупка материала", "details": ["Бумага Majestic"]},
                    {"name": "Цифровая типография под ключ", "details": ["Печать и отделка"]},
                ],
            },
            "costs": [],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 700}, [production_type], [])

        self.assertEqual(result["route"]["name"], "Закупка материала → Цифровая типография под ключ")
        self.assertFalse(result["route"]["is_turnkey"])

    def test_manual_logistics_is_not_labelled_as_tz_source(self):
        production_type = ProductionType.objects.create(code="source-test", name="Тест источника")
        raw = {
            "product_type": production_type.code,
            "route": {"steps": ["Универсальная типография"]},
            "costs": [{"category": "logistics", "name": "Логистика", "amount_total": 3000, "source": "Дано в ТЗ", "source_type": "supplier"}],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 1000, "logistics_unit": 3}, [production_type], [1])

        self.assertEqual(result["costs"][0]["source"], "Введено администратором в расчёте")
        self.assertEqual(result["costs"][0]["source_type"], "manager")

    def test_manual_fixed_logistics_gets_backend_recipe_without_warning(self):
        production_type = ProductionType.objects.create(code="manual-logistics", name="Ручная логистика")
        raw = {
            "product_type": production_type.code,
            "route": {"steps": ["Закупка готового изделия", "Нанесение"]},
            "costs": [{
                "category": "logistics",
                "name": "Межцеховая доставка",
                "amount_total": 1000,
                "source": "Введено администратором",
                "source_type": "manager",
                "recipe": {"method": "none", "inputs": {}},
            }],
        }

        result = _normalize_training_hypothesis(raw, {"quantity": 160}, [production_type], [])

        logistics = result["costs"][0]
        self.assertEqual(logistics["amount_total"], "1000.00")
        self.assertEqual(logistics["recipe"], {"method": "fixed", "inputs": {"fixed_amount": "1000.00"}})
        self.assertEqual(logistics["calculation_steps"], ["Фиксированная стоимость на тираж: 1000.00 ₽"])
        self.assertNotIn("нет проверяемой серверной формулы", " ".join(result["learning_warnings"]))

    def test_html_price_table_keeps_rows_and_backend_selects_exact_tier(self):
        html = """
            <div>КВАДРАТНЫЕ 70х70 см полиэфирный шелк, горячий рез</div>
            <div>Основная цена 325 ₽</div><div>от 101 шт. — 250 ₽</div>
            <table>
              <tr><th>Косынки, банданы ТРЕУГОЛЬНЫЕ</th><th>до 10 шт</th><th>11-20 шт</th><th>21-50 шт</th><th>51-100 шт</th><th>101-200 шт</th><th>201-500 шт</th><th>от 500 шт</th></tr>
              <tr><td>70х70х100 см полиэфирный шелк, горячий рез</td><td>225 ₽</td><td>215 ₽</td><td>210 ₽</td><td>200 ₽</td><td>185 ₽</td><td>165 ₽</td><td>155 ₽</td></tr>
              <tr><td>70х70х100 см армани/мокрый шелк, оверлок</td><td>380 ₽</td><td>365 ₽</td><td>360 ₽</td><td>345 ₽</td><td>325 ₽</td><td>285 ₽</td><td>275 ₽</td></tr>
              <tr><th>Цены на косынки КВАДРАТНЫЕ</th><th>до 10 шт</th><th>11-20 шт</th><th>21-50 шт</th><th>51-100 шт</th><th>101-200 шт</th><th>201-500 шт</th><th>от 500 шт</th></tr>
              <tr><td>70х70 см армани/мокрый шелк, оверлок</td><td>635 ₽</td><td>620 ₽</td><td>615 ₽</td><td>575 ₽</td><td>545 ₽</td><td>474 ₽</td><td>465 ₽</td></tr>
            </table>
        """
        parser = _VisibleTextParser()
        parser.feed(html)

        quote = _select_html_price_quote(parser.tables, {
            "line": {"name": "Платок", "quantity": 600, "requirements": {}},
            "feedback": "Найди платок армани, оверлок, 70х70х100, тираж 600 штук",
        })

        self.assertIn("СТРОКА 3: 70х70х100 см армани/мокрый шелк, оверлок", _format_html_tables(parser.tables))
        self.assertEqual(quote["row_label"], "70х70х100 см армани/мокрый шелк, оверлок")
        self.assertEqual(quote["tier"], "от 500 шт")
        self.assertEqual(quote["unit_price"], "275.00")
        self.assertEqual(quote["amount_total"], "165000.00")
        self.assertEqual(quote["confidence"], "exact")

    def test_html_price_table_refuses_ambiguous_product_row(self):
        html = """
            <table>
              <tr><th>Товар</th><th>до 100 шт</th><th>от 101 шт</th></tr>
              <tr><td>Платок армани красный</td><td>400 ₽</td><td>300 ₽</td></tr>
              <tr><td>Платок армани синий</td><td>410 ₽</td><td>310 ₽</td></tr>
            </table>
        """
        parser = _VisibleTextParser()
        parser.feed(html)

        quote = _select_html_price_quote(parser.tables, {
            "line": {"name": "Платок армани", "quantity": 600, "requirements": {}},
            "feedback": "Найди цену платка армани",
        })

        self.assertIsNone(quote)

    def test_html_price_table_respects_rowspan_and_colspan_headers(self):
        html = """
            <table>
              <tr><th rowspan="2">Товар</th><th colspan="2">Цена по тиражу</th></tr>
              <tr><th>до 100 шт</th><th>от 101 шт</th></tr>
              <tr><td>Платок 70х70 армани</td><td>400 ₽</td><td>300 ₽</td></tr>
            </table>
        """
        parser = _VisibleTextParser()
        parser.feed(html)

        quote = _select_html_price_quote(parser.tables, {
            "line": {"name": "Платок 70х70 армани", "quantity": 600, "requirements": {}},
        })

        self.assertEqual(parser.tables[0][1], ["Товар", "до 100 шт", "от 101 шт"])
        self.assertEqual(quote["unit_price"], "300.00")

    def test_backend_verified_web_quote_overrides_llm_price(self):
        production_type = ProductionType.objects.create(code="web-price", name="Цена с сайта")
        line = {"name": "Платок", "quantity": 600, "requirements": {}}
        hypothesis = {
            "product_type": production_type.code,
            "confidence": .5,
            "route": {"processes": [{"name": "Изготовление под ключ"}], "reason": "Поставщик"},
            "costs": [{
                "category": "material", "process_name": "Изготовление под ключ", "name": "Платок армани",
                "amount_total": "150000", "source": "Pro-flag", "source_type": "supplier",
                "source_url": "https://pro-flag.ru/price", "recipe": {"method": "unit_rate", "inputs": {"unit_rate": "250"}},
            }],
            "questions": ["Какова точная цена платка?"],
        }
        quote = {
            "confidence": "exact", "method": "html_table_tier",
            "row_label": "70х70х100 см армани/мокрый шелк, оверлок",
            "tier": "от 500 шт", "quantity": "600", "unit_price": "275.00", "amount_total": "165000.00",
        }

        result = apply_verified_source_quote(
            hypothesis, line, quote, "Pro-flag · платки", "https://pro-flag.ru/price"
        )

        self.assertEqual(result["costs"][0]["recipe"], {"method": "unit_rate", "inputs": {"unit_rate": "275.00"}})
        self.assertEqual(result["costs"][0]["amount_total"], "165000.00")
        self.assertEqual(result["totals"]["cost_unit"], "275.00")
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["verified_source_quote"]["tier"], "от 500 шт")

    def test_private_url_cannot_be_used_as_calculation_source(self):
        with self.assertRaisesMessage(Exception, "Локальные и служебные адреса"):
            _validate_public_url("http://127.0.0.1/price")

    @patch("tenders.views.build_training_hypothesis")
    @patch("tenders.views.extract_calculation_source")
    def test_admin_can_attach_source_before_any_cost_exists(self, extract, rebuild):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        production_type = ProductionType.objects.get(code="digital_sheet")
        current = {
            "stage": "training_dialogue",
            "product_type": production_type.code,
            "route": {"name": "Цифровая типография под ключ", "steps": ["Цифровая типография под ключ"]},
            "costs": [],
            "totals": {},
        }
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Визитки", requirements={}, current_hypothesis=current,
        )
        extract.return_value = {
            "content": "Sirio Pearl SRA3 — 380 руб./лист",
            "source_type": "image",
            "url": "https://bereg.example/paper",
        }
        rebuild.return_value = {
            **current,
            "costs": [{"name": "Бумага Sirio Pearl", "amount_total": "9500"}],
            "understood_changes": ["Добавлен раздельный маршрут"],
        }
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_add_calculation_source"), {
            "payload": json.dumps({
                "session_id": session.pk,
                "line": {"name": "Визитки с выборочным УФ-лаком", "quantity": 700},
                "supplier_name": "Берег",
                "source_url": "https://bereg.example/paper",
                "feedback": "Рассмотри закупку бумаги отдельным маршрутом.",
            })
        })

        self.assertEqual(response.status_code, 200)
        source = TenderKnowledgeSource.objects.get()
        self.assertEqual(source.structured_data["scope"], "position")
        self.assertFalse(source.is_active)
        self.assertEqual(response.json()["sources"][0]["supplier_name"], "Берег")
        feedback = rebuild.call_args.kwargs["feedback"]
        self.assertIn("Рассмотри закупку бумаги отдельным маршрутом", feedback)
        self.assertIn("Sirio Pearl", feedback)
        self.assertNotIn("cost_index", json.loads(response.wsgi_request.POST["payload"]))

    @patch("tenders.views.build_training_hypothesis")
    @patch("tenders.views.extract_calculation_source")
    def test_admin_can_attach_multiple_sources_in_one_recalculation(self, extract, rebuild):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        production_type = ProductionType.objects.get(code="digital_sheet")
        current = {
            "stage": "training_dialogue",
            "product_type": production_type.code,
            "route": {"name": "Комбинированный маршрут", "steps": ["Изготовление шнурков", "Закупка вкладышей"]},
            "costs": [],
            "totals": {},
        }
        session = ProductionTrainingSession.objects.create(
            created_by=self.user, position_name="Шнурок с вкладышем", requirements={}, current_hypothesis=current,
        )
        extract.side_effect = [
            {"content": "Изготовление шнурков 500 шт. по 80 руб.", "source_type": "link", "url": "https://lanyard.example/"},
            {"content": "Готовые вкладыши 500 шт. по 12 руб.", "source_type": "link", "url": "https://insert.example/"},
        ]
        rebuild.return_value = {**current, "sources": [], "understood_changes": ["Маршрут разделён на два процесса"]}
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_add_calculation_source"), {
            "payload": json.dumps({
                "session_id": session.pk,
                "line": {"name": "Шнурок для телефона с вкладышем", "quantity": 500},
                "feedback": "Шнурки изготавливаем на заказ, вкладыши закупаем готовыми.",
                "sources": [
                    {"supplier_name": "Шнурки", "title": "Изготовление шнурков", "source_url": "https://lanyard.example/"},
                    {"supplier_name": "Вкладыши", "title": "Готовые вкладыши", "source_url": "https://insert.example/"},
                ],
            }),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TenderKnowledgeSource.objects.count(), 2)
        self.assertEqual(len(response.json()["sources"]), 2)
        self.assertTrue(all(value["is_pending"] for value in response.json()["sources"]))
        rebuild.assert_called_once()
        feedback = rebuild.call_args.kwargs["feedback"]
        self.assertIn("ИСТОЧНИК № 1", feedback)
        self.assertIn("ИСТОЧНИК № 2", feedback)
        self.assertIn("Шнурки изготавливаем на заказ", feedback)
        self.assertIn("Готовые вкладыши", feedback)

    def test_only_relevant_knowledge_sources_are_selected_for_future_calculations(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        TenderKnowledgeSource.objects.create(
            title="Sirio Pearl и Majestic", supplier_name="Берег", source_type="link",
            url="https://bereg.example/designer-paper", content_summary="Дизайнерская перламутровая бумага Sirio Pearl SRA3 300 г/м²",
            created_by=self.user,
        )
        TenderKnowledgeSource.objects.create(
            title="Хлопковая ткань", supplier_name="Текстиль", source_type="text",
            content_summary="Ткань для пошива футболок", created_by=self.user,
        )

        sources = _knowledge_sources_for_line({
            "name": "Визитки на Sirio Pearl",
            "requirements": {"requirements": [{"label": "Бумага", "value": "перламутровая дизайнерская Sirio Pearl 300 г/м²"}]},
        })

        self.assertEqual([value["supplier"] for value in sources], ["Берег"])

    def test_lesson_row_stores_admin_words_and_learned_context(self):
        lesson = Lesson.objects.create(
            admin_text="подними мужские, женские не убирай",
            summary="приоритет мужским поло",
            item_word="поло",
            tz_labels=["материал", "плотность"],
            created_by=self.user,
        )

        self.assertEqual(lesson.scope, "catalog")
        self.assertTrue(lesson.is_active)
        self.assertEqual(lesson.outcome, {})
        self.assertEqual(str(lesson), "приоритет мужским поло")
        self.assertEqual(list(Lesson.objects.filter(scope="catalog", is_active=True)), [lesson])
