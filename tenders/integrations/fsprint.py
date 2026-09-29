"""Адаптер FSPrint (calc.fsprint.ru) — внешний калькулятор себестоимости.

Провайдер для StageCounterpartyLink.price_source_type =
SOURCE_EXTERNAL_CALCULATOR. FSPrint-специфичные поля формы
(main1paper_type, main1lam_film_id и т.п.) живут только здесь — наружу
отдаём FSPrintPriceResult с уже нормализованными данными; ядро расчёта и
модели Calculation про них не знают (см.
docs/claude-production-base-prompt.md §5, §31).

Расчёт цены — ДВА HTTP-запроса, не один (см. docs/backlog/fsprint-adapter.md):
POST /calculate возвращает HTML-заглушку с номером расчёта (`record`), сама
раскладка стоимости приходит вторым запросом POST /show_variant с этим
`record`. calculate() ниже делает оба запроса и отдаёт один результат —
по решению пользователя объединены, наружу это один вызов.

**Что известно на 28.09.2026, а что нет** — важно не путать:
- известна структура ЗАПРОСА для product_id="packet" (живой пример из
  DevTools, см. fsprint_sample_payload.json), список полей всех 23
  product_id (см. fsprint_product_fields.json), и формат ответа
  show_variant для product_id="packet" (fsprint_sample_show_variant_response.html);
- НЕ известны: допустимые значения полей для большинства product_id,
  ID плёнок/материалов (lam_film_id и т.п.), зависимости между полями,
  стабильна ли структура show_variant для ДРУГИХ product_id (проверено
  только на «Пакетах»).
Парсер ниже разбирает только то, что реально увидено в ответе: таблицу
сроков изготовления и построчные пары «подпись: значение» — угадывать
семантику отдельных статей себестоимости он не пытается."""

import logging
import re
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CALCULATE_URL = "https://calc.fsprint.ru/ext/calc/calculate"
SHOW_VARIANT_URL = "https://calc.fsprint.ru/ext/calc/show_variant"
DEFAULT_TIMEOUT = 20

_session_cookies: dict = {}

_RECORD_RE = re.compile(r'id="jir_nom">(\d+)<')
_TIMELINE_ROW_RE = re.compile(
    r'<td><input type="radio"[^>]*name="timeline"[^>]*></td>\s*'
    r'<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>'
)
_FIELD_ROW_RE = re.compile(r'<tr>\s*<td>([^<]+)</td>\s*<td>\s*<b>([^<]*)</b>\s*</td>\s*</tr>')


class FSPrintError(Exception):
    """Сеть/таймаут при обращении к FSPrint. Не бросается на «формат
    ответа не такой, как ожидали» — это не наша ошибка, а повод изучить
    ответ; такой случай возвращается как FSPrintPriceResult.error, не как
    исключение."""


@dataclass
class FSPrintTimelineOption:
    label: str
    markup_percent: float
    total_cost: float
    price_per_unit: float


@dataclass
class FSPrintPriceResult:
    record: str
    timeline_options: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    raw_text: str = ""
    error: str = ""


def calculate(payload: dict, *, timeout: int = DEFAULT_TIMEOUT) -> FSPrintPriceResult:
    """Полный расчёт: POST /calculate за номером расчёта, затем
    POST /show_variant за раскладкой стоимости. payload — уже готовый
    словарь полей формы FSPrint; как его строить из наших параметров —
    tenders.integrations.fsprint_mapping."""
    calculate_text = _post(CALCULATE_URL, payload, timeout=timeout)
    match = _RECORD_RE.search(calculate_text)
    if not match:
        return FSPrintPriceResult(
            record="", raw_text=calculate_text,
            error="В ответе /calculate не найден номер расчёта (id=\"jir_nom\") — формат мог измениться.",
        )
    record = match.group(1)
    variant_text = _post(SHOW_VARIANT_URL, {"record": record, "var": "0", "curr": "RUR", "usetemp": "0"}, timeout=timeout)
    return _parse_show_variant(record, variant_text)


def _post(url: str, payload: dict, *, timeout: int) -> str:
    body = urlencode(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "text/plain, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    if _session_cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in _session_cookies.items())
    request = Request(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
            _remember_cookies(response.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FSPrintError(f"FSPrint не ответил: {exc}") from exc
    logger.info("FSPrint POST %s: %d bytes", url, len(raw_text))
    return raw_text


def _remember_cookies(response_headers) -> None:
    """Сохраняет ТОЛЬКО те cookie, что сам сервер вернул в Set-Cookie —
    не чужие браузерные значения. Похоже, без сессии сайт после
    нескольких подряд анонимных расчётов начинает требовать логин
    (см. docs/backlog/fsprint-adapter.md) — эта сессия держит нас
    «одним визитом», а не N отдельными."""
    for set_cookie in response_headers.get_all("Set-Cookie") or []:
        name, _, rest = set_cookie.partition("=")
        value = rest.split(";", 1)[0]
        _session_cookies[name.strip()] = value.strip()


def _parse_show_variant(record: str, raw_text: str) -> FSPrintPriceResult:
    timeline_options = [
        FSPrintTimelineOption(
            label=label.strip(),
            markup_percent=float(markup_percent),
            total_cost=float(total_cost),
            price_per_unit=float(price_per_unit),
        )
        for label, markup_percent, total_cost, price_per_unit in _TIMELINE_ROW_RE.findall(raw_text)
    ]
    fields = [(label.strip().rstrip(":").strip(), value.strip()) for label, value in _FIELD_ROW_RE.findall(raw_text)]
    error = "" if timeline_options else "В ответе /show_variant не найдена таблица сроков изготовления — формат мог измениться."
    return FSPrintPriceResult(record=record, timeline_options=timeline_options, fields=fields, raw_text=raw_text, error=error)
