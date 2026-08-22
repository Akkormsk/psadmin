import json
import os
import re
import base64
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
import pypdfium2 as pdfium
import xlrd


MONEY = Decimal("0.01")
AI_MAX_SOURCE_CHARS = 120_000
AI_MAX_SCAN_PAGES = 12
AI_SCAN_MAX_SIDE = 1800


class TenderAIError(Exception):
    pass


def _cell_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _table_text(rows):
    result = []
    for row in rows:
        values = [_cell_text(value) for value in row]
        while values and not values[-1]:
            values.pop()
        if any(values):
            result.append(" | ".join(values))
    return "\n".join(result)


def extract_tender_source(upload):
    """Extract text/tables locally. The uploaded file is never persisted."""
    suffix = Path(upload.name).suffix.lower()
    if suffix == ".xlsx":
        workbook = load_workbook(upload, read_only=True, data_only=True)
        parts = []
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 1, 1000), max_col=min(sheet.max_column or 1, 60), values_only=True)
            parts.append(f"ЛИСТ: {sheet.title}\n{_table_text(rows)}")
        text = "\n\n".join(parts)
    elif suffix == ".xls":
        book = xlrd.open_workbook(file_contents=upload.read())
        parts = []
        for sheet in book.sheets():
            rows = (sheet.row_values(index) for index in range(min(sheet.nrows, 1000)))
            parts.append(f"ЛИСТ: {sheet.name}\n{_table_text(rows)}")
        text = "\n\n".join(parts)
    elif suffix == ".docx":
        document = Document(upload)
        parts = [_cell_text(paragraph.text) for paragraph in document.paragraphs if _cell_text(paragraph.text)]
        for index, table in enumerate(document.tables, start=1):
            parts.append(f"ТАБЛИЦА {index}\n{_table_text([[cell.text for cell in row.cells] for row in table.rows])}")
        text = "\n".join(parts)
    elif suffix == ".pdf":
        reader = PdfReader(upload)
        pages = []
        for index, page in enumerate(reader.pages[:100], start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pages.append(f"СТРАНИЦА {index}\n{page_text}")
        text = "\n\n".join(pages)
    else:
        raise TenderAIError("Поддерживаются .xlsx, .xls, .docx и текстовые .pdf.")
    text = text.strip()
    if not text and suffix == ".pdf":
        return "", False
    if not text:
        raise TenderAIError("В документе не найден текст. Сканированные PDF пока не поддерживаются.")
    return text[:AI_MAX_SOURCE_CHARS], len(text) > AI_MAX_SOURCE_CHARS


def _scan_pdf_images(upload):
    """Render scanned PDF pages in memory for multimodal recognition."""
    upload.seek(0)
    document = pdfium.PdfDocument(upload.read())
    page_count = len(document)
    if not page_count:
        raise TenderAIError("В PDF нет страниц.")
    if page_count > AI_MAX_SCAN_PAGES:
        raise TenderAIError(f"Скан содержит {page_count} страниц. Пока можно распознать не более {AI_MAX_SCAN_PAGES} страниц за один раз.")
    images = []
    for page_number in range(page_count):
        page = document[page_number]
        bitmap = page.render(scale=2)
        image = bitmap.to_pil().convert("RGB")
        image.thumbnail((AI_SCAN_MAX_SIDE, AI_SCAN_MAX_SIDE))
        output = BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        images.append(base64.b64encode(output.getvalue()).decode("ascii"))
        bitmap.close()
        page.close()
    document.close()
    upload.seek(0)
    return images


def _json_from_model(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise TenderAIError("Модель вернула ответ в неожиданном формате. Попробуйте ещё раз.") from exc


def recognize_tender_items(upload):
    api_key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    base_url = os.getenv("TIMEWEB_AI_BASE_URL", "https://api.timeweb.ai/v1").rstrip("/")
    model = os.getenv("TIMEWEB_AI_MODEL", "openai/gpt-4.1-mini").strip()
    if not api_key:
        raise TenderAIError("AI Gateway ещё не настроен.")
    source, truncated = extract_tender_source(upload)
    scan_ocr = Path(upload.name).suffix.lower() == ".pdf" and not source
    schema = '{"items":[{"name":"товар","quantity":1,"nmck_unit":100,"nmck_total":1000,"confidence":0.95}],"warnings":[]}'
    prompt = f"""Извлеки из документа позиции НМЦК для расчёта тендера.
Для каждой товарной позиции нужны: точное наименование, количество, НМЦК за единицу и итоговая НМЦК всей позиции.
Если указаны цены коммерческих предложений поставщиков (КП 1, КП 2, КП 3 или источники цены), не используй ни одну из них как НМЦК.
В таких таблицах выбирай конечную рассчитанную колонку «Средняя цена» или «Средняя рыночная цена» — это нужная НМЦК за единицу.
Колонка «НМЦК» рядом со средней ценой обычно содержит общую стоимость позиции: перенеси её в nmck_total, а не в nmck_unit.
Если в документе дана только общая сумма позиции, раздели её на количество.
Если отдельной общей суммы нет, рассчитай nmck_total как quantity * nmck_unit.
Не включай заголовки, итоги, НДС, доставку и пустые строки как товары.
Не выдумывай значения. Сомнения кратко перечисли в warnings.
Верни только JSON строго такого вида: {schema}

ДОКУМЕНТ:
{source or 'Перед тобой страницы сканированного документа в исходном порядке. Внимательно прочитай таблицу на изображениях.'}"""
    user_content = prompt
    if scan_ocr:
        user_content = [{"type": "text", "text": prompt}]
        user_content.extend({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}", "detail": "high"}} for image in _scan_pdf_images(upload))
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "Ты точно переносишь табличные данные. Отвечай только валидным JSON без markdown."},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 6000,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base_url}/chat/completions", data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:
            detail = None
        raise TenderAIError(detail or "AI Gateway отклонил запрос.") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TenderAIError("AI Gateway временно недоступен. Попробуйте ещё раз.") from exc
    try:
        result = _json_from_model(response_data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise TenderAIError("AI Gateway не вернул результат распознавания.") from exc
    items = []
    for raw in result.get("items", []):
        try:
            name = _cell_text(raw.get("name"))
            quantity = Decimal(str(raw.get("quantity", 0)).replace(",", "."))
            nmck_unit = Decimal(str(raw.get("nmck_unit", 0)).replace(",", "."))
            raw_total = raw.get("nmck_total")
            nmck_total = Decimal(str(raw_total).replace(",", ".")) if raw_total not in (None, "") else quantity * nmck_unit
            confidence = max(0, min(1, float(raw.get("confidence", 0))))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if name and quantity > 0 and nmck_unit > 0:
            source_total = _money(nmck_total) if nmck_total > 0 else _money(quantity * nmck_unit)
            calculated_total = _money(quantity * nmck_unit)
            # The source unit price is often displayed rounded to kopecks while the
            # source line total is calculated from a more precise hidden value.
            rounding_tolerance = max(Decimal("0.05"), quantity * Decimal("0.005") + Decimal("0.02"))
            items.append({
                "name": name[:500],
                "quantity": _decimal_text(quantity),
                "nmck_unit": str(_money(nmck_unit)),
                "nmck_total": str(source_total),
                "total_from_source": raw_total not in (None, "") and nmck_total > 0,
                "total_matches": abs(source_total - calculated_total) <= rounding_tolerance,
                "confidence": confidence,
            })
    if not items:
        raise TenderAIError("Не удалось уверенно найти позиции с количеством и НМЦК.")
    warnings = [str(value)[:300] for value in result.get("warnings", []) if str(value).strip()]
    if truncated:
        warnings.append("Документ был слишком большим: обработана основная часть содержимого.")
    if scan_ocr:
        warnings.insert(0, "PDF распознан по изображению. Внимательно проверьте названия, количество, цены и итоговые суммы.")
    usage = response_data.get("usage", {})
    return {"items": items, "warnings": warnings, "scan_ocr": scan_ocr, "usage": {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)}}


def _money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_tender(lines, reduction_percent, russia_delivery, vat_rate):
    coefficient = (Decimal("100") - reduction_percent) / Decimal("100")
    totals = {"nmck_total": Decimal("0"), "rrp_total": Decimal("0"), "purchase_total": Decimal("0"), "gross_profit": Decimal("0")}
    calculated_lines = []
    for line in lines:
        nmck_total = line["quantity"] * line["nmck_unit"]
        rrp_unit = line["nmck_unit"] * coefficient
        rrp_total = line["quantity"] * rrp_unit
        purchase_unit = line["material_unit"] + line["application_unit"] + line["logistics_unit"]
        purchase_total = line["quantity"] * purchase_unit
        profit = rrp_total - purchase_total
        calculated_lines.append({"nmck_total": _money(nmck_total), "rrp_unit": _money(rrp_unit), "rrp_total": _money(rrp_total), "purchase_unit": _money(purchase_unit), "purchase_total": _money(purchase_total), "profit": _money(profit)})
        totals["nmck_total"] += nmck_total
        totals["rrp_total"] += rrp_total
        totals["purchase_total"] += purchase_total
        totals["gross_profit"] += profit
    vat = totals["rrp_total"] * vat_rate / Decimal("100")
    all_expenses = totals["purchase_total"] + russia_delivery + vat
    net_profit = totals["rrp_total"] - all_expenses
    roi = net_profit / all_expenses * Decimal("100") if all_expenses else Decimal("0")
    summary = {**{key: _money(value) for key, value in totals.items()}, "vat": _money(vat), "all_expenses": _money(all_expenses), "net_profit": _money(net_profit), "roi": roi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "coefficient": coefficient.quantize(Decimal("0.0001"))}
    return calculated_lines, summary
