import base64
import io
import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from django.db import transaction
from django.db.models import Q
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import CatalogCategory, CatalogProduct, CatalogSupplier, CatalogSyncRun


logger = logging.getLogger(__name__)


CATALOG_SOURCE_CAPABILITIES = (
    {
        "source": "oasis",
        "mode": "live_api",
        "fields": ["category", "name", "full_name", "description", "attributes", "materials", "colors", "branding", "price", "stock"],
    },
    {
        "source": "gifts",
        "mode": "database",
        "fields": ["category", "name", "description", "attributes", "materials", "colors", "price", "stock"],
    },
)


def catalog_source_capabilities():
    return [dict(value) for value in CATALOG_SOURCE_CAPABILITIES]


class CatalogSyncError(Exception):
    pass


class OasisClient:
    def __init__(self, api_key=None, base_url=None, timeout=45, min_interval=1.05, max_attempts=4):
        self.api_key = (api_key or os.getenv("OASIS_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("OASIS_API_BASE_URL", "https://api.oasiscatalog.com")).rstrip("/")
        self.timeout = timeout
        self.min_interval = max(0, float(min_interval))
        self.max_attempts = max(1, int(max_attempts))
        self._last_request_at = 0.0
        if not self.api_key:
            raise CatalogSyncError("OASIS_API_KEY не настроен.")

    def get(self, path, params=None):
        query = urlencode(params or {}, doseq=True)
        url = f"{self.base_url}{path}{'?' + query if query else ''}"
        token = base64.b64encode(f"{self.api_key}:".encode("utf-8")).decode("ascii")
        request = Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json", "User-Agent": "PSAdmin catalog sync/1.0"})
        last_error = None
        for attempt in range(self.max_attempts):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise CatalogSyncError("Oasis отклонил API-ключ.") from exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise CatalogSyncError(f"Oasis API вернул HTTP {exc.code}.") from exc
                last_error = exc
            except (URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            finally:
                self._last_request_at = time.monotonic()
            if attempt + 1 < self.max_attempts:
                time.sleep(min(8, 2 ** attempt))
        raise CatalogSyncError("Oasis API не ответил после нескольких повторов запроса.") from last_error

    def pages(self, path, params=None, limit=500):
        offset = 0
        while True:
            payload = self.get(path, {**(params or {}), "format": "json", "limit": limit, "offset": offset})
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise CatalogSyncError("Oasis API вернул неожиданный формат списка.")
            if not items:
                return
            yield items
            if len(items) < limit:
                return
            offset += len(items)


class GiftsXmlClient:
    def __init__(self, username=None, password=None, base_url=None, timeout=120):
        self.username = (username or os.getenv("GIFTS_XML_USERNAME", "")).strip()
        self.password = password or os.getenv("GIFTS_XML_PASSWORD", "")
        self.base_url = (base_url or os.getenv("GIFTS_XML_BASE_URL", "https://api2.gifts.ru/export/v2")).rstrip("/")
        self.timeout = timeout
        if not self.username or not self.password:
            raise CatalogSyncError("GIFTS_XML_USERNAME и GIFTS_XML_PASSWORD не настроены.")

    def open(self, path):
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        request = Request(f"{self.base_url}/{path.lstrip('/')}", headers={"Authorization": f"Basic {token}", "User-Agent": "PSAdmin gifts XML sync/1.0"})
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise CatalogSyncError("gifts.ru отклонил XML-доступ или IP не зарегистрирован.") from exc
            raise CatalogSyncError(f"gifts.ru вернул HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise CatalogSyncError("gifts.ru XML недоступен.") from exc


def _gifts_text(node, name):
    value = node.attrib.get(name)
    if value:
        return _text(value, 5000)
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name:
            return _text(child.text, 5000)
    value = node.find(name)
    return _text(value.text if value is not None else "", 5000)


def _gifts_child(node, name):
    return next((child for child in node if child.tag.rsplit("}", 1)[-1] == name), None)


def _gifts_descendants(node, name):
    return (child for child in node.iter() if child is not node and child.tag.rsplit("}", 1)[-1].lower() == name)


def _gifts_image_src(node):
    image_names = {"small_image", "super_big_image", "image", "picture", "photo"}
    for child in node.iter():
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if local_name not in image_names and "image" not in local_name and "photo" not in local_name:
            continue
        value = next((child.attrib.get(key) for key in ("src", "url", "href", "path", "file", "value") if child.attrib.get(key)), None) or child.text
        value = _text(value, 1000)
        if value:
            return value
    raw_xml = ElementTree.tostring(node, encoding="unicode")
    match = re.search(r"(?:https?:)?//files\.gifts\.ru/[^\"'<\s]+", raw_xml)
    return match.group(0) if match else ""


def _gifts_colors(node):
    names = {"color", "colour", "colors", "color_name", "colour_name", "product_color", "product_colour", "цвет"}
    values = []
    candidates = [node]
    candidates.extend(_gifts_descendants(node, "color"))
    candidates.extend(_gifts_descendants(node, "colour"))
    candidates.extend(_gifts_descendants(node, "colors"))
    candidates.extend(_gifts_descendants(node, "color_name"))
    candidates.extend(_gifts_descendants(node, "colour_name"))
    candidates.extend(_gifts_descendants(node, "product_color"))
    candidates.extend(_gifts_descendants(node, "product_colour"))
    candidates.extend(_gifts_descendants(node, "цвет"))
    for child in candidates:
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if child is node or local_name not in names:
            continue
        value = child.attrib.get("name") or child.attrib.get("value") or child.text
        value = _text(value, 200)
        if value and value not in values:
            values.append(value)
    for group in node.iter():
        group_name = _text(group.attrib.get("name") or group.attrib.get("title"), 200).lower()
        if "цвет" not in group_name and "color" not in group_name and "colour" not in group_name:
            continue
        for value_node in group.iter():
            if value_node is group:
                continue
            value = value_node.attrib.get("name") or value_node.attrib.get("value") or value_node.text
            value = _text(value, 200)
            if value and value not in values and value_node.tag.rsplit("}", 1)[-1].lower() in {"value", "item", "option", "color", "colour"}:
                values.append(value)
    return values[:20]


_GIFTS_NAME_COLORS = (
    "белый", "белая", "белое", "белые", "черный", "черная", "черное", "черные",
    "серый", "серая", "серое", "серые", "красный", "красная", "красное", "красные",
    "синий", "синяя", "синее", "синие", "голубой", "голубая", "голубое", "голубые",
    "зеленый", "зеленая", "зеленое", "зеленые", "желтый", "желтая", "желтое", "желтые",
    "фиолетовый", "фиолетовая", "фиолетовое", "фиолетовые", "оранжевый", "оранжевая",
    "розовый", "розовая", "розовое", "розовые", "коричневый", "коричневая", "коричневое",
    "бежевый", "бежевая", "бежевое", "хаки", "лайм", "мятный", "мятная", "мятное",
    "ярко-зеленый", "ярко-зеленая", "ярко-зеленое", "ярко-зеленые",
    "бордовый", "бордовая", "бордовое", "бирюзовый", "бирюзовая", "золотой", "золотая",
    "серебристый", "серебристая", "мультиколор", "разноцветный", "разноцветная",
)


def _gifts_name_colors(name):
    normalized = _normalized(name)
    result = []
    for color in _GIFTS_NAME_COLORS:
        normalized_color = _normalized(color)
        if normalized_color and re.search(rf"(?<!\w){re.escape(normalized_color)}(?!\w)", normalized) and color not in result:
            result.append(color)
    return result


def _gifts_filter_colors(filters_xml):
    result = {}
    if filters_xml is None:
        return result
    for _, filtertype in ElementTree.iterparse(filters_xml, events=("end",)):
        if filtertype.tag.rsplit("}", 1)[-1].lower() != "filtertype":
            continue
        filtertype_id = _gifts_text(filtertype, "filtertypeid")
        if filtertype_id != "21":
            filtertype.clear()
            continue
        for value in filtertype.iter():
            if value.tag.rsplit("}", 1)[-1].lower() != "filter":
                continue
            filter_id = _gifts_text(value, "filterid")
            filter_name = _gifts_text(value, "filtername")
            if filter_id and filter_name:
                result[filter_id] = filter_name
        filtertype.clear()
    return result


def _gifts_image_url(image_src):
    image_src = _text(image_src, 1000)
    if not image_src:
        return ""
    if image_src.startswith("//"):
        return f"https:{image_src}"
    if image_src.startswith("http"):
        return image_src
    relative = image_src.lstrip("/")
    if not relative.startswith(("reviewer/", "size/", "download/")):
        relative = f"reviewer/{relative}"
    return f"https://files.gifts.ru/{relative}"


def parse_gifts_catalog(product_xml, tree_xml, stock_xml=None, category=None, limit=None, filters_xml=None):
    category = _normalized(category) if category else ""
    filter_colors = _gifts_filter_colors(filters_xml)
    category_ids = {}
    if category:
        for _, page in ElementTree.iterparse(tree_xml, events=("end",)):
            if page.tag.rsplit("}", 1)[-1] != "page":
                continue
            page_name = _gifts_text(page, "name")
            if category in _normalized(page_name):
                for product in page.iter():
                    if product.tag.rsplit("}", 1)[-1] != "product":
                        continue
                    product_id = product.attrib.get("product") or _gifts_text(product, "product") or _text(product.text, 500)
                    if product_id:
                        category_ids[str(product_id)] = page_name
            page.clear()

    stocks = {}
    if stock_xml is not None:
        for _, stock in ElementTree.iterparse(stock_xml, events=("end",)):
            if stock.tag.rsplit("}", 1)[-1] != "stock":
                continue
            product_id = stock.attrib.get("product_id") or _gifts_text(stock, "product_id")
            if product_id:
                stocks[str(product_id)] = {
                    "free": _gifts_text(stock, "free"),
                    "dealerprice": _gifts_text(stock, "dealerprice"),
                    "inwayfree": _gifts_text(stock, "inwayfree"),
                }
            stock.clear()

    result = []
    for _, product in ElementTree.iterparse(product_xml, events=("end",)):
        if product.tag.rsplit("}", 1)[-1] != "product":
            continue
        product_id = product.attrib.get("product_id") or _gifts_text(product, "product_id")
        if not product_id or (category and str(product_id) not in category_ids):
            product.clear()
            continue
        stock = stocks.get(str(product_id))
        name = _gifts_text(product, "name") or str(product_id)
        article = _gifts_text(product, "code")
        material = _gifts_text(product, "matherial")
        size = _text(_gifts_text(product, "product_size"), 100)
        brand = _gifts_text(product, "brand")
        description = _gifts_text(product, "content")
        colors = _gifts_colors(product)
        product_filters = _gifts_descendants(product, "filter")
        for product_filter in product_filters:
            filter_type = _gifts_text(product_filter, "filtertypeid")
            filter_id = _gifts_text(product_filter, "filterid")
            if filter_type == "21" and filter_id in filter_colors and filter_colors[filter_id] not in colors:
                colors.append(filter_colors[filter_id])
        name_colors = _gifts_name_colors(name)
        image_src = _gifts_image_src(product)
        image_url = _gifts_image_url(image_src)
        price_group = _gifts_child(product, "price")
        price_node = _gifts_child(price_group, "price") if price_group is not None else None
        price = _decimal(price_node.text if price_node is not None else None)
        stock_free = _integer(stock.get("free")) if stock is not None else 0
        dealer_price = _decimal(stock.get("dealerprice")) if stock is not None else None
        category_name = category_ids.get(str(product_id), "")
        search_text = _normalized(" ".join(filter(None, [name, article, material, size, brand, category_name, description, *colors, *name_colors])))[:20_000]
        result.append({
            "external_id": _text(str(product_id), 100), "article": article, "name": name, "full_name": name,
            "description": description, "category_ids": [], "category_names": [category_name] if category_name else [],
            "brand": brand, "size": size, "materials": [material] if material else [], "colors": colors, "attributes": [],
            "branding": [], "package": [], "price": price, "discount_price": dealer_price, "total_stock": stock_free,
            "stock_moscow": stock_free, "stock_remote": 0, "stock_transit": _integer(stock.get("inwayfree")) if stock is not None else 0,
            "is_on_order": _gifts_text(product, "ondemand").lower() == "true", "delivery_days": _integer(_gifts_text(product, "days")) or None,
            "image_url": image_url, "product_url": f"https://gifts.ru/id/{product_id}" if product_id else "https://gifts.ru",
            "supply_terms": _gifts_text(product, "demandtype"), "warning": _gifts_text(product, "alert"), "defect": "",
            "search_text": search_text, "source_updated_at": None, "sync_marker": "", "is_active": True,
            "raw_data": {"status": _gifts_text(product, "status"), "name_colors": name_colors},
        })
        product.clear()
        if limit and len(result) >= limit:
            break
    return result


def sync_gifts_catalog(client=None, category=None, limit=None):
    client = client or GiftsXmlClient()
    supplier, _ = CatalogSupplier.objects.get_or_create(code="gifts", defaults={"name": "gifts.ru", "base_url": client.base_url})
    supplier.base_url = client.base_url
    supplier.sync_status = "running"
    supplier.sync_message = "Получаю XML gifts.ru"
    supplier.save(update_fields=["base_url", "sync_status", "sync_message"])
    run = CatalogSyncRun.objects.create(supplier=supplier)
    try:
        if limit:
            with client.open("catalogue/product.xml") as product_xml, client.open("catalogue/filters.xml") as filters_xml:
                rows = parse_gifts_catalog(product_xml, io.BytesIO(b"<root />"), category=category, limit=limit, filters_xml=filters_xml)
        else:
            with client.open("catalogue/product.xml") as product_xml, client.open("catalogue/tree.xml") as tree_xml, client.open("catalogue/stock.xml") as stock_xml, client.open("catalogue/filters.xml") as filters_xml:
                rows = parse_gifts_catalog(product_xml, tree_xml, stock_xml, category=category, filters_xml=filters_xml)
        marker = str(uuid.uuid4())
        created_count = updated_count = 0
        batch_size = 500
        for offset in range(0, len(rows), batch_size):
            batch = [CatalogProduct(supplier=supplier, **{**row, "sync_marker": marker}) for row in rows[offset:offset + batch_size]]
            external_ids = [value.external_id for value in batch]
            existing = set(CatalogProduct.objects.filter(supplier=supplier, external_id__in=external_ids).values_list("external_id", flat=True))
            if batch:
                CatalogProduct.objects.bulk_create(batch, update_conflicts=True, unique_fields=["supplier", "external_id"], update_fields=PRODUCT_UPDATE_FIELDS)
            created_count += len(batch) - len(existing)
            updated_count += len(existing)
            run.received_count = offset + len(batch)
            run.created_count = created_count
            run.updated_count = updated_count
            run.save(update_fields=["received_count", "created_count", "updated_count"])
        now = timezone.now()
        supplier.last_synced_at = now
        supplier.sync_status = "success"
        supplier.sync_message = f"Товаров: {len(rows)}; новых: {created_count}; обновлено: {updated_count}"
        supplier.save(update_fields=["last_synced_at", "sync_status", "sync_message"])
        run.status = "success"
        run.finished_at = now
        run.received_count = len(rows)
        run.created_count = created_count
        run.updated_count = updated_count
        run.save(update_fields=["status", "finished_at", "received_count", "created_count", "updated_count"])
        run.imported_rows = rows
        return run
    except Exception as exc:
        now = timezone.now()
        supplier.sync_status = "failed"
        supplier.sync_message = _text(exc, 500)
        supplier.save(update_fields=["sync_status", "sync_message"])
        run.status = "failed"
        run.finished_at = now
        run.error = _text(exc, 5000)
        run.save(update_fields=["status", "finished_at", "error"])
        raise


def _text(value, limit=1000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _integer(value, default=0):
    try:
        return int(Decimal(str(value or default).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _decimal(value):
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _boolean(value):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float, Decimal)):
        return value != 0
    return _text(value, 20).lower() in {"1", "true", "yes", "y", "да"}


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _color_names(value):
    result = []
    for item in _list(value):
        name = _text(item.get("name") if isinstance(item, dict) else item, 100)
        if name and name not in result:
            result.append(name)
    return result


def _image_url(value):
    for item in _list(value):
        if isinstance(item, str) and item.startswith("http"):
            return item[:1000]
        if isinstance(item, dict):
            for key in ("small", "thumbnail", "big", "superbig"):
                candidate = _text(item.get(key), 1000)
                if candidate.startswith("http"):
                    return candidate
    return ""


def _parsed_datetime(value):
    parsed = parse_datetime(_text(value, 100)) if value else None
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _product_from_payload(supplier, raw, category_map, marker):
    external_id = _text(raw.get("id"), 100)
    if not external_id:
        return None
    category_ids = [str(value) for value in _list(raw.get("categories") or raw.get("categories_array"))]
    category_names = [category_map[value] for value in category_ids if value in category_map]
    attributes = [item for item in _list(raw.get("attributes")) if isinstance(item, dict)][:100]
    materials = [_text(value, 150) for value in _list(raw.get("materials")) if _text(value, 150)][:30]
    colors = _color_names(raw.get("colors"))[:30]
    branding = [_text(value, 150) for value in _list(raw.get("branding")) if _text(value, 150)][:30]
    name = _text(raw.get("name") or raw.get("full_name"), 500)
    full_name = _text(raw.get("full_name") or name, 1000)
    attribute_text = " ".join(f"{_text(item.get('name'), 150)} {_text(item.get('value'), 300)}" for item in attributes)
    search_text = " ".join([name, full_name, _text(raw.get("description"), 3000), _text(raw.get("brand"), 200), *category_names, *materials, *colors, *branding, attribute_text])
    search_text = re.sub(r"\s+", " ", search_text.lower().replace("ё", "е")).strip()[:20_000]
    return CatalogProduct(
        supplier=supplier,
        external_id=external_id,
        article=_text(raw.get("article"), 120),
        article_base=_text(raw.get("article_base"), 120),
        group_id=_text(raw.get("group_id"), 120),
        color_group_id=_text(raw.get("color_group_id"), 120),
        name=name or external_id,
        full_name=full_name,
        description=_text(raw.get("description"), 5000),
        category_ids=category_ids[:50],
        category_names=category_names[:50],
        brand=_text(raw.get("brand"), 200),
        size=_text(raw.get("size"), 100),
        materials=materials,
        colors=colors,
        attributes=attributes,
        branding=branding,
        package=_list(raw.get("package"))[:20],
        price=_decimal(raw.get("price") or raw.get("old_price")),
        discount_price=_decimal(raw.get("discount_price") or raw.get("dealerPrice")),
        total_stock=max(0, _integer(raw.get("total_stock"))),
        stock_moscow=max(0, _integer(raw.get("stock_msk"))),
        is_on_order=bool(_integer(raw.get("is_on_order"))),
        delivery_days=max(0, _integer(raw.get("delivery_days"))) if raw.get("delivery_days") not in (None, "") else None,
        image_url=_image_url(raw.get("images")),
        product_url=_text(raw.get("url"), 1000) or f"https://www.oasiscatalog.com/item/{external_id}",
        supply_terms=_text(raw.get("supply_terms"), 1000),
        warning=_text(raw.get("lead"), 1000),
        defect=_text(raw.get("defect"), 1000),
        search_text=search_text,
        source_updated_at=_parsed_datetime(raw.get("updated_at")),
        sync_marker=marker,
        is_active=not _boolean(raw.get("is_deleted")) and not _boolean(raw.get("is_stopped")),
        raw_data={
            "discount_group_id": raw.get("discount_group_id"),
            "included_branding": raw.get("included_branding"),
        },
    )


PRODUCT_UPDATE_FIELDS = [
    "article", "article_base", "group_id", "color_group_id", "name", "full_name", "description",
    "category_ids", "category_names", "brand", "size", "materials", "colors", "attributes", "branding",
    "package", "price", "discount_price", "total_stock", "stock_moscow", "is_on_order", "delivery_days",
    "image_url", "product_url", "supply_terms", "warning", "defect", "search_text", "source_updated_at",
    "sync_marker", "is_active", "raw_data", "synced_at",
]


def _sync_categories(client, supplier):
    payload = client.get("/v4/categories", {"format": "json"})
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise CatalogSyncError("Oasis вернул неожиданный формат категорий.")
    objects = []
    for raw in items:
        if not isinstance(raw, dict) or raw.get("id") in (None, ""):
            continue
        objects.append(CatalogCategory(
            supplier=supplier,
            external_id=str(raw["id"]),
            parent_external_id=_text(raw.get("parent_id"), 100),
            name=_text(raw.get("name"), 300),
            path=_text(raw.get("path") or raw.get("name"), 1000),
            is_active=True,
        ))
    if objects:
        CatalogCategory.objects.bulk_create(objects, update_conflicts=True, unique_fields=["supplier", "external_id"], update_fields=["parent_external_id", "name", "path", "is_active"])
    return {value.external_id: value.path or value.name for value in CatalogCategory.objects.filter(supplier=supplier, is_active=True)}


def _apply_stock_page(supplier, rows):
    articles = {_text(row.get("article"), 120) for row in rows if isinstance(row, dict) and row.get("article")}
    products = {value.article: value for value in CatalogProduct.objects.filter(supplier=supplier, article__in=articles)}
    changed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        product = products.get(_text(row.get("article"), 120))
        if not product:
            continue
        product.stock_moscow = max(0, _integer(row.get("stock")))
        product.stock_remote = max(0, _integer(row.get("stock-remote")))
        product.stock_transit = max(0, _integer(row.get("stock-transit")))
        product.total_stock = product.stock_moscow + product.stock_remote
        product.price = _decimal(row.get("price")) or product.price
        product.discount_price = _decimal(row.get("price-discount")) or product.discount_price
        changed.append(product)
    if changed:
        CatalogProduct.objects.bulk_update(changed, ["stock_moscow", "stock_remote", "stock_transit", "total_stock", "price", "discount_price"], batch_size=500)


def sync_oasis_catalog(client=None):
    client = client or OasisClient()
    supplier, _ = CatalogSupplier.objects.get_or_create(code="oasis", defaults={"name": "Oasis", "base_url": client.base_url})
    supplier.base_url = client.base_url
    supplier.sync_status = "running"
    supplier.sync_message = "Получаю категории и товары"
    supplier.save(update_fields=["base_url", "sync_status", "sync_message"])
    run = CatalogSyncRun.objects.create(supplier=supplier)
    marker = str(uuid.uuid4())
    received = created = updated = 0
    try:
        category_map = _sync_categories(client, supplier)
        for page in client.pages("/v4/products", {"showDeleted": 1, "includeGroupId": 1, "extend": "discount_price,total_stock,outlets"}):
            objects = [value for value in (_product_from_payload(supplier, raw, category_map, marker) for raw in page if isinstance(raw, dict)) if value]
            external_ids = [value.external_id for value in objects]
            existing = set(CatalogProduct.objects.filter(supplier=supplier, external_id__in=external_ids).values_list("external_id", flat=True))
            with transaction.atomic():
                CatalogProduct.objects.bulk_create(objects, update_conflicts=True, unique_fields=["supplier", "external_id"], update_fields=PRODUCT_UPDATE_FIELDS)
            received += len(objects)
            updated += len(existing)
            created += len(objects) - len(existing)
        for page in client.pages("/v4/stock", {"allProducts": 1}):
            _apply_stock_page(supplier, page)
        deactivated = CatalogProduct.objects.filter(supplier=supplier).exclude(sync_marker=marker).update(is_active=False)
        now = timezone.now()
        supplier.last_synced_at = now
        supplier.sync_status = "success"
        supplier.sync_message = f"Товаров: {received}; новых: {created}; обновлено: {updated}"
        supplier.save(update_fields=["last_synced_at", "sync_status", "sync_message"])
        run.status = "success"
        run.finished_at = now
        run.received_count = received
        run.created_count = created
        run.updated_count = updated
        run.deactivated_count = deactivated
        run.save(update_fields=["status", "finished_at", "received_count", "created_count", "updated_count", "deactivated_count"])
        return run
    except Exception as exc:
        now = timezone.now()
        supplier.sync_status = "failed"
        supplier.sync_message = _text(exc, 500)
        supplier.save(update_fields=["sync_status", "sync_message"])
        run.status = "failed"
        run.finished_at = now
        run.error = _text(exc, 5000)
        run.received_count = received
        run.created_count = created
        run.updated_count = updated
        run.save(update_fields=["status", "finished_at", "error", "received_count", "created_count", "updated_count"])
        raise


BRANDING_ALIASES = {
    "вышивка": ("вышив",),
    "dtf": ("dtf", "дтф"),
    "термотрансфер": ("термотрансфер", "термоперенос"),
    "шелкография": ("шелкограф", "трафаретная печать"),
    "тампопечать": ("тампопечат",),
    "уф-печать": ("уф-печат", "ультрафиолетовая печать", "uv-печат"),
    "гравировка": ("гравиров",),
    "тиснение": ("тиснен",),
    "сублимация": ("сублимац",),
    "деколь": ("декол",),
}


def _normalized(value):
    return re.sub(r"[^a-zа-я0-9%²≥≤]+", " ", _text(value, 20_000).lower().replace("ё", "е")).strip()


def _requirement_values(line):
    requirements = line.get("requirements") if isinstance(line, dict) else {}
    if isinstance(requirements, dict):
        requirements = requirements.get("requirements", [])
    return [value for value in requirements if isinstance(value, dict)] if isinstance(requirements, list) else []


def _constraint_text(line, label_marker):
    return " ".join(_text(value.get("value"), 1000) for value in _requirement_values(line) if label_marker in _normalized(value.get("label")))


def _meaningful_tokens(value):
    ignored = {"цвет", "материал", "состав", "изделие", "товар", "требуется", "должен", "должна", "менее", "более", "процентов"}
    return {token for token in _normalized(value).split() if len(token) >= 3 and token not in ignored and not token.isdigit()}


def _entity_phrase_matches(required, offered):
    required_tokens = _normalized(required).split()
    offered_tokens = _normalized(offered).split()
    if not required_tokens or not offered_tokens:
        return False

    def compatible(left, right):
        if left == right:
            return True
        if min(len(left), len(right)) < 5:
            return False
        prefix_length = max(4, min(6, len(left) - 1, len(right) - 1))
        return left[:prefix_length] == right[:prefix_length]

    return all(any(compatible(required_token, offered_token) for offered_token in offered_tokens) for required_token in required_tokens)


COLOR_FAMILIES = {
    "lime": ("лайм", "лаймов", "салатов", "ярко зелен", "зеленое яблоко", "яблочно зелен", "кислотно зелен"),
    "navy": ("темно син", "темно син", "navy"),
    "sky": ("голуб", "небесно син"),
    "turquoise": ("бирюз", "аквамарин"),
    "burgundy": ("бордов", "бургунди", "марсала"),
    "scarlet": ("алый", "ярко крас"),
    "orange": ("оранж", "апельсин"),
    "violet": ("фиолет", "пурпур"),
    "pink": ("розов", "фуксия"),
    "beige": ("бежев", "песочн", "слоновая кость"),
    "gray": ("серый", "серебрист", "графит", "меланж"),
    "white": ("белый", "молочн"),
    "black": ("черный", "антрацит"),
    "yellow": ("желтый", "лимонн"),
}
COLOR_PARENTS = {
    "lime": "green", "navy": "blue", "sky": "blue", "turquoise": "blue",
    "burgundy": "red", "scarlet": "red", "orange": "orange", "violet": "violet",
    "pink": "pink", "beige": "beige", "gray": "gray", "white": "white",
    "black": "black", "yellow": "yellow",
}
BASE_COLOR_MARKERS = {
    "green": ("зелен",), "blue": ("син",), "red": ("красн",),
    "orange": ("оранж",), "violet": ("фиолет",), "pink": ("розов",),
    "beige": ("бежев",), "gray": ("сер", "графит", "меланж"),
    "white": ("бел",), "black": ("черн",), "yellow": ("желт",),
}


def _color_family(value):
    """Map commercial shade names to a stable family without flattening shades."""
    normalized = _normalized(value)
    for family, aliases in COLOR_FAMILIES.items():
        if any(_normalized(alias) in normalized for alias in aliases):
            return family
    for family, aliases in BASE_COLOR_MARKERS.items():
        if any(alias in normalized for alias in aliases):
            return family
    return ""


def _colors_compatible(required, offered):
    required_family = _color_family(required)
    offered_family = _color_family(offered)
    if not required_family or not offered_family:
        return bool(_meaningful_tokens(required) & _meaningful_tokens(offered)), ""
    if required_family == offered_family:
        return True, required_family
    # A generic requirement such as "green" accepts its named shades, but a
    # precise requirement such as "lime" must not silently accept any green.
    if COLOR_PARENTS.get(offered_family) == required_family:
        return True, offered_family
    return False, ""


def _density_constraint(line):
    text = _normalized(_constraint_text(line, "плотност"))
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:г|гр)?\s*(?:м2|м²)", text)
    if not match:
        return None
    value = Decimal(match.group(1).replace(",", "."))
    if "не менее" in text or "от " in f"{text} " or "≥" in text:
        return {"kind": "min", "value": value}
    if "не более" in text or "до " in f"{text} " or "≤" in text:
        return {"kind": "max", "value": value}
    return {"kind": "exact", "value": value}


def _product_density(product):
    values = []
    for attribute in product.attributes if isinstance(product.attributes, list) else []:
        if not isinstance(attribute, dict) or "плотност" not in _normalized(attribute.get("name")):
            continue
        match = re.search(r"(\d+(?:[.,]\d+)?)", _text(attribute.get("value"), 300))
        if match:
            try:
                values.append(Decimal(match.group(1).replace(",", ".")))
            except InvalidOperation:
                pass
    return max(values) if values else None


def _requested_branding(line):
    text = " ".join(
        _text(value.get("value"), 1000)
        for value in _requirement_values(line)
        if any(marker in _normalized(value.get("label")) for marker in ("нанес", "печат", "логотип", "вышив"))
    )
    normalized = _normalized(text)
    return [name for name, aliases in BRANDING_ALIASES.items() if any(alias in normalized for alias in aliases)]


def _planner_categories(intent):
    if not isinstance(intent, dict):
        return []
    values = []
    has_planned_categories = isinstance(intent.get("categories"), list) and intent.get("categories")
    keys = ("categories",) if has_planned_categories else ("item", "product_class")
    for key in keys:
        raw = intent.get(key)
        raw = raw if isinstance(raw, list) else [raw]
        for value in raw:
            value = _text(value, 200)
            if value and _normalized(value) not in {_normalized(item) for item in values}:
                values.append(value)
    return values[:12]


def _planner_requirements(intent):
    if not isinstance(intent, dict):
        return []
    result = []
    defaults = (("required", 1), ("preferred", .6), ("secondary", .3))
    for key, default_weight in defaults:
        values = intent.get(key)
        if not isinstance(values, list):
            continue
        for value in values[:20]:
            if not isinstance(value, dict):
                continue
            label, item_value = _text(value.get("label"), 300), _text(value.get("value"), 1000)
            if not label or not item_value:
                continue
            try:
                weight = float(value.get("weight", default_weight))
            except (TypeError, ValueError):
                weight = default_weight
            if weight > 1:
                weight /= 100
            result.append({"label": label, "value": item_value, "weight": max(0, min(1, weight)), "group": key})
    return result


def _line_with_planner_requirements(line, intent):
    existing = _requirement_values(line)
    requirements = list(existing)
    seen = {(_normalized(value.get("label")), _normalized(value.get("value"))) for value in existing}
    for value in _planner_requirements(intent):
        key = (_normalized(value["label"]), _normalized(value["value"]))
        if key in seen:
            continue
        requirements.append({"label": value["label"], "value": value["value"]})
        seen.add(key)
    if len(requirements) == len(existing):
        return line
    result = dict(line)
    result["requirements"] = {"requirements": requirements}
    return result


def _criterion_key(value):
    normalized = _normalized(value)
    groups = (
        ("price", ("цена", "стоимост", "бюджет")),
        ("name", ("назван", "наименован", "модель")),
        ("product_type", ("тип товара", "категор", "вид изделия")),
        ("material", ("материал", "состав", "сырье")),
        ("color", ("цвет", "оттен")),
        ("density", ("плотност",)),
        ("branding", ("нанес", "вышив", "гравиров", "печать", "логотип")),
        ("stock", ("остаток", "налич", "тираж", "количеств", "склад")),
        ("gender", ("gender", "пол", "гендер", "мужск", "женск", "унисекс")),
        ("source", ("source", "источник", "поставщик")),
    )
    return next((key for key, markers in groups if any(marker in normalized for marker in markers)), normalized)


def _planner_weight_map(intent):
    result = {}
    for value in intent.get("ranking", []) if isinstance(intent, dict) and isinstance(intent.get("ranking"), list) else []:
        if not isinstance(value, dict):
            continue
        criterion = _criterion_key(value.get("criterion"))
        try:
            weight = float(value.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if weight > 1:
            weight /= 100
        if criterion:
            result[criterion] = max(0, min(1, weight))
    for value in _planner_requirements(intent):
        label = _criterion_key(value["label"])
        if label:
            result[label] = value["weight"]
    for value in intent.get("constraints", []) if isinstance(intent, dict) and isinstance(intent.get("constraints"), list) else []:
        if not isinstance(value, dict):
            continue
        label = _criterion_key(value.get("field"))
        try:
            weight = float(value.get("weight", 1))
        except (TypeError, ValueError):
            weight = 1
        if label:
            result[label] = max(0, min(1, weight))
    return result


def _ranking_weight(intent, *markers):
    if not isinstance(intent, dict) or not isinstance(intent.get("ranking"), list):
        return 0
    normalized_markers = tuple(_normalized(value) for value in markers if _normalized(value))
    weights = []
    for value in intent["ranking"]:
        if not isinstance(value, dict):
            continue
        criterion = _normalized(value.get("criterion"))
        if not criterion or not any(marker in criterion for marker in normalized_markers):
            continue
        try:
            weight = float(value.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if weight > 1:
            weight /= 100
        weights.append(max(0, min(1, weight)))
    return max(weights, default=0)


def _required_mismatches(mismatches, intent):
    required_labels = {
        _criterion_key(value["label"])
        for value in _planner_requirements(intent)
        if value.get("group") == "required" and _normalized(value.get("label"))
    }
    return [
        mismatch for mismatch in mismatches
        if _criterion_key(mismatch) in required_labels
        or any(label in _normalized(mismatch) for label in required_labels)
    ]


def _planner_source_terms(intent, source_code):
    if not isinstance(intent, dict) or not isinstance(intent.get("source_strategy"), list):
        return []
    result = []
    for strategy in intent["source_strategy"]:
        if not isinstance(strategy, dict) or _normalized(strategy.get("source")) != _normalized(source_code):
            continue
        for key in ("category_terms", "query_terms"):
            for value in strategy.get(key, []) if isinstance(strategy.get(key), list) else []:
                value = _text(value, 300)
                if value and _normalized(value) not in {_normalized(item) for item in result}:
                    result.append(value)
    return result[:16]


def _live_category_data(client):
    cached = cache.get("oasis-live-categories-v1")
    if isinstance(cached, list) and cached:
        return cached
    payload = client.get("/v4/categories", {"format": "json"})
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise CatalogSyncError("Oasis вернул неожиданный формат категорий.")
    result = [{
        "id": str(item.get("id")),
        "parent_id": str(item.get("parent_id") or ""),
        "name": _text(item.get("name"), 300),
        "path": _text(item.get("path") or item.get("name"), 1000),
    } for item in items if isinstance(item, dict) and item.get("id") not in (None, "")]
    cache.set("oasis-live-categories-v1", result, 6 * 60 * 60)
    return result


def _live_category_for_intent(categories, intent, line):
    planner_categories = _planner_categories(intent)
    aliases = planner_categories or [_text(line.get("name", ""), 300)]
    if isinstance(intent, dict):
        aliases.extend(_text(value, 80) for value in intent.get("synonyms", [])[:8] if _text(value, 80))
    terms = {_normalized(value) for value in aliases if _normalized(value)}
    ranked = []
    for category in categories:
        name = _normalized(category["name"])
        path = _normalized(category["path"])
        matches = [
            term for term in terms
            if term == name or term in name or name in term or SequenceMatcher(None, term, name).ratio() >= .72
        ]
        if not matches:
            continue
        exact = any(term == name for term in matches)
        depth = category["path"].count("/")
        score = (100 if exact else 60) + max(len(value) for value in matches) - depth * 2
        ranked.append((score, -depth, category))
    return max(ranked, default=(0, 0, None), key=lambda value: (value[0], value[1]))[2]


def _attribute_values(product, markers):
    values = []
    for attribute in product.attributes if isinstance(product.attributes, list) else []:
        if not isinstance(attribute, dict):
            continue
        name = _normalized(attribute.get("name"))
        if any(marker in name for marker in markers):
            value = _text(attribute.get("value"), 500)
            if value:
                values.append(value)
    return values


def _values_compatible(required, offered):
    """Compare a requirement with a catalogue value without relying on exact inflection."""
    required_text = _text(required, 1000)
    offered_text = _text(offered, 1000)
    if _color_family(required_text) and _color_family(offered_text):
        return _colors_compatible(required_text, offered_text)[0]
    required_tokens = _meaningful_tokens(required_text)
    offered_tokens = _meaningful_tokens(offered_text)
    if required_tokens & offered_tokens:
        return True
    return any(
        len(required_token) >= 4 and len(offered_token) >= 4
        and (required_token.startswith(offered_token[:4]) or offered_token.startswith(required_token[:4]))
        for required_token in required_tokens
        for offered_token in offered_tokens
    )


def _catalog_parameter_weight(value, planner_weights=None):
    normalized = _normalized(value)
    criterion = _criterion_key(value)
    if criterion in (planner_weights or {}):
        return max(1, round(100 * planner_weights[criterion]))
    for label, weight in (planner_weights or {}).items():
        if label and (label in normalized or normalized.startswith(label)):
            return max(1, round(100 * weight))
    if "тип товара" in normalized:
        return 100
    if "материал" in normalized or "состав" in normalized:
        return 40
    if "цвет" in normalized:
        return 40
    if "плотност" in normalized:
        return 30
    if "нанес" in normalized or "гравиров" in normalized or "печать" in normalized:
        return 25
    if "остаток" in normalized:
        return 15
    return 35


def _catalog_parameter_score(matches, mismatches, unknown, planner_weights=None):
    return (
        sum(_catalog_parameter_weight(value, planner_weights) for value in matches)
        - sum(_catalog_parameter_weight(value, planner_weights) for value in mismatches)
        - sum(max(8, _catalog_parameter_weight(value, planner_weights) // 3) for value in unknown)
    )


def _product_sizes(product):
    values = []
    if _text(product.size, 100):
        values.append(_text(product.size, 100))
    for attribute in product.attributes if isinstance(product.attributes, list) else []:
        if not isinstance(attribute, dict):
            continue
        name = _normalized(attribute.get("name"))
        if name not in {"размер", "российский размер"}:
            continue
        value = _text(attribute.get("value"), 100)
        if value and value not in values:
            values.append(value)
    return values


def _aggregate_color_variants(products):
    """Represent one colour family as one offer instead of one card per size SKU."""
    grouped = {}
    for product in products:
        grouped.setdefault(product.color_group_id or product.external_id, []).append(product)
    result = []
    for family_id, variants in grouped.items():
        representative = next((value for value in variants if value.external_id == family_id), variants[0])
        variant_ids = [value.external_id for value in variants]
        prices = [value.effective_price for value in variants if value.effective_price is not None]
        sizes = []
        for value in variants:
            for size in _product_sizes(value):
                if size not in sizes:
                    sizes.append(size)
        representative.external_id = family_id
        representative.total_stock = sum(max(0, value.total_stock) for value in variants)
        representative.is_on_order = any(value.is_on_order for value in variants)
        representative.product_url = f"https://www.oasiscatalog.com/item/{family_id}"
        if prices:
            # Use the highest variant price to avoid silently understating a
            # mixed-size tender when a supplier prices sizes differently.
            representative.discount_price = max(prices)
        representative.raw_data = {
            **(representative.raw_data if isinstance(representative.raw_data, dict) else {}),
            "variant_ids": variant_ids,
            "sizes": sizes,
        }
        result.append(representative)
    return result


CONSTRAINT_FIELD_LABELS = {
    "gender": "Пол", "material": "Материал", "color": "Цвет", "density": "Плотность",
    "branding": "Нанесение", "stock": "Остаток", "price": "Цена", "name": "Название",
    "product_type": "Тип товара", "source": "Поставщик",
}


def _canonical_gender(value):
    normalized = _normalized(value)
    if any(marker in normalized for marker in ("унисекс", "unisex")):
        return "unisex"
    if any(marker in normalized for marker in ("женск", "female", "women", "woman")):
        return "female"
    if any(marker in normalized for marker in ("мужск", "male", "men", "man")):
        return "male"
    return ""


def _constraint_product_values(product, field):
    field = _criterion_key(field)
    attributes = product.attributes if isinstance(product.attributes, list) else []
    related = [
        _text(value.get("value"), 1000)
        for value in attributes if isinstance(value, dict) and _criterion_key(value.get("name")) == field
        and _text(value.get("value"), 1000)
    ]
    if field == "gender":
        explicit = [value for value in (_canonical_gender(item) for item in related) if value]
        if explicit:
            return list(dict.fromkeys(explicit))
        inferred = _canonical_gender(" ".join([product.name, product.full_name]))
        return [inferred] if inferred else []
    if field == "material":
        return product.materials if isinstance(product.materials, list) and product.materials else related
    if field == "color":
        return product.colors if isinstance(product.colors, list) and product.colors else related
    if field == "branding":
        return product.branding if isinstance(product.branding, list) and product.branding else related
    if field == "density":
        value = _product_density(product)
        return [value] if value is not None else []
    if field == "stock":
        return [product.total_stock]
    if field == "price":
        return [product.effective_price] if product.effective_price is not None else []
    if field == "name":
        return [product.full_name or product.name] if product.full_name or product.name else []
    if field == "product_type":
        return [*product.category_names, product.full_name or product.name]
    if field == "source":
        return [product.supplier.code, product.supplier.name]
    return related


def _constraint_expected_values(field, values):
    if field == "gender":
        return [value for value in (_canonical_gender(item) for item in values) if value]
    return values


def _constraint_number(value):
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    match = re.search(r"-?\d+(?:[.,]\d+)?", _text(value, 300).replace(" ", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", "."))
    except InvalidOperation:
        return None


def _constraint_values_match(field, expected, offered):
    if field == "gender":
        return _canonical_gender(expected) == _canonical_gender(offered)
    return _values_compatible(expected, offered)


def _evaluate_structured_constraints(product, intent):
    matches, mismatches, unknown, hard_mismatches = [], [], [], []
    constraints = intent.get("constraints", []) if isinstance(intent, dict) and isinstance(intent.get("constraints"), list) else []
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        field = _criterion_key(constraint.get("field"))
        operator = _normalized(constraint.get("operator")).replace(" ", "_")
        expected = _constraint_expected_values(field, constraint.get("values", []) if isinstance(constraint.get("values"), list) else [])
        offered = _constraint_product_values(product, field)
        label = CONSTRAINT_FIELD_LABELS.get(field, _text(constraint.get("field"), 80).rstrip(":") or "Характеристика")
        level = _normalized(constraint.get("level"))
        missing_policy = _normalized(constraint.get("missing_policy")).replace(" ", "_")
        if not offered:
            message = f"{label} не указан в каталоге"
            if missing_policy == "reject":
                mismatches.append(message)
                if level == "required":
                    hard_mismatches.append(message)
            elif missing_policy == "allow_with_penalty":
                unknown.append(message)
            continue

        valid = False
        if operator == "exists":
            valid = True
        elif operator in {"in", "contains"}:
            valid = any(_constraint_values_match(field, wanted, actual) for wanted in expected for actual in offered)
        elif operator in {"not_in", "not_contains"}:
            valid = not any(_constraint_values_match(field, wanted, actual) for wanted in expected for actual in offered)
        elif operator in {"lte", "gte", "between"}:
            actual_numbers = [value for value in (_constraint_number(item) for item in offered) if value is not None]
            expected_numbers = [value for value in (_constraint_number(item) for item in expected) if value is not None]
            if actual_numbers and expected_numbers:
                if operator == "lte":
                    valid = any(actual <= expected_numbers[0] for actual in actual_numbers)
                elif operator == "gte":
                    valid = any(actual >= expected_numbers[0] for actual in actual_numbers)
                elif len(expected_numbers) >= 2:
                    low, high = sorted(expected_numbers[:2])
                    valid = any(low <= actual <= high for actual in actual_numbers)

        offered_text = ", ".join(str(value) for value in offered)
        if valid:
            matches.append(f"{label}: {offered_text}")
        else:
            verb = "запрещённое значение" if operator in {"not_in", "not_contains"} else "не соответствует правилу"
            message = f"{label}: {verb} ({offered_text})"
            mismatches.append(message)
            if level == "required":
                hard_mismatches.append(message)
    return (
        list(dict.fromkeys(matches)),
        list(dict.fromkeys(mismatches)),
        list(dict.fromkeys(unknown)),
        list(dict.fromkeys(hard_mismatches)),
    )


def _fit_product(product, line, anchors, quantity, intent=None):
    matches, mismatches, unknown = [], [], []
    type_text = _normalized(" ".join([
        *(product.category_names if isinstance(product.category_names, list) else []),
        product.name,
        product.full_name,
    ]))
    anchor_hit = next((value for value in anchors if _entity_phrase_matches(value, type_text)), "")
    if anchor_hit:
        matches.append(f"Тип товара: {anchor_hit}")
    else:
        mismatches.append("Не совпадает тип товара")

    material_text = _constraint_text(line, "материал") or _constraint_text(line, "состав")
    material_tokens = _meaningful_tokens(material_text)
    material_values = product.materials if isinstance(product.materials, list) and product.materials else _attribute_values(product, ("материал", "состав"))
    product_materials = _meaningful_tokens(" ".join(material_values))
    if material_tokens:
        if material_tokens & product_materials:
            matches.append(f"Материал: {', '.join(sorted(material_tokens & product_materials))}")
        elif product_materials:
            mismatches.append(f"Материал не совпадает: требуется {material_text}; в каталоге {', '.join(material_values)}")
        else:
            unknown.append("Материал не указан в каталоге")

    color_text = _constraint_text(line, "цвет")
    color_values = product.colors if isinstance(product.colors, list) and product.colors else _attribute_values(product, ("цвет",))
    product_color_text = " ".join(color_values)
    name_color_values = []
    if isinstance(product.raw_data, dict):
        name_color_values = [str(value) for value in product.raw_data.get("name_colors", []) if str(value).strip()]
    if not name_color_values:
        name_color_values = _gifts_name_colors(product.full_name or product.name)
    name_color_match = False
    if _meaningful_tokens(color_text):
        color_matches, color_family = _colors_compatible(color_text, product_color_text)
        if color_matches:
            family_note = f" (семейство: {color_family})" if color_family else ""
            matches.append(f"Цвет: {', '.join(color_values)}{family_note}")
        else:
            name_color_match, _ = _colors_compatible(color_text, " ".join(name_color_values))
            required_family = _color_family(color_text)
            offered_family = _color_family(product_color_text)
            if name_color_match and required_family and offered_family and COLOR_PARENTS.get(required_family) == offered_family:
                matches.append(f"Цвет: {', '.join(color_values)} (оттенок в названии: {', '.join(name_color_values)})")
            elif _meaningful_tokens(product_color_text):
                mismatches.append(f"Цвет не совпадает: требуется {color_text}; в каталоге {', '.join(color_values)}")
            else:
                unknown.append("Цвет не указан в каталоге")

    density = _density_constraint(line)
    product_density = _product_density(product)
    if density:
        if product_density is None:
            unknown.append("Плотность не указана в каталоге")
        else:
            valid = (
                density["kind"] == "min" and product_density >= density["value"]
                or density["kind"] == "max" and product_density <= density["value"]
                or density["kind"] == "exact" and product_density == density["value"]
            )
            if valid:
                matches.append(f"Плотность: {_text(product_density)} г/м²")
            else:
                sign = {"min": "не менее", "max": "не более", "exact": "ровно"}[density["kind"]]
                mismatches.append(f"Плотность {_text(product_density)} г/м²; требуется {sign} {_text(density['value'])} г/м²")

    requested_branding = _requested_branding(line)
    branding_values = product.branding if isinstance(product.branding, list) and product.branding else _attribute_values(product, ("нанес", "брендирован"))
    product_branding = _normalized(" ".join(branding_values))
    for method in requested_branding:
        if any(alias in product_branding for alias in BRANDING_ALIASES[method]):
            matches.append(f"Поддерживается нанесение: {method}")
        elif product_branding:
            mismatches.append(f"Не заявлено нужное нанесение: {method}")
        else:
            unknown.append(f"Не указана совместимость с нанесением: {method}")

    handled_markers = ("материал", "состав", "цвет", "плотност", "нанес", "печат", "логотип", "вышив", "остаток", "наличие", "тираж")
    product_attributes = [
        attribute for attribute in product.attributes
        if isinstance(attribute, dict) and _text(attribute.get("name"), 300) and _text(attribute.get("value"), 1000)
    ] if isinstance(product.attributes, list) else []
    for requirement in _requirement_values(line):
        label = _text(requirement.get("label"), 300)
        value = _text(requirement.get("value"), 1000)
        label_normalized = _normalized(label)
        if not label_normalized or not value or any(marker in label_normalized for marker in handled_markers):
            continue
        if any(marker in label_normalized for marker in ("коммент", "примеч")):
            continue
        label_tokens = _meaningful_tokens(label_normalized)
        related = [
            attribute for attribute in product_attributes
            if label_tokens and any(token in _normalized(attribute.get("name")) for token in label_tokens)
        ]
        display_label = label.rstrip(":")
        if not related:
            unknown.append(f"{display_label} не указан в каталоге")
            continue
        offered_values = [_text(attribute.get("value"), 1000) for attribute in related]
        if any(_values_compatible(value, offered) for offered in offered_values):
            matches.append(f"{display_label}: {', '.join(offered_values)}")
        else:
            mismatches.append(f"{display_label} не совпадает: требуется {value}; в каталоге {', '.join(offered_values)}")

    if quantity > 0:
        if product.total_stock >= quantity:
            matches.append(f"Остаток достаточен: {product.total_stock} шт.")
        elif product.is_on_order:
            mismatches.append(f"На складе {product.total_stock} из {quantity} шт.; товар доступен только под заказ")
        else:
            mismatches.append(f"Недостаточный остаток: {product.total_stock} из {quantity} шт.")

    score = _catalog_parameter_score(matches, mismatches, unknown, _planner_weight_map(intent))
    if name_color_match:
        score += 8
    return score, matches, mismatches, unknown


def catalog_candidates_for_line(line, limit=3, supplier_code="oasis", intent=None, client=None, include_diagnostics=False):
    """Return a relevance-ranked shortlist from live Oasis and cached suppliers."""
    try:
        quantity = int(Decimal(str(line.get("quantity") or 0).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        quantity = 0
    categories, category, category_map = [], None, {}
    fields = (
        "id,article,name,full_name,description,article_base,group_id,color_group_id,size,images,colors,"
        "categories,categories_array,brand,attributes,materials,branding,package,price,discount_price,"
        "total_stock,is_on_order,delivery_days,supply_terms,lead,defect,updated_at"
    )
    rows, seen_ids = [], set()
    planner_categories = _planner_categories(intent)
    aliases = tuple(planner_categories) or (_text(line.get("name", ""), 300),)
    search_aliases = tuple(dict.fromkeys([*aliases, *((intent or {}).get("synonyms", []) if isinstance(intent, dict) else [])]))
    anchors = search_aliases
    effective_line = _line_with_planner_requirements(line, intent)
    text_terms = list(aliases)
    for value in [
        line.get("name", ""),
        *((intent or {}).get("synonyms", []) if isinstance(intent, dict) else []),
        *((intent or {}).get("hard_constraints", []) if isinstance(intent, dict) else []),
        *((intent or {}).get("preferences", []) if isinstance(intent, dict) else []),
        *[item.get("value", "") for item in _requirement_values(effective_line)],
        *[item.get("value", "") for item in _planner_requirements(intent)],
        *[
            term
            for query in (intent or {}).get("fallback_queries", []) if isinstance(query, dict)
            for term in query.get("terms", []) if isinstance(query.get("terms"), list)
        ],
    ]:
        text_terms.extend(sorted(_meaningful_tokens(value)))
    text_terms = list(dict.fromkeys(value for value in text_terms if _text(value, 100)))
    pool = []
    source_status = {
        "oasis": {"status": "not_searched", "message": "", "received": 0},
        "gifts": {"status": "not_searched", "message": "", "received": 0},
    }

    # Oasis is optional: an API/category failure must not suppress Gifts.
    try:
        client = client or OasisClient()
        categories = _live_category_data(client)
        category = _live_category_for_intent(categories, intent or {}, line)
        category_map = {value["id"]: value["path"] or value["name"] for value in categories}
        # Use the category when available. Otherwise ask Oasis for a bounded
        # full-text page; final matching is still performed by _fit_product.
        search_terms = list(dict.fromkeys(_planner_source_terms(intent, "oasis") + list(search_aliases)))
        search_terms = search_terms[:8]
        for offset in range(0, 1000, 500):
            params = {
                "format": "json", "limit": 500, "offset": offset,
                "available": 1, "includeGroupId": 1, "fields": fields,
            }
            if category:
                params["category"] = category["id"]
            elif search_terms:
                params["search"] = " ".join(search_terms)
            payload = client.get("/v4/products", params)
            page = payload.get("items", []) if isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise CatalogSyncError("Oasis вернул неожиданный формат товаров.")
            fresh = [value for value in page if isinstance(value, dict) and str(value.get("id", "")) not in seen_ids]
            rows.extend(fresh)
            seen_ids.update(str(value.get("id", "")) for value in fresh)
            if len(page) < 500 or not fresh:
                break
        supplier = CatalogSupplier(code=supplier_code, name="Oasis", base_url=client.base_url)
        marker = str(uuid.uuid4())
        pool.extend(value for value in (
            _product_from_payload(supplier, raw, category_map, marker)
            for raw in rows if isinstance(raw, dict)
        ) if value and value.is_active)
        pool = _aggregate_color_variants(pool)
        source_status["oasis"] = {"status": "success", "message": "", "received": len(pool)}
    except CatalogSyncError as exc:
        source_status["oasis"] = {"status": "failed", "message": str(exc)[:300], "received": 0}
        pool = []
    except Exception as exc:
        logger.exception("Unexpected Oasis catalogue search failure")
        source_status["oasis"] = {
            "status": "failed",
            "message": "Oasis вернул данные в неожиданном формате.",
            "received": 0,
        }
        pool = []

    # Gifts is searched independently by words present in its stored name and
    # description (search_text), regardless of whether Oasis had a category.
    gifts_query = Q()
    gifts_terms = list(dict.fromkeys(_planner_source_terms(intent, "gifts") + list(search_aliases)))
    for term in gifts_terms:
        gifts_query |= Q(search_text__icontains=term)
    gifts_supplier_exists = CatalogSupplier.objects.filter(code="gifts", is_active=True).exists()
    cached_products = list(CatalogProduct.objects.filter(
        Q(supplier__code="gifts") & Q(is_active=True) & gifts_query
    ).order_by("id")[:1500])
    pool.extend(cached_products)
    source_status["gifts"] = {
        "status": "success" if gifts_supplier_exists else "not_configured",
        "message": "" if gifts_supplier_exists else "Каталог Gifts ещё не загружен.",
        "received": len(cached_products),
    }
    ranked = []
    allowed_source_values = intent.get("allowed_sources", []) if isinstance(intent, dict) and isinstance(intent.get("allowed_sources"), list) else []
    allowed_sources = {_normalized(value) for value in allowed_source_values if _normalized(value)}
    for product in pool:
        if product.total_stock <= 0:
            continue
        if allowed_sources and not ({_normalized(product.supplier.code), _normalized(product.supplier.name)} & allowed_sources):
            continue
        score, matches, mismatches, unknown = _fit_product(product, effective_line, anchors, quantity, intent=intent)
        if "Не совпадает тип товара" in mismatches:
            continue
        hard_mismatches = _required_mismatches(mismatches, intent)
        constraint_matches, constraint_mismatches, constraint_unknown, constraint_hard = _evaluate_structured_constraints(product, intent)
        if hard_mismatches or constraint_hard:
            continue
        matches.extend(constraint_matches)
        mismatches.extend(constraint_mismatches)
        unknown.extend(constraint_unknown)
        score += _catalog_parameter_score(constraint_matches, constraint_mismatches, constraint_unknown, _planner_weight_map(intent))
        name_score = SequenceMatcher(
            None,
            _normalized(line.get("name", "")),
            _normalized(product.full_name or product.name),
        ).ratio()
        ranked.append((score, name_score, product, matches, mismatches, unknown))
    price_weight = _ranking_weight(intent, "цена", "стоимость")
    name_weight = _ranking_weight(intent, "название", "наименование", "модель")
    prices = [value[2].effective_price for value in ranked if value[2].effective_price is not None]
    minimum_price = min(prices) if prices else None
    maximum_price = max(prices) if prices else None
    preferred_source_values = intent.get("preferred_sources", []) if isinstance(intent, dict) and isinstance(intent.get("preferred_sources"), list) else []
    preferred_sources = {_normalized(value) for value in preferred_source_values if _normalized(value)}

    def total_score(value):
        relevance_score, product = value[0], value[2]
        score = relevance_score + value[1] * 100 * name_weight
        if preferred_sources and ({_normalized(product.supplier.code), _normalized(product.supplier.name)} & preferred_sources):
            score += 20
        if not price_weight or product.effective_price is None or minimum_price is None:
            return score
        if maximum_price == minimum_price:
            price_score = 100
        else:
            price_score = float((maximum_price - product.effective_price) / (maximum_price - minimum_price)) * 100
        return score + price_score * price_weight

    ranked.sort(key=lambda value: (
        -total_score(value),
        value[2].effective_price is None,
        value[2].effective_price if value[2].effective_price is not None else Decimal("Infinity"),
        -value[1],
        _normalized(value[2].full_name or value[2].name),
        _normalized(value[2].article),
    ))
    selected, seen_groups = [], set()
    for score, name_score, product, matches, mismatches, unknown in ranked:
        group_key = product.group_id or product.external_id
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        price = product.effective_price
        product_url = product.product_url
        supplier_site = urlparse(product_url or product.supplier.base_url).netloc.lower()
        if supplier_site.startswith("www."):
            supplier_site = supplier_site[4:]
        selected.append({
            "id": product.external_id,
            "supplier_code": product.supplier.code,
            "supplier_name": product.supplier.name,
            "supplier_site": supplier_site,
            "external_id": product.external_id,
            "article": product.article,
            "name": product.full_name or product.name,
            "price": str(price) if price is not None else None,
            "cost_total": str((price * quantity).quantize(Decimal("0.01"))) if price is not None and quantity > 0 else None,
            "stock": product.total_stock,
            "delivery_days": product.delivery_days,
            "image_url": product.image_url,
            "url": product_url,
            "fit": "exact" if not mismatches and not unknown else "partial",
            "matches": matches,
            "mismatches": mismatches,
            "unknown": unknown,
            "score": round(score, 2),
            "synced_at": timezone.now().isoformat(),
            "category": (category["path"] or category["name"]) if category else "Поиск по названию и описанию",
            "sizes": product.raw_data.get("sizes", []) if isinstance(product.raw_data, dict) else [],
            "variant_ids": product.raw_data.get("variant_ids", []) if isinstance(product.raw_data, dict) else [],
        })
        if len(selected) >= max(1, min(10, limit)):
            break
    if include_diagnostics:
        return {
            "candidates": selected,
            "sources": source_status,
            "attempts": [{
                "categories": planner_categories,
                "terms": text_terms,
                "candidate_count": len(selected),
            }],
        }
    return selected
