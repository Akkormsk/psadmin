"""Предпросмотр документов извещения: качаем файл с zakupki.gov.ru у себя на сервере
и вытаскиваем текст/таблицы. Требует, чтобы сервер доставал zakupki.gov.ru (прод, РФ).
"""
from __future__ import annotations

import io
import re
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.utils.html import escape

ALLOWED_PREFIX = "https://zakupki.gov.ru/"
MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 300
MAX_PDF_PAGES = 40
MAX_PARAGRAPHS = 1200


class DocumentError(RuntimeError):
    pass


def fetch_document(url: str) -> bytes:
    if not url.startswith(ALLOWED_PREFIX):
        raise DocumentError("Ссылка не с портала ЕИС.")
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
    })
    try:
        with urlopen(request, timeout=40) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise DocumentError(f"Портал ЕИС ответил HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise DocumentError("Не удалось скачать файл с портала ЕИС (таймаут или недоступен). Откройте оригинал по ссылке.") from exc
    if len(data) > MAX_BYTES:
        raise DocumentError("Файл слишком большой для предпросмотра.")
    return data


def _ext(filename: str) -> str:
    return (re.sub(r"\.zip$", "", (filename or "").lower())).rsplit(".", 1)[-1]


def _docx_html(data: bytes) -> str:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(io.BytesIO(data))
    out, count = [], 0
    for child in doc.element.body.iterchildren():
        if count > MAX_PARAGRAPHS:
            out.append("<p>…</p>")
            break
        if child.tag == qn("w:p"):
            text = "".join(node.text or "" for node in child.iter(qn("w:t"))).strip()
            if text:
                out.append(f"<p>{escape(text)}</p>")
                count += 1
        elif child.tag == qn("w:tbl"):
            rows = []
            for tr in child.iter(qn("w:tr")):
                cells = []
                for tc in tr.iter(qn("w:tc")):
                    cell_text = "".join(node.text or "" for node in tc.iter(qn("w:t"))).strip()
                    cells.append(f"<td>{escape(cell_text)}</td>")
                if cells:
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                count += 1
            if rows:
                out.append('<table class="ts-doc-table">' + "".join(rows) + "</table>")
    return "\n".join(out) or "<p class='ts-sub'>Документ без текста.</p>"


def _xlsx_html(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for sheet in wb.worksheets:
        out.append(f"<h4>{escape(sheet.title)}</h4>")
        rows = []
        for i, row in enumerate(sheet.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                rows.append(f"<tr><td>… ещё строки</td></tr>")
                break
            values = ["" if v is None else str(v) for v in row]
            if not any(values):
                continue
            rows.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in values) + "</tr>")
        out.append('<table class="ts-doc-table">' + "".join(rows) + "</table>" if rows else "<p class='ts-sub'>Лист пуст.</p>")
    wb.close()
    return "\n".join(out)


def _pdf_html(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        text = (page.extract_text() or "").strip()
        if text:
            parts.append("<p>" + "<br>".join(escape(line) for line in text.splitlines() if line.strip()) + "</p>")
    if len(reader.pages) > MAX_PDF_PAGES:
        parts.append("<p>…</p>")
    return "\n".join(parts) or "<p class='ts-sub'>В PDF нет извлекаемого текста (возможно, скан).</p>"


def extract_preview(data: bytes, filename: str) -> dict:
    """Возвращает {'kind': ..., 'html': ...} или {'kind': ..., 'error': ...}."""
    name = filename or ""
    is_zip = data[:2] == b"PK"

    # .zip-обёртка: достаём единственный вложенный файл
    if is_zip and (name.lower().endswith(".zip") or _ext(name) not in ("docx", "xlsx")):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                office = [n for n in names if n.lower().rsplit(".", 1)[-1] in ("docx", "xlsx", "doc", "pdf")]
                if len(office) == 1:
                    return extract_preview(zf.read(office[0]), office[0])
                if names:
                    listing = "".join(f"<li>{escape(n)}</li>" for n in names[:50])
                    return {"kind": "zip", "html": f"<p>Архив, файлы внутри:</p><ul>{listing}</ul>"}
        except zipfile.BadZipFile:
            pass

    ext = _ext(name)
    try:
        if ext == "docx" or (is_zip and b"word/document.xml" in data[:4000] + data[-4000:]):
            return {"kind": "docx", "html": _docx_html(data)}
        if ext == "xlsx":
            return {"kind": "xlsx", "html": _xlsx_html(data)}
        if ext == "pdf" or data[:4] == b"%PDF":
            return {"kind": "pdf", "html": _pdf_html(data)}
        if ext == "doc":
            return {"kind": "doc", "error": "Старый формат .doc — предпросмотр недоступен, скачайте файл."}
    except Exception as exc:  # noqa: BLE001 — любой сбой парсинга -> мягкая ошибка
        return {"kind": ext or "?", "error": f"Не удалось разобрать файл: {exc}"}
    return {"kind": ext or "?", "error": "Формат не поддерживается для предпросмотра."}
