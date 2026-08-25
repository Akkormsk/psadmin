import json
from io import BytesIO
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from docx import Document
from openpyxl import Workbook

from calculator.models import PriceItem
from .models import ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, TenderEstimate, TenderKnowledgeSource, TenderSettings
from .services import _evaluate_cost_recipe, _json_from_model, _knowledge_sources_for_line, _normalize_training_hypothesis, _paper_candidates, _resolve_line_match, _shorten_structured_item_names, _strip_shared_item_boilerplate, _technical_source_chunks, _validate_public_url, analyze_production_route, analyze_tender_requirements, calculate_sheet_imposition, calculate_tender, classify_production_type, detect_tender_document_type, extract_tender_source, recognize_tender_items


class TenderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager", password="password")
        self.other = get_user_model().objects.create_user(username="other", password="password")
        self.payload = [{"name": "Ручка", "quantity": "10", "nmck_unit": "100", "material_unit": "40", "application_unit": "10", "logistics_unit": "5", "product_url": "https://example.com/item", "comment": "Синяя"}]

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
        self.assertContains(reopened, "Расчёт не завершён")

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

    @patch("tenders.views.build_training_hypothesis")
    def test_production_route_preview_uses_current_position(self, analyze):
        analyze.return_value = {"stage": "training_dialogue", "product_type": "digital_sheet", "confidence": .4, "route": {"name": "Под ключ", "steps": ["Изготовление"]}, "costs": [], "totals": {}}
        self.client.force_login(self.user)

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])

        response = self.client.post(reverse("tender_production_route_preview"), {"line_json": json.dumps({"name": "Блокнот А5", "quantity": 300, "requirements": {}})})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["product_type"], "digital_sheet")
        self.assertTrue(response.json()["session_id"])
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[0]["name"], "Блокнот А5")

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

        response = self.client.post(reverse("tender_revise_production_hypothesis"), {"payload": json.dumps(payload)})

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.current_hypothesis["route"]["name"], "Подрядчик под ключ")
        turn = ProductionTrainingTurn.objects.get(session=session)
        self.assertEqual(turn.feedback, "Считать у подрядчика под ключ")
        self.assertEqual(turn.understood_changes, ["Выбран подрядчик под ключ"])

    def test_confirmed_dialogue_becomes_training_example(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        session = ProductionTrainingSession.objects.create(
            created_by=self.user,
            position_name="Папка",
            requirements={"requirements": [{"label": "Нанесение", "value": "Тиснение"}]},
            current_hypothesis={
                "product_type": "binding_special",
                "facts": ["Тиснение"],
                "route": {"name": "Под ключ", "reason": "Специализированное изделие", "steps": ["Изготовление под ключ"]},
                "costs": [{"category": "application", "name": "Изготовление", "amount_total": "10000.00"}],
                "totals": {"cost_total": "10000.00"},
            },
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_confirm_production_type"), {"payload": json.dumps({"session_id": session.pk})})

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertTrue(session.is_confirmed)
        self.assertEqual(session.confirmed_example.routes[0]["name"], "Под ключ")

    @patch("tenders.services._ai_gateway_json")
    def test_untrained_classification_cannot_claim_high_confidence(self, gateway):
        gateway.return_value = ({"suggested_type": "digital_sheet", "confidence": .95, "reason": "Малый тираж", "features": ["100 штук"], "alternatives": [], "matched_example_ids": [], "questions": []}, {})

        result = classify_production_type({"name": "Открытка", "quantity": 100, "requirements": {}})

        self.assertEqual(result["stage"], "production_classification")
        self.assertLessEqual(result["confidence"], .45)

    @patch("tenders.services._ai_gateway_json")
    def test_classification_drops_question_already_answered_by_tz(self, gateway):
        gateway.return_value = ({
            "suggested_type": "binding_specialized",
            "confidence": .45,
            "reason": "Папка с декоративной отделкой",
            "features": ["3D конгрев герба", "горячее тиснение надписи"],
            "alternatives": [],
            "matched_example_ids": [],
            "routes": [],
            "questions": [{
                "question": "Требуется ли офсетная печать или возможно цифровая печать для нанесения текста и герба?",
                "missing_fact": "способ нанесения",
                "why_it_changes_route": "выбор технологии",
            }],
        }, {})
        line = {
            "name": "Папка «Благодарность»",
            "quantity": 1000,
            "requirements": {"requirements": [
                {"label": "Нанесение герба", "value": "Полнообъемный 3D конгрев с золотой металлизацией"},
                {"label": "Надпись", "value": "Горячее тиснение фольгой цвет золото"},
            ]},
        }

        result = classify_production_type(line)

        self.assertEqual(result["questions"], [])

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

    def test_private_url_cannot_be_used_as_calculation_source(self):
        with self.assertRaisesMessage(Exception, "Локальные и служебные адреса"):
            _validate_public_url("http://127.0.0.1/price")

    @patch("tenders.views.build_training_hypothesis")
    @patch("tenders.views.extract_calculation_source")
    def test_admin_can_attach_source_to_specific_cost(self, extract, rebuild):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        production_type = ProductionType.objects.get(code="digital_sheet")
        current = {"stage": "training_dialogue", "product_type": production_type.code, "route": {"name": "Закупка материала", "steps": ["Закупка материала"]}, "costs": [{"name": "Бумага", "amount_total": "1000"}], "totals": {}}
        session = ProductionTrainingSession.objects.create(created_by=self.user, position_name="Папка", requirements={}, current_hypothesis=current)
        extract.return_value = {"content": "Majestic SRA3 — 380 руб./лист", "source_type": "link", "url": "https://supplier.example/price"}
        rebuilt = {**current, "costs": [{"name": "Бумага", "amount_total": "9500", "calculation_steps": ["25 листов × 380 ₽"]}], "understood_changes": ["Цена бумаги пересчитана"]}
        rebuild.return_value = rebuilt
        self.client.force_login(self.user)

        response = self.client.post(reverse("tender_add_calculation_source"), {
            "payload": json.dumps({"session_id": session.pk, "line": {"name": "Папка", "quantity": 100}, "cost_index": 0, "supplier_name": "Дубль В", "source_url": "https://supplier.example/price"})
        })

        self.assertEqual(response.status_code, 200)
        source = TenderKnowledgeSource.objects.get()
        self.assertEqual(source.supplier_name, "Дубль В")
        self.assertFalse(source.is_active)
        self.assertEqual(response.json()["costs"][0]["source_id"], source.pk)
        self.assertTrue(response.json()["sources"][0]["is_pending"])
        self.assertIn("Majestic SRA3", rebuild.call_args.kwargs["feedback"])

        confirm_response = self.client.post(reverse("tender_confirm_production_type"), {
            "payload": json.dumps({"session_id": session.pk, "line": {"name": "Папка", "quantity": 100}}),
        })

        self.assertEqual(confirm_response.status_code, 200)
        source.refresh_from_db()
        self.assertTrue(source.is_active)

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

    @patch("tenders.services._ai_gateway_json")
    def test_known_embossing_and_spring_are_recovered_from_catalog(self, gateway):
        setup = PriceItem.objects.create(category="embossing", name="Приладка (количество клише)", aliases="горячее тиснение", unit_name="клише", unit_price="1000")
        hits = PriceItem.objects.create(category="embossing", name="Ударов", aliases="тиснение фольгой", unit_name="удар", unit_price="4")
        spring = PriceItem.objects.create(category="postpress", name="Пружина в бобине, 30см", aliases="металлическая пружина", unit_price="3")
        gateway.return_value = ({"selected_source": "psodin_sheet", "calculator": "sheet", "route": "internal", "confidence": .9, "reason": "Нужна проверка", "components": [{"name": "Блок", "source": "internal", "kind": "sheet", "finished_width_mm": 148, "finished_height_mm": 210, "units_per_product": 60, "material_query": "офсетная бумага", "grammage_gsm": 80, "operation_item_ids": []}], "questions": [], "warnings": []}, {})

        result = analyze_production_route({"name": "Блокнот на металлической пружине с горячим тиснением", "quantity": 300, "requirements": {}})

        embossing = [value for value in result["cost_options"] if value["group"] == "embossing"]
        spring_options = [value for value in result["cost_options"] if value["group"] == "spring"]
        self.assertTrue({setup.pk, hits.pk}.issubset({value["catalog_item_id"] for value in embossing}))
        self.assertEqual(spring_options[0]["catalog_item_id"], spring.pk)
        self.assertIn("2 шт. с 30 см", spring_options[0]["calculation"])

    @patch("tenders.services._ai_gateway_json")
    def test_outsourced_component_is_not_sent_to_internal_calculator(self, gateway):
        PriceItem.objects.create(category="postpress", name="Пружина в бобине, 30см", aliases="пружина", unit_price="3")
        gateway.return_value = ({"selected_source": "supplier_price", "route": "outsourcing", "confidence": .9, "reason": "Закупаем готовое", "components": [{"name": "Готовый товар", "source": "outsourcing", "source_reason": "Нет технологии", "kind": "material", "operation_item_ids": []}], "questions": [], "warnings": []}, {})

        result = analyze_production_route({"name": "Готовый сувенир на пружине", "quantity": 100, "requirements": {}})

        self.assertEqual(result["route"], "outsourcing")
        self.assertEqual(result["cost_options"], [])

    @patch("tenders.services._ai_gateway_json")
    def test_manager_answers_are_used_and_canon_is_a_supported_calculator(self, gateway):
        gateway.return_value = ({"selected_source": "psodin_canon", "calculator": "canon", "route": "internal", "confidence": .9, "reason": "Широкоформатная печать", "components": [], "questions": [], "warnings": []}, {})

        result = analyze_production_route({"name": "Постер A1", "quantity": 2, "manager_answers": {"Материал?": "Plain paper 80g"}, "requirements": {}})

        self.assertEqual(result["calculator"], "canon")
        self.assertIn("Plain paper 80g", gateway.call_args.args[0])

    @patch("tenders.services._ai_gateway_json")
    def test_supplier_source_cannot_accidentally_use_print_shop_catalog(self, gateway):
        PriceItem.objects.create(category="embossing", name="Приладка", aliases="тиснение", unit_price="1000")
        gateway.return_value = ({
            "selected_source": "supplier_price",
            "calculator": "sheet",
            "route": "internal",
            "confidence": .9,
            "reason": "Нужен специализированный поставщик",
            "components": [{"name": "Пакет", "source": "internal", "kind": "sheet", "operation_item_ids": []}],
            "questions": [],
            "warnings": [],
        }, {})

        result = analyze_production_route({"name": "Подарочный пакет А3 с тиснением", "quantity": 100, "requirements": {}})

        self.assertEqual(result["selected_source"], "supplier_price")
        self.assertEqual(result["calculator"], "none")
        self.assertEqual(result["cost_options"], [])
