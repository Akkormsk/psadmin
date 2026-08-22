import json
from io import BytesIO
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from docx import Document
from openpyxl import Workbook

from .models import TenderEstimate, TenderSettings
from .services import calculate_tender, extract_tender_source


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
        response = self.client.post(reverse("tender_home"), {"tender_number": "123", "name": "Тест", "reduction_percent": "30", "russia_delivery": "100", "lines_json": json.dumps(self.payload)})
        self.assertEqual(response.status_code, 302)
        estimate = TenderEstimate.objects.get()
        self.assertEqual(estimate.owner, self.user)
        self.assertEqual(estimate.lines.count(), 1)
        self.assertEqual(estimate.vat_rate_snapshot, Decimal("5.00"))

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

    def test_invalid_post_keeps_entered_tender_and_lines(self):
        invalid = {**self.payload[0], "material_unit": "", "application_unit": "", "logistics_unit": ""}
        self.client.force_login(self.user)
        response = self.client.post(reverse("tender_home"), {"tender_number": "ABC-999", "name": "Не терять", "reduction_percent": "30", "russia_delivery": "", "lines_json": json.dumps([invalid])})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ABC-999")
        self.assertContains(response, "Не терять")
        self.assertContains(response, "Ручка")
        self.assertFalse(TenderEstimate.objects.exists())

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

    def test_scanned_pdf_is_detected_for_ocr_fallback(self):
        content = BytesIO(b"%PDF-1.4")
        content.name = "scan.pdf"
        with patch("tenders.services.PdfReader") as reader:
            reader.return_value.pages = [type("Page", (), {"extract_text": lambda self: ""})()]
            text, truncated = extract_tender_source(content)
        self.assertEqual(text, "")
        self.assertFalse(truncated)
