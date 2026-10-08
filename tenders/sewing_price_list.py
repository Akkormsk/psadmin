from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import xlrd


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_data_from_rows(rows: list[list[Any]]) -> dict[str, Any]:
    exchange_rate = ""
    exchange_rate_with_markup = ""
    header_index = None
    for index, row in enumerate(rows):
        if len(row) > 5 and _text(row[4]) == "Курс $":
            exchange_rate = _decimal_text(row[5])
        if len(row) > 5 and _text(row[4]) == "+%":
            exchange_rate_with_markup = _decimal_text(row[5])
        if [_text(value) for value in row[:6]] == [
            "Изделие", "Крой", "Ткань", "Тираж (мин)", "Комментарии", "Стоимость"
        ]:
            header_index = index
            break

    if header_index is None:
        raise ValueError("Не найдена строка заголовков прайс-листа пошива.")

    variants: dict[str, dict[str, str | int]] = {}
    product = ""
    for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        values = list(row) + [""] * max(0, 6 - len(row))
        product = _text(values[0]) or product
        cut, fabric, minimum, comment, cost = (_text(value) for value in values[1:6])
        if not product or not cut or not fabric or not cost:
            continue
        price = _decimal_text(cost)
        if not price:
            continue
        variants[" | ".join((product, cut, fabric))] = {
            "product": product,
            "cut": cut,
            "fabric": fabric,
            "minimum_quantity": _decimal_text(minimum),
            "comment": comment,
            "unit_price": price,
            "source_row": row_index,
        }

    if not variants:
        raise ValueError("В прайс-листе пошива нет распознаваемых ценовых строк.")

    return {
        "source_kind": "sewing_xls",
        "requires_confirmation": True,
        "formula_status": "unresolved",
        "formula_note": "XLS содержит только кэшированные результаты формул; валюта и формула требуют подтверждения владельцем.",
        "input_schema": [
            {
                "key": "variant",
                "label": "Модель пошива",
                "required": True,
                "type": "select",
                "options": sorted(variants),
            }
        ],
        "pricing": {
            "currency": None,
            "exchange_rate": exchange_rate,
            "exchange_rate_with_markup": exchange_rate_with_markup,
            "variants": variants,
        },
    }


def parse_sewing_workbook(path: str | Path) -> dict[str, Any]:
    workbook = xlrd.open_workbook(str(path), formatting_info=False)
    sheet = workbook.sheet_by_index(0)
    rows = [sheet.row_values(row_index) for row_index in range(sheet.nrows)]
    return canonical_data_from_rows(rows)


def parse_sewing_workbook_bytes(raw_content: bytes) -> dict[str, Any]:
    workbook = xlrd.open_workbook(file_contents=raw_content, formatting_info=False)
    sheet = workbook.sheet_by_index(0)
    return canonical_data_from_rows([sheet.row_values(index) for index in range(sheet.nrows)])
