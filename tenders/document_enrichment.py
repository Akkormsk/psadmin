"""Generic document enrichment helpers; no tender or product-specific rules."""
from __future__ import annotations
from html.parser import HTMLParser
import re
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell, self.attrs = [], None, None, None, {}
    def handle_starttag(self, tag, attrs):
        if tag == "table": self.table = []
        elif tag == "tr" and self.table is not None: self.row = []
        elif tag in {"td", "th"} and self.row is not None: self.cell, self.attrs = [], dict(attrs)
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append({"text": re.sub(r"\s+", " ", "".join(self.cell)).strip()[:1000], "rowspan": max(1, int(self.attrs.get("rowspan", 1) or 1)), "colspan": max(1, int(self.attrs.get("colspan", 1) or 1))}); self.cell = None
        elif tag == "tr" and self.row is not None: self.table.append(self.row); self.row = None
        elif tag == "table" and self.table is not None: self.tables.append(expand_table(self.table)); self.table = None
def expand_table(rows):
    """Expand row/col spans without flattening product-row relationships."""
    spans, result = {}, []
    for source in rows:
        row, column, cells = [], 0, iter(source); pending = next(cells, None)
        while pending is not None or any(position >= column for position in spans):
            if column in spans:
                text, remaining = spans[column]; row.append(text)
                if remaining == 1: del spans[column]
                else: spans[column] = (text, remaining - 1)
                column += 1; continue
            text, colspan, rowspan = pending["text"], pending["colspan"], pending["rowspan"]
            row.extend([text] * colspan)
            if rowspan > 1:
                for offset in range(colspan): spans[column + offset] = (text, rowspan - 1)
            column += colspan; pending = next(cells, None)
        if any(row): result.append(row)
    return result
def document_table_payload(html):
    parser = _TableParser(); parser.feed(html or "")
    return [{"rows": rows} for rows in parser.tables[:12] if rows]


def component_total_quantity(parent_quantity, quantity_per_parent):
    """One quantity rule for staging and accepting documented aggregate contents."""
    from decimal import Decimal, InvalidOperation

    if parent_quantity is None or quantity_per_parent is None:
        return None
    try:
        parent, per_parent = Decimal(str(parent_quantity)), Decimal(str(quantity_per_parent))
        if not parent.is_finite() or not per_parent.is_finite() or parent <= 0 or per_parent <= 0:
            raise ValueError("Количество должно быть положительным числом")
        total = parent * per_parent
        if total != total.quantize(Decimal("0.01")):
            raise ValueError("Количество требует большей точности; уточните единицу измерения")
        return total
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Не удалось определить количество компонента") from exc
