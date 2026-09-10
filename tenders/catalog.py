import base64
import io
import json
import logging
import os
import re
import time
import uuid
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import CatalogCategory, CatalogProduct, CatalogSupplier, CatalogSyncRun


logger = logging.getLogger(__name__)


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


def _gifts_tree_index(tree_xml):
    categories, products = [], {}
    for _, node in ElementTree.iterparse(tree_xml, events=("end",)):
        node_name = node.tag.rsplit("}", 1)[-1]
        if node_name == "product":
            product_id = node.attrib.get("product") or _gifts_text(node, "product") or _text(node.text, 500)
            page_id = node.attrib.get("page") or _gifts_text(node, "page")
            if product_id and page_id:
                category_ids = products.setdefault(str(product_id), [])
                if str(page_id) not in category_ids:
                    category_ids.append(str(page_id))
                node.clear()
            continue
        if node_name != "page":
            continue
        page_id = _text(node.attrib.get("page_id") or _gifts_text(node, "page_id"), 100)
        page_name = _text(node.attrib.get("name") or _gifts_text(node, "name"), 300)
        if page_id and page_name:
            categories.append({
                "external_id": page_id,
                "parent_external_id": _text(
                    node.attrib.get("parent_id") or node.attrib.get("parent_page_id")
                    or _gifts_text(node, "parent_id"), 100,
                ),
                "name": page_name,
                "path": page_name,
            })
            for product in node.iter():
                if product.tag.rsplit("}", 1)[-1] != "product":
                    continue
                product_id = product.attrib.get("product") or _gifts_text(product, "product") or _text(product.text, 500)
                if product_id:
                    category_ids = products.setdefault(str(product_id), [])
                    if page_id not in category_ids:
                        category_ids.append(page_id)
            node.clear()
        elif list(node):
            node.clear()
    return categories, products


def parse_gifts_catalog(
    product_xml, tree_xml, stock_xml=None, category=None, limit=None, filters_xml=None,
    include_categories=False,
):
    category = _normalized(category) if category else ""
    filter_colors = _gifts_filter_colors(filters_xml)
    categories, product_categories = _gifts_tree_index(tree_xml)
    category_names = {value["external_id"]: value["path"] for value in categories}
    allowed_product_ids = {
        product_id for product_id, category_ids in product_categories.items()
        if any(category in _normalized(category_names.get(category_id)) for category_id in category_ids)
    } if category else set()

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
        if not product_id or (category and str(product_id) not in allowed_product_ids):
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
        product_category_ids = product_categories.get(str(product_id), [])
        product_category_names = [category_names[value] for value in product_category_ids if value in category_names]
        search_text = _normalized(" ".join(filter(None, [name, article, material, size, brand, *product_category_names, description, *colors, *name_colors])))[:20_000]
        result.append({
            "external_id": _text(str(product_id), 100), "article": article, "name": name, "full_name": name,
            "description": description, "category_ids": product_category_ids[:50], "category_names": product_category_names[:50],
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
    return (result, categories) if include_categories else result


def _store_gifts_categories(supplier, categories):
    objects = [CatalogCategory(
        supplier=supplier,
        external_id=value["external_id"],
        parent_external_id=value.get("parent_external_id", ""),
        name=value["name"],
        path=value.get("path", ""),
        is_active=True,
    ) for value in categories if value.get("external_id") and value.get("name")]
    if not objects:
        return
    with transaction.atomic():
        CatalogCategory.objects.filter(supplier=supplier).update(is_active=False)
        CatalogCategory.objects.bulk_create(
            objects, update_conflicts=True, unique_fields=["supplier", "external_id"],
            update_fields=["parent_external_id", "name", "path", "is_active"],
        )


def _gifts_category_tree(tree_xml):
    root = ElementTree.parse(tree_xml).getroot()
    if root.tag.rsplit("}", 1)[-1].lower() == "error":
        raise CatalogSyncError(_text(root.text, 5000) or "gifts.ru не вернул карту категорий.")

    records = {}

    def visit(node, nested_parent_id=""):
        local_name = node.tag.rsplit("}", 1)[-1].lower()
        child_parent_id = nested_parent_id
        if local_name == "page":
            external_id = _text(node.attrib.get("page_id") or _gifts_text(node, "page_id"), 100)
            name = _text(node.attrib.get("name") or _gifts_text(node, "name"), 300)
            if external_id and name:
                parent_external_id = _text(
                    node.attrib.get("parent_id") or node.attrib.get("parent_page_id")
                    or _gifts_text(node, "parent_id") or nested_parent_id,
                    100,
                )
                records[external_id] = {
                    "external_id": external_id,
                    "parent_external_id": parent_external_id,
                    "name": name,
                }
                child_parent_id = external_id
        for child in node:
            visit(child, child_parent_id)

    visit(root)
    if not records:
        raise CatalogSyncError("gifts.ru вернул пустую карту категорий.")

    resolved_paths = {}

    def resolve_path(external_id, trail=None):
        if external_id in resolved_paths:
            return resolved_paths[external_id]
        record = records[external_id]
        parent_id = record["parent_external_id"]
        trail = set(trail or ())
        if external_id in trail or not parent_id or parent_id not in records:
            path = record["name"]
        else:
            trail.add(external_id)
            path = f"{resolve_path(parent_id, trail)} > {record['name']}"
        resolved_paths[external_id] = path
        return path

    return [
        {**record, "path": resolve_path(external_id)}
        for external_id, record in records.items()
    ]


def sync_gifts_categories(client=None):
    client = client or GiftsXmlClient()
    supplier, _ = CatalogSupplier.objects.get_or_create(
        code="gifts", defaults={"name": "gifts.ru", "base_url": client.base_url},
    )
    supplier.base_url = client.base_url
    supplier.sync_status = "running"
    supplier.sync_message = "Получаю карту категорий gifts.ru"
    supplier.save(update_fields=["base_url", "sync_status", "sync_message"])
    try:
        with client.open("catalogue/treeWithoutProducts.xml") as tree_xml:
            categories = _gifts_category_tree(tree_xml)
        _store_gifts_categories(supplier, categories)
        supplier.last_synced_at = timezone.now()
        supplier.sync_status = "success"
        supplier.sync_message = f"Категорий: {len(categories)}"
        supplier.save(update_fields=["last_synced_at", "sync_status", "sync_message"])
        return {
            value.external_id: value.path or value.name
            for value in CatalogCategory.objects.filter(supplier=supplier, is_active=True)
        }
    except Exception as exc:
        supplier.sync_status = "failed"
        supplier.sync_message = _text(exc, 500)
        supplier.save(update_fields=["sync_status", "sync_message"])
        raise


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
                rows, categories = parse_gifts_catalog(
                    product_xml, tree_xml, stock_xml, category=category,
                    filters_xml=filters_xml, include_categories=True,
                )
                _store_gifts_categories(supplier, categories)
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
    # Same story as _normalized below: the same ТЗ requirement strings get
    # passed in millions of times across a large pool search. str(value) is
    # always defined and hashable, so cache on that rather than on `value`
    # itself (which may not be hashable, e.g. a list).
    return _text_cached(str(value or ""), limit)


@lru_cache(maxsize=32_768)
def _text_cached(raw, limit):
    return re.sub(r"\s+", " ", raw).strip()[:limit]


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
        with transaction.atomic():
            CatalogCategory.objects.filter(supplier=supplier).update(is_active=False)
            CatalogCategory.objects.bulk_create(objects, update_conflicts=True, unique_fields=["supplier", "external_id"], update_fields=["parent_external_id", "name", "path", "is_active"])
    return {value.external_id: value.path or value.name for value in CatalogCategory.objects.filter(supplier=supplier, is_active=True)}


def sync_oasis_categories(client=None):
    client = client or OasisClient()
    supplier, _ = CatalogSupplier.objects.get_or_create(
        code="oasis", defaults={"name": "Oasis", "base_url": client.base_url},
    )
    return _sync_categories(client, supplier)


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


def _normalized(value):
    # A catalog search compares the same ТЗ requirement text against every
    # pool product (hundreds to thousands), so this is called millions of
    # times per search with a small set of repeated strings — cache the
    # regex work on the string form, never on the raw (possibly unhashable) value.
    return _normalized_cached(_text(value, 20_000))


@lru_cache(maxsize=16_384)
def _normalized_cached(text):
    return re.sub(r"[^a-zа-я0-9%²³≥≤]+", " ", text.lower().replace("ё", "е")).strip()







































def _meaningful_tokens(value):
    ignored = {"цвет", "материал", "состав", "изделие", "товар", "требуется", "должен", "должна", "менее", "более", "процентов"}
    return {token for token in _normalized(value).split() if len(token) >= 3 and token not in ignored and not token.isdigit()}


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


_SIZE_SUFFIX_RE = re.compile(r",?\s*размер\s+[0-9a-zа-яё./\-–\s]+$", re.I)
_SIZE_IN_NAME_RE = re.compile(r"размер\s+([0-9a-zа-яё./\-–]+)", re.I)


def _variant_size(product):
    """The size of one SKU — from a ", размер XL" tail in the name (Gifts)
    or the `size` field / an attribute (Oasis). A "Размер" that is really a
    dimension string ("4 х 1,2 х 0,4 см") is not a variant label — skip it."""
    match = _SIZE_IN_NAME_RE.search(_text(product.name, 300))
    if match:
        return match.group(1).strip(" .,")
    for size in _product_sizes(product):
        if _looks_like_variant_size(size):
            return size
    return ""


def _looks_like_variant_size(text):
    """True for "32ГБ" / "XL" / "48-50" / "M"; false for "5,8 х 1,8 х 0,8 см"
    (a dimensions string a supplier happened to file under "Размер")."""
    value = _text(text, 60).strip()
    if not value:
        return False
    if _capacity_mb(value) is not None:
        return True
    if re.search(r"\d\s*[x×х*]\s*\d", value) or re.search(r"\d\s*(?:см|мм|cm|mm)\b", value, re.I):
        return False
    return len(value) <= 8


def _gifts_variant_keys(products):
    """Map each Gifts product to its colour-family key. Gifts has no
    `color_group_id`; the size SKUs of one colour share an article prefix
    with the parent row ("Ветровка Kivach, ярко-красная" `7102.51` →
    "…красная, размер L" `7102.513`) and their colour name can drift
    ("ярко-красная" vs "красная"), so the article is the reliable link.
    Group by the shortest article another row of the same product line
    extends; if nothing extends it, by the size-stripped name."""
    head6_by_article = {}
    for product in products:
        article = _text(product.article, 60)
        if article and article not in head6_by_article:
            head6_by_article[article] = _normalized(_SIZE_SUFFIX_RE.sub("", _text(product.name, 300)))[:6]
    keys = {}
    for product in products:
        article = _text(product.article, 60)
        head6 = _normalized(_SIZE_SUFFIX_RE.sub("", _text(product.name, 300)))[:6]
        parent = ""
        for cut in range(len(article) - 1, 4, -1):
            candidate = article[:cut]
            if candidate != article and head6_by_article.get(candidate) == head6 and head6:
                parent = candidate
                break
        # A parent row keys on its own article so its size children (which
        # resolve to that same article) join it.
        keys[id(product)] = f"gifts:{parent or article}" if article else f"gifts:name:{_normalized(product.name)[:44]}"
    return keys


def _aggregate_color_variants(products, supplier_code="oasis"):
    """Represent one colour family as one offer instead of one card per size SKU."""
    gifts_keys = _gifts_variant_keys(products) if supplier_code == "gifts" else {}
    grouped = {}
    for product in products:
        if _text(product.color_group_id, 120):
            key = product.color_group_id
        elif supplier_code == "gifts":
            key = gifts_keys.get(id(product), product.external_id)
        else:
            key = product.external_id
        grouped.setdefault(key, []).append(product)
    result = []
    for family_id, variants in grouped.items():
        # Prefer the parent row: it carries the image and the colour list;
        # a size SKU row usually has neither and a ", размер XL" name.
        representative = (
            next((value for value in variants if value.color_group_id and value.color_group_id == family_id), None)
            or next((value for value in variants if not _SIZE_SUFFIX_RE.search(_text(value.name, 300)) and _text(value.image_url, 500)), None)
            or next((value for value in variants if _text(value.image_url, 500)), None)
            or next((value for value in variants if value.external_id == family_id), None)
            or variants[0]
        )
        variant_ids = [value.external_id for value in variants]
        prices = [value.effective_price for value in variants if value.effective_price is not None]
        variant_details = [{
            "size": _variant_size(value),
            "product_id": value.external_id,
            "article": value.article,
            "stock": max(0, value.total_stock),
            "price": str(value.effective_price.quantize(Decimal("0.01"))) if value.effective_price is not None else None,
        } for value in variants]
        sizes = []
        for value in variants:
            size = _variant_size(value)
            if size and size not in sizes:
                sizes.append(size)
        representative.total_stock = sum(max(0, value.total_stock) for value in variants)
        representative.is_on_order = any(value.is_on_order for value in variants)
        if len(variants) > 1:
            # The card is now one colour, all sizes — drop a ", размер XL"
            # tail if the representative row happens to be a size SKU.
            representative.name = _SIZE_SUFFIX_RE.sub("", _text(representative.name, 300)).rstrip(" ,")
            representative.full_name = _SIZE_SUFFIX_RE.sub("", _text(representative.full_name, 300)).rstrip(" ,")
        # Only a real Oasis color_group_id gives a stable per-family URL
        # (`/item/<id>`); the synthetic "supplier:name" key and every Gifts
        # group keep the representative's own id and product_url.
        if supplier_code == "oasis" and ":" not in str(family_id):
            representative.external_id = family_id
            representative.product_url = f"https://www.oasiscatalog.com/item/{family_id}"
        if prices:
            # Use the highest variant price to avoid silently understating a
            # mixed-size tender when a supplier prices sizes differently.
            representative.discount_price = max(prices)
        representative.raw_data = {
            **(representative.raw_data if isinstance(representative.raw_data, dict) else {}),
            "variant_ids": variant_ids,
            "sizes": sizes,
            "variants": variant_details,
        }
        result.append(representative)
    return result


def _product_variants(product):
    if isinstance(product.raw_data, dict) and isinstance(product.raw_data.get("variants"), list):
        return [value for value in product.raw_data["variants"] if isinstance(value, dict)]
    sizes = _product_sizes(product)
    return [{
        "size": sizes[0] if sizes else "",
        "product_id": product.external_id,
        "article": product.article,
        "stock": max(0, product.total_stock),
        "price": str(product.effective_price.quantize(Decimal("0.01"))) if product.effective_price is not None else None,
    }]


_CAPACITY_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(гигабайт|гбайт|гб|gb|терабайт|тбайт|тб|tb|мегабайт|мбайт|мб|mb)(?![а-яa-z])",
    re.I,
)
_CAPACITY_TO_MB = {
    "мб": 1, "мбайт": 1, "мегабайт": 1, "mb": 1,
    "гб": 1024, "гбайт": 1024, "гигабайт": 1024, "gb": 1024,
    "тб": 1048576, "тбайт": 1048576, "терабайт": 1048576, "tb": 1048576,
}
def _capacity_mb(text):
    """Memory capacity of a string ("32 ГБ", "…, 16 Gb, …") in megabytes, or None."""
    match = _CAPACITY_RE.search(_normalized(text))
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ".")) * _CAPACITY_TO_MB[match.group(2).lower()]
    except (InvalidOperation, KeyError):
        return None


# "Честный Знак" / ЦРПТ marking is a labelling-compliance obligation the
# supplier fulfils when producing the batch — never a catalogue attribute.
# Checking it against products is pure noise: an "unknown" for every product,
# or a FALSE mismatch where a catalogue reuses the field name "Маркировка" for
# something unrelated (Oasis stores a certification date there, e.g.
# "2024-04-01"). That false mismatch sank every Oasis product below every
# Gifts one on any tender that requires Честный Знак — i.e. most of them.
_COMPLIANCE_MARKING_RE = re.compile(r"честн\w*\s*знак|црпт|обязательн\w*\s+маркиров")







def _query_stems(phrases):
    """The meaningful words of the query, stemmed to 6 chars, deduped —
    the unit both retrieval and relevance work in."""
    stems = []
    for phrase in phrases:
        for token in _meaningful_tokens(phrase):
            stem = token[:6]
            if stem not in stems:
                stems.append(stem)
    return stems


def _stem_in_words(stem, words):
    """A query stem (a query word cut to 6 chars) matches a product word
    when they share a long-enough leading run — bridges ordinary Russian
    inflection (сумка / сумки / сумке, рубашка / рубашке) without a full
    morphology library. Does NOT bridge fleeting-vowel pairs (мешок /
    мешка) or spelling variants (шопер / шоппер) — the search plan is
    expected to supply those word forms itself. A short stem (< 5 chars,
    e.g. "поло") only matches exactly, so "поло" never matches
    "полотенце"."""
    for word in words:
        if word == stem:
            return True
        shared = min(len(word), len(stem))
        if shared >= 5 and word[: shared - 1] == stem[: shared - 1]:
            return True
        if len(stem) == 6 and word.startswith(stem):
            return True
    return False


def _text_search_pool(supplier_code, phrases):
    """Every mirrored card of one supplier whose PRODUCT NAME carries a
    query word (as a stem). Description and attributes are deliberately not
    searched here: a card that merely mentions «флешка» somewhere in its
    spec table is not a flash drive, and a name-only pool is small enough
    (a few thousand) to hand straight to the AI name filter without a cap.
    What the rest of the card says is read later — by that filter and by
    the per-ТЗ agent pass.

    The pool comes back ordered by how many distinct query words the name
    carries (more first), so the most-likely items lead; every card is
    tagged `_name_hits` with that count for `_score_pool_relevance`."""
    stems = _query_stems(phrases)
    if not stems:
        return []
    base = CatalogProduct.objects.filter(supplier__code=supplier_code, is_active=True)
    from .name_index import rank_names

    rows = base.values_list("id", "name", "full_name").order_by("id").iterator(chunk_size=4000)
    ranked_ids = rank_names(rows, stems)
    if not ranked_ids:
        return []
    # More query words in the name first; stable within a tier, so cards
    # keep the id order the funnel and variant grouping expect.
    ranked_ids.sort(key=lambda value: -value[0])
    by_id = base.select_related("supplier").in_bulk([pid for _, pid in ranked_ids])
    pool = []
    for hits, pid in ranked_ids:
        product = by_id.get(pid)
        if product is not None:
            product._name_hits = hits
            pool.append(product)
    return pool


def _score_pool_relevance(pool, item_phrase, phrases):
    """Merge the per-supplier name-matched lists into one order — the most
    query words in the name first — and tag every card with `_relevance`
    for the ranking tiebreak (Cascade.step_7_collapse_and_sort):

      0  the whole item phrase is in the name, or the name carries two or
         more distinct query words  («Сумка-шоппер …», «USB-флешка …»)
      1  the name carries only one query word  («Спортивная сумка …»)

    `_text_search_pool` already dropped every card with no query word in
    the name and set `_name_hits`; this only re-orders the combined pool
    and fills `_relevance`. No document-frequency maths, no word lists."""
    all_stems = _query_stems(phrases)
    item_stems = set(_query_stems([item_phrase])) if item_phrase else set()
    survivors = []
    for product in pool:
        name_words = _normalized(f"{product.name} {product.full_name}").split()
        hits = getattr(product, "_name_hits", None)
        if hits is None:
            hits = sum(1 for stem in all_stems if _stem_in_words(stem, name_words))
            if not hits:
                continue
            product._name_hits = hits
        full_item = bool(item_stems) and all(_stem_in_words(stem, name_words) for stem in item_stems)
        product._relevance = 0 if (full_item or hits >= 2) else 1
        survivors.append(product)
    survivors.sort(key=lambda product: -getattr(product, "_name_hits", 0))
    return survivors


def _refresh_live_oasis_prices(client, candidates, quantity=0):
    """One batched live call for exactly the Oasis cards about to be shown —
    the mirror can be hours old, but the price and stock quoted to a tender
    must be current. Uses the API's `ids=` filter (confirmed batch lookup,
    ~1s regardless of count) instead of a per-item call."""
    oasis_candidates = [value for value in candidates if value.get("supplier_code") == "oasis" and value.get("external_id")]
    if not client or not oasis_candidates:
        return
    ids = list(dict.fromkeys(value["external_id"] for value in oasis_candidates))
    try:
        payload = client.get("/v4/products", {
            "format": "json", "ids": ",".join(ids), "available": 1, "includeGroupId": 1,
        })
        rows = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return
    except Exception:
        logger.exception("Live Oasis price/stock refresh failed; showing mirrored values")
        return
    fresh = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
    for value in oasis_candidates:
        row = fresh.get(value["external_id"])
        if not row:
            continue
        price = _decimal(row.get("discount_price") or row.get("price"))
        stock = max(0, _integer(row.get("total_stock")))
        value["stock"] = stock
        value["price"] = str(price) if price is not None else None
        value["cost_total"] = (
            str((price * quantity).quantize(Decimal("0.01"))) if price is not None and quantity > 0 else None
        )




