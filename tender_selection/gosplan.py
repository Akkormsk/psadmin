"""Тонкий клиент ГосПлан API (https://gosplan.info) — 44-ФЗ, эндпоинт /fz44/purchases.

Без зависимостей: только стандартная библиотека, как в tenders/services.py.
Бесплатный тест-сервер отдаёт урезанный набор данных и лимитирует ~10 запросов/мин,
поэтому клиент жёстко троттлит запросы.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE = "https://v2test.gosplan.info"
THROTTLE_SECONDS = 9.0
PAGE_SIZE = 100
RATE_LIMIT_WAIT = 60  # одна пауза-повтор при HTTP 429, потом отдаём частичный результат

_TITLE_PREFIX_RE = re.compile(r"^\s*\d{4}-\d+\*+\s*")

# purchase_type -> сегмент пути карточки извещения на zakupki.gov.ru
_EIS_NOTICE_SEGMENTS = (
    ("epNotificationEF", "ea44"),   # электронный аукцион
    ("epNotificationEOK", "ok44"),  # открытый конкурс (в т.ч. EOKD / EOKOU / EOK2020)
    ("epNotificationEZK", "zk44"),  # запрос котировок
    ("epNotificationEZP", "zp44"),  # запрос предложений
)


class GosplanError(RuntimeError):
    """Ошибка обращения к ГосПлан API (не-200, сеть, невалидный JSON)."""


class RateLimitError(GosplanError):
    """HTTP 429 — превышен лимит запросов бесплатного тарифа."""


def base_url() -> str:
    return os.getenv("GOSPLAN_API_BASE", DEFAULT_BASE).rstrip("/")


def api_key() -> str:
    return os.getenv("GOSPLAN_API_KEY", "").strip()


def clean_title(object_info: str) -> str:
    """Убрать служебный префикс вида ``2026-06422**`` из наименования объекта закупки."""
    return _TITLE_PREFIX_RE.sub("", object_info or "").strip()


def eis_url(purchase_number: str, purchase_type: str) -> str:
    """Ссылка на карточку закупки в ЕИС. API прямой ссылки не отдаёт — собираем сами."""
    number = (purchase_number or "").strip()
    if not number:
        return ""
    for prefix, segment in _EIS_NOTICE_SEGMENTS:
        if (purchase_type or "").startswith(prefix):
            return (
                f"https://zakupki.gov.ru/epz/order/notice/{segment}/view/"
                f"common-info.html?regNumber={number}"
            )
    return f"https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={number}"


def _get(path: str, params: dict):
    query = urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)
    url = f"{base_url()}{path}?{query}"
    headers = {"Accept": "application/json"}
    if api_key():
        headers["X-API-Key"] = api_key()
    try:
        with urlopen(Request(url, headers=headers, method="GET"), timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError("Сервис попросил подождать — превышен лимит запросов.") from exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise GosplanError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise GosplanError(f"Сеть недоступна: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GosplanError(f"Невалидный JSON: {body[:200]}") from exc


def fetch_page(params: dict, law: str = "fz44") -> list[dict]:
    """Одна страница GET /{law}/purchases."""
    data = _get(f"/{law}/purchases", params)
    if not isinstance(data, list):
        raise GosplanError(f"Ожидался массив, пришло: {str(data)[:200]}")
    return data


def fetch_organization(inn: str, law: str = "fz44") -> dict | None:
    """Карточка организации по ИНН из реестра нужного закона. None, если не найдено."""
    data = _get(f"/{law}/organizations", {"inn": inn, "limit": 1})
    if not isinstance(data, list) or not data:
        return None
    return data[0].get("source") or {}


def fetch_notification(purchase_number: str) -> dict:
    """Извещение о закупке: {doc_type, published_at, source}. source — как в ЕИС."""
    data = _get(f"/fz44/purchases/{purchase_number}/notification", {})
    if not isinstance(data, dict):
        raise GosplanError(f"Извещение пришло не объектом: {str(data)[:200]}")
    return data


def fetch_clarifications(purchase_number: str) -> list:
    """Разъяснения положений извещения: список {doc_type, published_at, source}."""
    data = _get(f"/fz44/purchases/{purchase_number}/clarifications", {})
    return data if isinstance(data, list) else []


def fetch_complaints(purchase_number: str) -> list:
    """Жалобы в ФАС по закупке: список {reg_number, published_at, object, docs[]}."""
    data = _get(f"/fz44/purchases/{purchase_number}/complaints", {})
    return data if isinstance(data, list) else []


def fetch_purchase(purchase_number: str) -> dict | None:
    """Карточка одной закупки (в т.ч. завершённой) — нужна ради max_price (НМЦК)."""
    data = _get(f"/fz44/purchases/{purchase_number}", {})
    return data if isinstance(data, dict) else None


def fetch_contracts(params: dict) -> list[dict]:
    """Одна страница реестра контрактов GET /fz44/contracts."""
    data = _get("/fz44/contracts", params)
    if not isinstance(data, list):
        raise GosplanError(f"Ожидался массив контрактов, пришло: {str(data)[:200]}")
    return data


def iter_contracts(
    *,
    params: dict,
    page_size: int = PAGE_SIZE,
    max_requests: int = 6,
    throttle: float = THROTTLE_SECONDS,
    on_request: Callable[[int, int], None] | None = None,
) -> Iterator[dict]:
    """Листает /fz44/contracts по ``skip`` до пустой страницы или ``max_requests``."""
    skip = 0
    for request_no in range(1, max_requests + 1):
        if request_no > 1:
            time.sleep(throttle)
        page_params = {**params, "limit": page_size, "skip": skip}
        try:
            page = fetch_contracts(page_params)
        except RateLimitError:
            time.sleep(RATE_LIMIT_WAIT)
            page = fetch_contracts(page_params)
        if on_request:
            on_request(request_no, len(page))
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        skip += page_size


def _fetch_with_retry(page_params: dict, law: str) -> list[dict]:
    """Одна страница: при 429 — одна пауза и повтор, при сетевой ошибке — короткий повтор."""
    try:
        return fetch_page(page_params, law)
    except RateLimitError:
        time.sleep(RATE_LIMIT_WAIT)
        return fetch_page(page_params, law)  # второй 429 пробросится наверх — вернём частичный результат
    except GosplanError:
        time.sleep(15)
        return fetch_page(page_params, law)


def iter_purchases(
    *,
    params: dict,
    law: str = "fz44",
    page_size: int = PAGE_SIZE,
    max_requests: int = 8,
    throttle: float = THROTTLE_SECONDS,
    on_request: Callable[[int, int], None] | None = None,
) -> Iterator[dict]:
    """Листает /fz44/purchases по ``skip`` до пустой страницы или ``max_requests``.

    Между запросами (кроме первого) спит ``throttle`` секунд, чтобы не упереться в лимит.
    При GosplanError делает один повтор через 60 секунд. ``on_request(n, got)`` вызывается
    после каждого успешного запроса — для журналирования.
    """
    skip = 0
    for request_no in range(1, max_requests + 1):
        if request_no > 1:
            time.sleep(throttle)
        page_params = {**params, "limit": page_size, "skip": skip}
        page = _fetch_with_retry(page_params, law)
        if on_request:
            on_request(request_no, len(page))
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        skip += page_size
