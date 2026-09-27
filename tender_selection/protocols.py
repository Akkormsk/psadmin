"""Итоговый протокол закупки 44-ФЗ с публичных страниц zakupki.gov.ru.

ГосПлан отдаёт протоколы только на следующий день, ЕИС — через минуты после
публикации. Два запроса на закупку:
1. «Результаты определения поставщика» — 404, пока итога нет; когда есть —
   ссылка на итоговый протокол;
2. вкладка протокола «Список заявок» — все участники: номер заявки, место,
   цена, решение комиссии, причина отклонения.

Участники в протоколе обезличены (только номер заявки), поэтому «мы» находимся
по номеру нашей заявки или её сумме, которые вносит менеджер.
ЕИС сбрасывает соединения при частых запросах с одного IP — вызывающий код
обязан делать паузы между закупками (см. retry_pending_protocols).
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

EIS_ORIGIN = "https://zakupki.gov.ru"
REQUEST_TIMEOUT = 25
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

_PROTOCOL_LINK_RE = re.compile(r'href="([^"]*/protocol/protocol-main-info\.html\?[^"]*)"[^>]*>(.*?)</a>', re.S)
_SECTION_RE = re.compile(
    r'<span class="[^"]*section__title[^"]*">(.*?)</span>\s*<span class="[^"]*section__info[^"]*">(.*?)</span>', re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


class ProtocolError(RuntimeError):
    pass


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _money(value: str) -> str:
    digits = re.sub(r"[^\d,.]", "", value or "").replace(",", ".")
    try:
        return str(Decimal(digits).quantize(Decimal("0.01"))) if digits else ""
    except InvalidOperation:
        return ""


class _TableParser(HTMLParser):
    """Таблицы tableBlock: заголовки и строки как текст ячеек."""

    def __init__(self):
        super().__init__()
        self.tables: list[dict] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._in_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "table" and "tableBlock" in (dict(attrs).get("class") or ""):
            self.tables.append({"headers": [], "rows": []})
        elif not self.tables:
            return
        elif tag == "thead":
            self._in_head = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if not self.tables:
            return
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            table = self.tables[-1]
            if self._in_head:
                table["headers"] = self._row
            elif any(self._row):
                table["rows"].append(self._row)
            self._row = None
        elif tag == "thead":
            self._in_head = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _tables(page: str) -> list[list[dict]]:
    """Каждая таблица — список строк {заголовок: текст ячейки}."""
    parser = _TableParser()
    parser.feed(page)
    return [[dict(zip(t["headers"], row)) for row in t["rows"]] for t in parser.tables]


def _column(row: dict, prefix: str) -> str:
    return next((value for header, value in row.items() if header.startswith(prefix)), "")


def _sections(page: str) -> dict[str, str]:
    return {_text(title): _text(info) for title, info in _SECTION_RE.findall(page)}


_LEGACY_SEGMENT_RE = re.compile(r"/notice/([a-z]+)44/")


def supplier_results_url(eis_url: str) -> str:
    if "/common-info.html" not in (eis_url or ""):
        return ""
    # Карточки хранят старые разделы ЕИС (zk44, ea44). Итоги живут только в разделах
    # закупок по правилам с 2022 года (zk20, ea20): ea44 ЕИС ещё перенаправляет, zk44 — нет.
    return _LEGACY_SEGMENT_RE.sub(r"/notice/\g<1>20/", eis_url).replace("common-info.html", "supplier-results.html")


def bid_list_url(protocol_url: str) -> str:
    return protocol_url.replace("protocol-main-info.html", "protocol-bid-list.html")


def parse_supplier_results(page: str) -> dict | None:
    match = _PROTOCOL_LINK_RE.search(page)
    if not match:
        return None
    return {"name": _text(match.group(2)), "url": urljoin(EIS_ORIGIN, html.unescape(match.group(1)))}


def _rank(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def parse_bid_list(page: str) -> dict:
    sections = _sections(page)
    total = re.match(r"\s*(\d+)", next((v for k, v in sections.items() if k.startswith("Подано заявок")), ""))
    participants: list[dict] = []
    for rows in _tables(page):
        for row in rows:
            number = _column(row, "Идентификационный номер")
            if not number:
                continue
            reason = _column(row, "Причина отклонения")
            participants.append({
                "id": number,
                "submitted_at": _column(row, "Дата и время подачи").replace("(МСК)", "").strip(),
                "result": _column(row, "Результат рассмотрения"),
                "rank": _rank(_column(row, "Порядковый номер")),
                "price": _money(_column(row, "Предлагаемая цена")),
                "rejected": bool(reason),
                "reject_reason": reason,
            })
    return {
        "nmck": _money(next((v for k, v in sections.items() if k.startswith("Начальная (максимальная) цена")), "")),
        "applications_total": int(total.group(1)) if total else None,
        "failed_reason": next((v for k, v in sections.items() if "несостоявш" in k.lower()), ""),
        "participants": participants,
    }


def _get(url: str) -> str | None:
    """Текст страницы; None — страницы ещё нет (404)."""
    try:
        with urlopen(Request(url, headers=_HEADERS), timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise ProtocolError(f"ЕИС ответил HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise ProtocolError(f"ЕИС недоступен: {exc}") from exc


def fetch_protocol(eis_url: str) -> dict | None:
    """Итоговый протокол закупки или None, если итог ещё не опубликован."""
    results_url = supplier_results_url(eis_url)
    if not results_url:
        return None
    page = _get(results_url)
    found = parse_supplier_results(page) if page else None
    if not found:
        return None
    bids_page = _get(bid_list_url(found["url"]))
    if not bids_page:
        return None
    return {**found, **parse_bid_list(bids_page)}


def winner(protocol: dict) -> dict | None:
    return next((p for p in protocol.get("participants", []) if p.get("rank") == 1 and not p.get("rejected")), None)


def _normalized_number(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def find_ours(protocol: dict, *, bid_number: str, bid_price) -> dict | None:
    participants = protocol.get("participants", [])
    number = _normalized_number(bid_number)
    if number:
        by_number = next((p for p in participants if _normalized_number(p["id"]) == number), None)
        if by_number:
            return by_number
    if bid_price is None:
        return None
    price = str(Decimal(bid_price).quantize(Decimal("0.01")))
    return next((p for p in participants if p.get("price") == price), None)
