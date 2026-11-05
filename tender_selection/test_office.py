import io
from pathlib import Path
import subprocess
from unittest.mock import patch

from django.test import SimpleTestCase
from docx import Document
from openpyxl import Workbook

from .documents import DocumentError, extract_preview
from .office import convert_office


class OfficeConversionTests(SimpleTestCase):
    def test_conversion_uses_isolated_profile_and_removes_temporary_files(self):
        paths = []

        def run(command, **kwargs):
            source = Path(command[-1])
            paths.append(source.parent)
            self.assertEqual(source.name, "document.docx")
            self.assertIn("--headless", command)
            self.assertTrue(any(arg.startswith("-env:UserInstallation=file:") for arg in command))
            self.assertEqual(kwargs["timeout"], 60)
            (source.parent / "output" / "document.pdf").write_bytes(b"%PDF-test")

        with patch("tender_selection.office.shutil.which", return_value="/usr/bin/libreoffice"), \
                patch("tender_selection.office.subprocess.run", side_effect=run):
            self.assertEqual(convert_office(b"office", "../../name.docx", "pdf"), b"%PDF-test")
        self.assertFalse(paths[0].exists())

    def test_timeout_is_explicit_failure(self):
        with patch("tender_selection.office.shutil.which", return_value="libreoffice"), \
                patch("tender_selection.office.subprocess.run", side_effect=subprocess.TimeoutExpired("office", 60)):
            with self.assertRaises(DocumentError):
                convert_office(b"office", "name.xlsx", "pdf")

    def test_legacy_word_uses_structured_reader_after_conversion(self):
        document = Document()
        document.add_paragraph("Русский текст без искажений")
        output = io.BytesIO()
        document.save(output)
        with patch("tender_selection.office.convert_office", return_value=output.getvalue()):
            result = extract_preview(b"legacy", "source.doc")
        self.assertIn("Русский текст без искажений", result["html"])

    def test_legacy_excel_uses_structured_reader_after_conversion(self):
        workbook = Workbook()
        workbook.active.append(["Товар", 123])
        output = io.BytesIO()
        workbook.save(output)
        with patch("tender_selection.office.convert_office", return_value=output.getvalue()):
            result = extract_preview(b"legacy", "source.xls")
        self.assertIn("Товар", result["html"])
