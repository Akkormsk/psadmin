"""Адаптер FSPrint (calc.fsprint.ru) — внешний калькулятор себестоимости.

Провайдер для StageCounterpartyLink.price_source_type =
SOURCE_EXTERNAL_CALCULATOR. FSPrint-специфичные поля формы
(main1paper_type, main1lam_film_id и т.п.) живут только здесь — наружу
отдаём FSPrintResult с уже нормализованными данными; ядро расчёта и
модели Calculation про них не знают (см.
docs/claude-production-base-prompt.md §5, §31).

**Что известно на 28.09.2026, а что нет** — важно не путать:
- известна структура ЗАПРОСА ровно для одного продукта
  (product_id="packet", живой пример из DevTools, см.
  fsprint_sample_payload.json) — количество полей, их имена;
- НЕ известны: список product_id, допустимые значения paper_type,
  ID плёнок/материалов (lam_film_id и т.п.), зависимости между полями,
  формат ответа для ЛЮБОГО продукта, в т.ч. packet.
Поэтому `_parse_response` ничего не вытаскивает из ответа, кроме факта
«это JSON или нет» — вытаскивать конкретные поля (итоговую цену и т.п.)
можно только после того, как реальный ответ будет изучен глазами."""

import json
import logging
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CALCULATE_URL = "https://calc.fsprint.ru/ext/calc/calculate"
DEFAULT_TIMEOUT = 20


class FSPrintError(Exception):
    """Сеть/таймаут при обращении к FSPrint. Не бросается на «формат
    ответа не такой, как ожидали» — это не наша ошибка, а повод изучить
    ответ; такой случай возвращается как FSPrintResult.error, не как
    исключение."""


@dataclass
class FSPrintResult:
    raw_text: str
    raw_json: object = None
    error: str = ""


def calculate(payload: dict, *, timeout: int = DEFAULT_TIMEOUT) -> FSPrintResult:
    """Минимальный HTTP-клиент: form-urlencoded POST, без cookies и без
    browser-only заголовков (sec-ch-ua/sec-fetch-*/user-agent и т.п. —
    см. docs/backlog/fsprint-adapter.md, почему их сознательно нет).
    payload — уже готовый словарь полей формы FSPrint; как его строить
    из наших параметров — tenders.integrations.fsprint_mapping."""
    body = urlencode(payload).encode("utf-8")
    request = Request(
        CALCULATE_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "text/plain, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FSPrintError(f"FSPrint не ответил: {exc}") from exc
    logger.info("FSPrint calculate: %d bytes, product_id=%s", len(raw_text), payload.get("product_id"))
    return _parse_response(raw_text)


def _parse_response(raw_text: str) -> FSPrintResult:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return FSPrintResult(raw_text=raw_text, error="Ответ не JSON — формат ещё не изучен.")
    return FSPrintResult(raw_text=raw_text, raw_json=parsed)
