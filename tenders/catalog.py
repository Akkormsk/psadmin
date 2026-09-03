import base64
import hashlib
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

from django.db import connection, transaction
from django.db.models import Q
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
    _refresh_category_embeddings(supplier)


def _category_embedding_text(category, names_by_id):
    parent_name = names_by_id.get(category.parent_external_id, "")
    return " | ".join(filter(None, [
        category.name,
        f"Родитель: {parent_name}" if parent_name else "",
        f"Путь: {category.path}" if category.path else "",
    ]))


def _refresh_category_embeddings(supplier):
    from .services import TenderAIError, _embedding_model, _embedding_vectors, _embeddings_enabled

    if not _embeddings_enabled():
        return 0
    categories = list(CatalogCategory.objects.filter(supplier=supplier, is_active=True))
    names_by_id = {value.external_id: value.name for value in categories}
    model = _embedding_model()
    pending = []
    for category in categories:
        text = _category_embedding_text(category, names_by_id)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if category.embedding and category.embedding_model == model and category.embedding_text_hash == text_hash:
            continue
        pending.append((category, text, text_hash))
    updated = []
    try:
        for offset in range(0, len(pending), 64):
            batch = pending[offset:offset + 64]
            vectors = _embedding_vectors([text for _, text, _ in batch], model=model)
            for (category, _, text_hash), vector in zip(batch, vectors):
                category.embedding = vector
                category.embedding_model = model
                category.embedding_text_hash = text_hash
                category.embedding_updated_at = timezone.now()
                updated.append(category)
    except TenderAIError:
        logger.warning("Could not refresh category embeddings for supplier %s", supplier.code)
        return 0
    if updated:
        CatalogCategory.objects.bulk_update(
            updated,
            ["embedding", "embedding_model", "embedding_text_hash", "embedding_updated_at"],
            batch_size=200,
        )
    return len(updated)


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
        with transaction.atomic():
            CatalogCategory.objects.filter(supplier=supplier).update(is_active=False)
            CatalogCategory.objects.bulk_create(objects, update_conflicts=True, unique_fields=["supplier", "external_id"], update_fields=["parent_external_id", "name", "path", "is_active"])
        _refresh_category_embeddings(supplier)
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
    return re.sub(r"[^a-zа-я0-9%²³≥≤]+", " ", _text(value, 20_000).lower().replace("ё", "е")).strip()


REQUIREMENT_FIELD_MARKERS = (
    ("volume", ("объем", "вместимост")),
    ("material", ("материал", "состав")),
    ("color", ("цвет", "оттен")),
    ("density", ("плотност",)),
    ("size", ("размер",)),
    ("mass", ("масса", "вес")),
    ("length", ("длина",)),
    ("width", ("ширина",)),
    ("height", ("высота",)),
    ("diameter", ("диаметр",)),
    ("thickness", ("толщина",)),
)

MEASUREMENT_UNITS = {
    "volume": "ml",
    "mass": "g",
    "length": "mm",
    "width": "mm",
    "height": "mm",
    "diameter": "mm",
    "thickness": "mm",
    "density": "g/m²",
}


def _requirement_field(label):
    normalized = _normalized(label)
    return next((field for field, markers in REQUIREMENT_FIELD_MARKERS if any(marker in normalized for marker in markers)), normalized)


def _decimal_text(value):
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _normalized_measurement(field, label, value):
    if field not in MEASUREMENT_UNITS:
        return None
    raw = f"{_text(label, 300)} {_text(value, 1000)}".lower().replace("ё", "е")
    match = re.search(r"-?\d+(?:[.,]\d+)?", raw.replace(" ", ""))
    if not match:
        return None
    try:
        number = Decimal(match.group(0).replace(",", "."))
    except InvalidOperation:
        return None

    factor = Decimal("1")
    if field == "volume":
        if re.search(r"(?:^|[^a-zа-я])(л|l|литр(?:а|ов)?)(?:$|[^a-zа-я])", raw) and not re.search(r"(?:мл|ml)", raw):
            factor = Decimal("1000")
    elif field == "mass":
        if re.search(r"(?:кг|kg|килограмм)", raw):
            factor = Decimal("1000")
        elif re.search(r"(?:мг|mg|миллиграмм)", raw):
            factor = Decimal("0.001")
    elif field in {"length", "width", "height", "diameter", "thickness"}:
        if re.search(r"(?:^|[^a-zа-я])(см|cm)(?:$|[^a-zа-я])", raw):
            factor = Decimal("10")
        elif re.search(r"(?:^|[^a-zа-я])(м|m|метр(?:а|ов)?)(?:$|[^a-zа-я])", raw) and not re.search(r"(?:мм|mm)", raw):
            factor = Decimal("1000")
    return {
        "field": field,
        "value": _decimal_text(number * factor),
        "unit": MEASUREMENT_UNITS[field],
    }


def _normalized_requirement(requirement):
    label = _text(requirement.get("label"), 300)
    value = _text(requirement.get("value"), 1000)
    field = _requirement_field(label)
    measurement = _normalized_measurement(field, label, value)
    scope = _requirement_scope(label)
    if measurement:
        result = {**measurement, "operator": "eq"}
    else:
        result = {"field": field, "operator": "eq", "value": _normalized(value), "unit": ""}
    if scope:
        result["scope"] = scope
    return result


def _requirement_scope(label):
    prefix, separator, suffix = _text(label, 300).partition(":")
    if not separator or not _normalized(prefix) or not _normalized(suffix):
        return ""
    suffix_field = _requirement_field(suffix)
    prefix_field = _requirement_field(prefix)
    return _normalized(prefix) if suffix_field and suffix_field != prefix_field else ""


def _requirement_identity(requirement):
    normalized = _normalized_requirement(requirement)
    return normalized["field"], normalized["operator"], normalized["value"], normalized["unit"], normalized.get("scope", "")


def _requirement_values(line):
    requirements = line.get("requirements") if isinstance(line, dict) else {}
    if isinstance(requirements, dict):
        requirements = requirements.get("requirements", [])
    if not isinstance(requirements, list):
        return []
    result, seen = [], set()
    for value in requirements:
        if not isinstance(value, dict):
            continue
        key = _requirement_identity(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _product_requirement_values(line):
    return [value for value in _requirement_values(line) if not _requirement_scope(value.get("label"))]


def _constraint_text(line, label_marker):
    return " ".join(_text(value.get("value"), 1000) for value in _product_requirement_values(line) if label_marker in _normalized(value.get("label")))


def _meaningful_tokens(value):
    ignored = {"цвет", "материал", "состав", "изделие", "товар", "требуется", "должен", "должна", "менее", "более", "процентов"}
    return {token for token in _normalized(value).split() if len(token) >= 3 and token not in ignored and not token.isdigit()}


_MATERIAL_SYNONYMS = (
    {"спанбонд", "спанбонда", "спанбонде", "нетканый", "нетканого", "нетканое", "нетканая", "полипропилен", "полипропиленовый"},
    {"хлопок", "хлопка", "хлопковый", "хлопковая", "хлопчатобумажный", "cotton"},
    {"полиэстер", "полиэстера", "полиэфир", "polyester"},
)


def _expand_material_tokens(tokens):
    expanded = set(tokens)
    for group in _MATERIAL_SYNONYMS:
        if tokens & group:
            expanded |= group
    return expanded


def _required_gender(line):
    text = _normalized(" ".join([
        _constraint_text(line, "пол"), _constraint_text(line, "гендер"), _text(line.get("name", ""), 300),
    ]))
    if "унисекс" in text:
        return "унисекс"
    if "женск" in text:
        return "женский"
    if "мужск" in text:
        return "мужской"
    return ""


def _product_gender(product):
    text = _normalized(" ".join([
        product.name or "", product.full_name or "",
        *_attribute_values(product, ("пол", "гендер", "половой")),
    ]))
    if "унисекс" in text:
        return "унисекс"
    if "женск" in text:
        return "женский"
    if "мужск" in text:
        return "мужской"
    return ""


def _shares_product_token(phrases, text):
    """True when a meaningful word from any phrase also appears (as a stem) in text."""
    text_tokens = _meaningful_tokens(text)
    for phrase in phrases:
        for token in _meaningful_tokens(phrase):
            if len(token) < 4:
                continue
            for other in text_tokens:
                if token == other:
                    return True
                short, long = sorted((token, other), key=len)
                if len(short) >= 4 and long.startswith(short):
                    return True
    return False


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
    for key in ("required", "preferred"):
        values = intent.get(key)
        if not isinstance(values, list):
            continue
        for value in values[:20]:
            if not isinstance(value, dict):
                continue
            label, item_value = _text(value.get("label"), 300), _text(value.get("value"), 1000)
            if label and item_value:
                result.append({"label": label, "value": item_value, "group": key})
    return result


def _line_with_planner_requirements(line, intent):
    existing = _requirement_values(line)
    requirements = list(existing)
    seen = {_requirement_identity(value) for value in existing}
    existing_fields = {_requirement_field(value.get("label")) for value in _product_requirement_values(line)}
    line_name = _normalized(line.get("name") if isinstance(line, dict) else "")
    for value in _planner_requirements(intent):
        field = _requirement_field(value["label"])
        field_markers = next((markers for key, markers in REQUIREMENT_FIELD_MARKERS if key == field), ())
        if existing and field not in existing_fields and not any(marker in line_name for marker in field_markers):
            continue
        key = _requirement_identity(value)
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
        ("volume", ("volume", "объем", "вместимост")),
        ("mass", ("mass", "масса", "вес")),
        ("length", ("length", "длина")),
        ("width", ("width", "ширина")),
        ("height", ("height", "высота")),
        ("diameter", ("diameter", "диаметр")),
        ("thickness", ("thickness", "толщина")),
        ("size", ("size", "размер")),
        ("density", ("плотност",)),
        ("branding", ("нанес", "вышив", "гравиров", "печать", "логотип")),
        ("stock", ("остаток", "налич", "тираж", "количеств", "склад")),
        ("gender", ("gender", "пол", "гендер", "мужск", "женск", "унисекс")),
        ("source", ("source", "источник", "поставщик")),
    )
    return next((key for key, markers in groups if any(marker in normalized for marker in markers)), normalized)


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


def _oasis_category_snapshot(client):
    supplier = CatalogSupplier.objects.filter(code="oasis", is_active=True).first()
    categories = list(CatalogCategory.objects.filter(
        supplier=supplier, is_active=True,
    ).values("external_id", "parent_external_id", "name", "path", "embedding", "embedding_model")) if supplier else []
    if not categories:
        sync_oasis_categories(client)
        supplier = CatalogSupplier.objects.get(code="oasis")
        categories = list(CatalogCategory.objects.filter(
            supplier=supplier, is_active=True,
        ).values("external_id", "parent_external_id", "name", "path", "embedding", "embedding_model"))
    return [{
        "id": value["external_id"],
        "parent_id": value["parent_external_id"],
        "name": value["name"],
        "path": value["path"] or value["name"],
        "embedding": value["embedding"],
        "embedding_model": value["embedding_model"],
    } for value in categories]


def _category_candidates(categories_by_source, line, intent, excluded_tasks=None, limit_per_source=12):
    intent = intent if isinstance(intent, dict) else {}
    excluded = {
        (_text(value.get("source"), 50).lower(), str(value.get("category_id", "")))
        for value in excluded_tasks or [] if isinstance(value, dict)
    }
    weighted_phrases = []
    for weight, values in (
        (6, [intent.get("item")]),
        (5, [line.get("name") if isinstance(line, dict) else ""]),
        (5, intent.get("synonyms", []) if isinstance(intent.get("synonyms"), list) else []),
        (3, [intent.get("product_class")]),
        (1, intent.get("categories", []) if isinstance(intent.get("categories"), list) else []),
    ):
        for value in values:
            normalized = _normalized(value)
            if normalized:
                weighted_phrases.append((weight, normalized))

    generic_tokens = {"товар", "товары", "каталог", "одежда", "текстиль", "сувенир", "сувениры", "продукция"}
    class_tokens = set(_normalized(intent.get("product_class")).split())
    token_weights = {}
    for weight, phrase in weighted_phrases:
        for token in phrase.split():
            if len(token) < 3 or token.isdigit():
                continue
            token_weights[token] = token_weights.get(token, 0) + weight
    for token in set(_normalized(intent.get("item")).split()) - class_tokens - generic_tokens:
        if len(token) >= 3 and not token.isdigit():
            token_weights[token] = token_weights.get(token, 0) + 8

    def compatible(left, right):
        if left == right:
            return True
        if min(len(left), len(right)) < 5:
            return False
        prefix_length = max(4, min(6, len(left) - 1, len(right) - 1))
        return left[:prefix_length] == right[:prefix_length]

    result = []
    for source, categories in categories_by_source.items():
        source_rows = []
        for category in categories if isinstance(categories, list) else []:
            category_id = str(category.get("id") or category.get("external_id") or "")
            source_code = _text(source, 50).lower()
            if not category_id or (source_code, category_id) in excluded:
                continue
            name = _text(category.get("name"), 300)
            path = _text(category.get("path") or name, 1000)
            name_tokens = _normalized(name).split()
            path_tokens = _normalized(path).split()
            matched_weights = [
                weight for token, weight in token_weights.items()
                if any(compatible(token, offered) for offered in [*name_tokens, *path_tokens])
            ]
            if not matched_weights:
                continue
            depth = path.count("/") + path.count(">")
            distinctive_matches = sum(
                weight for token, weight in token_weights.items()
                if token not in generic_tokens and any(compatible(token, offered) for offered in name_tokens)
            )
            exact_bonus = max((
                weight * 3 for weight, phrase in weighted_phrases
                if phrase == _normalized(name)
            ), default=0)
            generic_penalty = 20 if name_tokens and all(token in generic_tokens for token in name_tokens) else 0
            score = sum(matched_weights) + distinctive_matches + exact_bonus + min(8, depth * 2) - generic_penalty
            source_rows.append({
                "source": source_code,
                "category_id": category_id,
                "name": name,
                "path": path,
                "specificity": round(score, 3),
            })
        source_rows.sort(key=lambda value: (-value["specificity"], -value["path"].count("/"), _normalized(value["path"])))
        result.extend(source_rows[:max(1, min(30, limit_per_source))])
    return sorted(result, key=lambda value: (-value["specificity"], value["source"], _normalized(value["path"])))


def _complete_category_options(categories_by_source, line, intent, excluded_tasks=None):
    excluded = {
        (_text(value.get("source"), 50).lower(), str(value.get("category_id", "")))
        for value in excluded_tasks or [] if isinstance(value, dict)
    }
    ranked = _category_candidates(
        categories_by_source, line, intent, excluded_tasks=excluded_tasks, limit_per_source=10_000,
    )
    scores = {
        (value["source"], value["category_id"]): value["specificity"] for value in ranked
    }
    result = []
    for source, categories in categories_by_source.items():
        normalized_source = _text(source, 50).lower()
        for category in categories if isinstance(categories, list) else []:
            category_id = str(category.get("id") or category.get("external_id") or "")
            if not category_id or (normalized_source, category_id) in excluded:
                continue
            result.append({
                "source": normalized_source,
                "category_id": category_id,
                "name": _text(category.get("name"), 300),
                "parent_id": str(category.get("parent_id") or category.get("parent_external_id") or ""),
                "path": _text(category.get("path") or category.get("name"), 1000),
                "specificity": scores.get((normalized_source, category_id), 0),
                "embedding": category.get("embedding") if isinstance(category.get("embedding"), list) else [],
                "embedding_model": _text(category.get("embedding_model"), 100),
            })
    return sorted(result, key=lambda value: (
        -value["specificity"], value["source"], _normalized(value["path"]), value["category_id"],
    ))


def _category_search_representation(line, intent, search_terms=None):
    intent = intent if isinstance(intent, dict) else {}
    item = _text(intent.get("item"), 300)
    primary_terms = []

    def add_term(value):
        value = _text(value, 300)
        if value and _normalized(value) not in {_normalized(term) for term in primary_terms}:
            primary_terms.append(value)

    if search_terms is None:
        add_term(item)
        for value in intent.get("synonyms", []) if isinstance(intent.get("synonyms"), list) else []:
            add_term(value)
        for query in intent.get("fallback_queries", []) if isinstance(intent.get("fallback_queries"), list) else []:
            if isinstance(query, dict):
                for value in query.get("terms", []) if isinstance(query.get("terms"), list) else []:
                    add_term(value)
    else:
        for value in search_terms:
            add_term(value)

    category_hints = [
        _text(value, 300)
        for value in (intent.get("categories", []) if isinstance(intent.get("categories"), list) else [])
        if _text(value, 300)
    ]
    requirement_terms = []
    for group in ("required", "preferred"):
        for value in intent.get(group, []) if isinstance(intent.get(group), list) else []:
            if isinstance(value, dict) and _text(value.get("value"), 300):
                requirement_terms.append(_text(value.get("value"), 300))
    for constraint in intent.get("constraints", []) if isinstance(intent.get("constraints"), list) else []:
        if not isinstance(constraint, dict):
            continue
        for value in constraint.get("values", []) if isinstance(constraint.get("values"), list) else []:
            if _text(value, 300):
                requirement_terms.append(_text(value, 300))
    return {
        "item": item,
        "search_terms": primary_terms[:20],
        "product_class": _text(intent.get("product_class"), 300),
        "category_hints": list(dict.fromkeys(category_hints))[:10],
        "requirement_terms": list(dict.fromkeys(requirement_terms))[:20],
    }


def _category_text_match(term, name, path):
    term = _normalized(term)
    name = _normalized(name)
    path = _normalized(path)
    if not term:
        return 0
    if term == name:
        return 1
    if term in name or name in term:
        return .88
    if term in path:
        return .68
    term_tokens = {value for value in term.split() if len(value) >= 3}
    offered_tokens = {value for value in f"{name} {path}".split() if len(value) >= 3}
    matched_tokens = sum(
        any(
            required == offered
            or (min(len(required), len(offered)) >= 5 and required[:4] == offered[:4])
            for offered in offered_tokens
        )
        for required in term_tokens
    )
    overlap = matched_tokens / len(term_tokens) if term_tokens else 0
    fuzzy = SequenceMatcher(None, term, name).ratio()
    return max(overlap * .72, fuzzy * .55 if fuzzy >= .55 else 0)


def _category_retrieval(categories, line, intent, search_terms=None, limit=48):
    representation = _category_search_representation(line, intent, search_terms=search_terms)
    model = ""
    query_embedding = []
    try:
        from .services import TenderAIError, _cosine_similarity, _embedding_model, _embedding_vector, _embeddings_enabled

        model = _embedding_model()
        has_embeddings = any(
            value.get("embedding_model") == model and isinstance(value.get("embedding"), list) and value.get("embedding")
            for value in categories
        )
        if _embeddings_enabled() and has_embeddings and representation["search_terms"]:
            from django.core.cache import cache

            embedding_text = " | ".join(representation["search_terms"])
            cache_key = f"category-query-embedding:{model}:{hashlib.sha256(embedding_text.encode('utf-8')).hexdigest()}"
            query_embedding = cache.get(cache_key) or []
            if not query_embedding:
                query_embedding = _embedding_vector(embedding_text, model=model)
                cache.set(cache_key, query_embedding, 24 * 60 * 60)
    except TenderAIError:
        query_embedding = []

    weighted_terms = [
        *[(value, 1 if index == 0 else .9) for index, value in enumerate(representation["search_terms"])],
        *[(value, .3) for value in representation["category_hints"]],
        *[(value, .2) for value in representation["requirement_terms"]],
    ]
    if representation["product_class"]:
        weighted_terms.append((representation["product_class"], .15))
    ranked = []
    for category in categories:
        source = _text(category.get("source"), 50).lower()
        category_id = str(category.get("category_id", ""))
        if not source or not category_id:
            continue
        matches = [weight * _category_text_match(term, category.get("name", ""), category.get("path", "")) for term, weight in weighted_terms]
        lexical_score = max(matches, default=0) + sum(sorted(matches, reverse=True)[1:4]) * .12
        semantic_score = 0
        embedding = category.get("embedding")
        if query_embedding and category.get("embedding_model") == model and isinstance(embedding, list):
            semantic_score = max(0, _cosine_similarity(query_embedding, embedding))
        score = lexical_score + semantic_score * .45
        if score >= .12:
            ranked.append({**category, "retrieval_score": round(score, 6)})
    ranked.sort(key=lambda value: (-value["retrieval_score"], value["source"], _normalized(value.get("path"))))

    leaders = {}
    for value in ranked:
        leaders.setdefault(value["source"], value)
    selected = sorted(leaders.values(), key=lambda value: (-value["retrieval_score"], value["source"]))[:limit]
    selected_keys = {(value["source"], value["category_id"]) for value in selected}
    for value in ranked:
        key = (value["source"], value["category_id"])
        if key in selected_keys:
            continue
        selected.append(value)
        selected_keys.add(key)
        if len(selected) >= limit:
            break
    selected.sort(key=lambda value: (-value["retrieval_score"], value["source"], _normalized(value.get("path"))))
    return selected, {
        "considered_count": len(categories),
        "candidate_count": len(selected),
        "represented_sources": sorted({value["source"] for value in selected}),
        "semantic_embedding_used": bool(query_embedding),
        "representation": representation,
    }


def _expand_category_graph(categories, seeds, limit=120):
    index = {(value.get("source"), str(value.get("category_id", ""))): value for value in categories}
    children_by_parent = {}
    for value in categories:
        parent_id = str(value.get("parent_id") or "")
        if parent_id:
            children_by_parent.setdefault((value.get("source"), parent_id), []).append(value)
    selected = {}

    def include(value):
        if not value or len(selected) >= limit:
            return
        key = (value.get("source"), str(value.get("category_id", "")))
        if key[0] and key[1]:
            selected.setdefault(key, value)

    for seed in seeds:
        include(seed)
    for seed in seeds:
        key = (seed.get("source"), str(seed.get("category_id", "")))
        parent_id = str(seed.get("parent_id") or "")
        include(index.get((key[0], parent_id)))
        children = sorted(
            children_by_parent.get(key, []),
            key=lambda value: (_normalized(value.get("name")), str(value.get("category_id", ""))),
        )[:24]
        for child in children:
            include(child)

    selected_keys = set(selected)
    result = []
    for key, value in sorted(selected.items(), key=lambda item: (item[0][0], _normalized(item[1].get("path")), item[0][1])):
        child_ids = [
            str(child.get("category_id", ""))
            for child in children_by_parent.get(key, [])
            if (key[0], str(child.get("category_id", ""))) in selected_keys
        ]
        result.append({
            "source": key[0], "category_id": key[1], "name": value.get("name", ""),
            "parent_id": str(value.get("parent_id") or ""), "child_ids": child_ids,
            "path": value.get("path") or value.get("name", ""),
        })
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
        variant_details = [{
            "size": _product_sizes(value)[0] if _product_sizes(value) else "",
            "product_id": value.external_id,
            "article": value.article,
            "stock": max(0, value.total_stock),
            "price": str(value.effective_price.quantize(Decimal("0.01"))) if value.effective_price is not None else None,
        } for value in variants]
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
            "variants": variant_details,
        }
        result.append(representative)
    return result


CONSTRAINT_FIELD_LABELS = {
    "gender": "Пол", "material": "Материал", "color": "Цвет", "density": "Плотность",
    "branding": "Нанесение", "stock": "Остаток", "price": "Цена", "name": "Название",
    "product_type": "Тип товара", "source": "Поставщик", "volume": "Объём", "mass": "Масса",
    "length": "Длина", "width": "Ширина", "height": "Высота", "diameter": "Диаметр",
    "thickness": "Толщина", "size": "Размер",
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
        return [f"{_decimal_text(value)} {MEASUREMENT_UNITS[field]}"] if value is not None else []
    if field in MEASUREMENT_UNITS:
        values = _measurement_values_for_product(product, field)
        return [f"{value['value']} {value['unit']}" for value in values]
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
    if field in MEASUREMENT_UNITS:
        result = []
        for value in values:
            normalized = _normalized_measurement(field, field, value)
            if normalized:
                canonical = f"{normalized['value']} {normalized['unit']}"
                if canonical not in result:
                    result.append(canonical)
        return result
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
    if field in MEASUREMENT_UNITS:
        left = _normalized_measurement(field, field, expected)
        right = _normalized_measurement(field, field, offered)
        return bool(left and right and left["unit"] == right["unit"] and left["value"] == right["value"])
    return _values_compatible(expected, offered)


def _deduplicated_structured_constraints(intent):
    values = intent.get("constraints", []) if isinstance(intent, dict) and isinstance(intent.get("constraints"), list) else []
    result, seen = [], set()
    for constraint in values:
        if not isinstance(constraint, dict):
            continue
        field = _criterion_key(constraint.get("field"))
        operator = _normalized(constraint.get("operator")).replace(" ", "_")
        expected = _constraint_expected_values(
            field,
            constraint.get("values", []) if isinstance(constraint.get("values"), list) else [],
        )
        key = field, operator, tuple(expected), _normalized(constraint.get("level")), _normalized(constraint.get("missing_policy"))
        if key in seen:
            continue
        seen.add(key)
        result.append(constraint)
    return result


def _structured_constraint_requirement_keys(constraint):
    field = _criterion_key(constraint.get("field"))
    operator = _normalized(constraint.get("operator")).replace(" ", "_")
    if operator not in {"eq", "in", "contains"}:
        return set()
    values = constraint.get("values", []) if isinstance(constraint.get("values"), list) else []
    keys = set()
    for value in values:
        measurement = _normalized_measurement(field, field, value)
        if measurement:
            keys.add((field, "eq", measurement["value"], measurement["unit"], ""))
        else:
            keys.add((field, "eq", _normalized(value), "", ""))
    return keys


def _evaluate_structured_constraints(product, intent, requirement_keys=None):
    matches, mismatches, unknown, hard_mismatches = [], [], [], []
    constraints = _deduplicated_structured_constraints(intent)
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        if _structured_constraint_requirement_keys(constraint) & set(requirement_keys or ()):
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
        elif operator in {"eq", "in", "contains"}:
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


REQUIREMENT_DISPLAY_LABELS = {
    "volume": "Объём",
    "mass": "Масса",
    "length": "Длина",
    "width": "Ширина",
    "height": "Высота",
    "diameter": "Диаметр",
    "thickness": "Толщина",
    "size": "Размер",
}


def _normalized_size(value):
    return re.sub(r"\s+", "", _text(value, 100).upper().replace("Х", "X"))


def _requested_size_quantities(line):
    result = {}
    for requirement in _product_requirement_values(line):
        if _requirement_field(requirement.get("label")) != "size":
            continue
        value = _text(requirement.get("value"), 2000)
        for match in re.finditer(r"(?<!\w)([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9./-]{0,11})\s*[—–:=]\s*(\d+)\s*(?:шт\.?\b)?", value):
            size = _normalized_size(match.group(1))
            quantity = int(match.group(2))
            if size and quantity > 0:
                result[size] = result.get(size, 0) + quantity
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


def _measurement_values_for_product(product, field):
    if field == "density":
        density = _product_density(product)
        return [{"field": field, "value": _decimal_text(density), "unit": MEASUREMENT_UNITS[field]}] if density is not None else []
    result = []
    for attribute in product.attributes if isinstance(product.attributes, list) else []:
        if not isinstance(attribute, dict) or _requirement_field(attribute.get("name")) != field:
            continue
        normalized = _normalized_measurement(field, attribute.get("name"), attribute.get("value"))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalized_comparison_values(product, line, quantity, intent=None):
    requirements, product_values = [], []
    size_quantities = _requested_size_quantities(line)
    requirement_keys = {_requirement_identity(value) for value in _product_requirement_values(line)}
    for requirement in _product_requirement_values(line):
        normalized = _normalized_requirement(requirement)
        field = normalized["field"]
        if field == "size" and size_quantities:
            continue
        if normalized not in requirements:
            requirements.append(normalized)
        if field in MEASUREMENT_UNITS:
            offered = _measurement_values_for_product(product, field)
        elif field == "material":
            offered = [{"field": field, "value": _normalized(value), "unit": ""} for value in product.materials]
        elif field == "color":
            offered = [{"field": field, "value": _normalized(value), "unit": ""} for value in product.colors]
        else:
            offered = []
            for attribute in product.attributes if isinstance(product.attributes, list) else []:
                if isinstance(attribute, dict) and _requirement_field(attribute.get("name")) == field:
                    offered.append({"field": field, "value": _normalized(attribute.get("value")), "unit": ""})
        for value in offered:
            if value not in product_values:
                product_values.append(value)
    for constraint in _deduplicated_structured_constraints(intent):
        if _structured_constraint_requirement_keys(constraint) & requirement_keys:
            continue
        field = _criterion_key(constraint.get("field"))
        operator = _normalized(constraint.get("operator")).replace(" ", "_")
        expected = _constraint_expected_values(
            field,
            constraint.get("values", []) if isinstance(constraint.get("values"), list) else [],
        )
        normalized_expected = []
        for value in expected:
            measurement = _normalized_measurement(field, field, value)
            normalized_expected.append({
                "value": measurement["value"] if measurement else _normalized(value),
                "unit": measurement["unit"] if measurement else "",
            })
        record = {"field": field, "operator": operator, "values": normalized_expected}
        if record not in requirements:
            requirements.append(record)
        offered_values = _constraint_product_values(product, field)
        for value in offered_values:
            measurement = _normalized_measurement(field, field, value)
            product_record = {
                "field": field,
                "value": measurement["value"] if measurement else _normalized(value),
                "unit": measurement["unit"] if measurement else "",
            }
            if product_record not in product_values:
                product_values.append(product_record)
    if size_quantities:
        requirements.append({
            "field": "size_stock", "operator": "gte",
            "values": [{"size": size, "quantity": needed} for size, needed in size_quantities.items()],
            "unit": "pcs",
        })
        product_values.append({"field": "size_stock", "variants": _product_variants(product), "unit": "pcs"})
    elif quantity > 0:
        requirements.append({"field": "stock", "operator": "gte", "value": str(quantity), "unit": "pcs"})
        product_values.append({"field": "stock", "value": str(product.total_stock), "unit": "pcs"})
    return requirements, product_values


def _requirement_matches_attribute(field, requirement, attribute):
    required_measurement = _normalized_measurement(field, requirement.get("label"), requirement.get("value"))
    offered_measurement = _normalized_measurement(field, attribute.get("name"), attribute.get("value"))
    if required_measurement and offered_measurement:
        return (
            required_measurement["unit"] == offered_measurement["unit"]
            and required_measurement["value"] == offered_measurement["value"]
        )
    return _values_compatible(requirement.get("value"), attribute.get("value"))


def _fit_product(product, line, anchors, quantity, intent=None, from_selected_category=False, name_anchors=()):
    name_anchors = name_anchors or anchors
    matches, mismatches, unknown = [], [], []
    type_text = _normalized(" ".join([
        *(product.category_names if isinstance(product.category_names, list) else []),
        product.name,
        product.full_name,
    ]))
    anchor_hit = next((value for value in anchors if _entity_phrase_matches(value, type_text)), "")
    if anchor_hit:
        matches.append(f"Тип товара: {anchor_hit}")
    elif from_selected_category and _shares_product_token(name_anchors, type_text):
        # The category was picked for this search and the name shares a word with
        # the planned item ("снуд" in a "шарфы" node) — trust it over an exact
        # phrase miss. A card with nothing in common (напульсник for бафф) is
        # still the wrong type.
        unknown.append("Тип товара не подтверждён по названию")
    else:
        mismatches.append("Не совпадает тип товара")

    material_text = _constraint_text(line, "материал") or _constraint_text(line, "состав")
    material_tokens = _meaningful_tokens(material_text)
    material_values = product.materials if isinstance(product.materials, list) and product.materials else _attribute_values(product, ("материал", "состав"))
    product_materials = _meaningful_tokens(" ".join(material_values))
    if material_tokens:
        material_overlap = _expand_material_tokens(material_tokens) & _expand_material_tokens(product_materials)
        if material_overlap:
            matches.append(f"Материал: {', '.join(sorted(material_tokens & product_materials) or material_overlap)}")
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
            elif required_family and offered_family and not name_color_match:
                # Both colours are known and belong to different families
                # (white asked, red offered). Confident enough to drop the card.
                mismatches.append(f"Цвет не подходит: требуется {color_text}; в каталоге {', '.join(color_values)}")
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

    required_gender = _required_gender(line)
    product_gender = _product_gender(product) if required_gender else ""
    if required_gender and product_gender and not (
        product_gender == required_gender
        or product_gender == "унисекс"
        or (required_gender == "унисекс" and product_gender == "мужской")
    ):
        mismatches.append(f"Пол не совпадает: требуется {required_gender}; товар {product_gender}")

    handled_markers = ("материал", "состав", "цвет", "плотност", "нанес", "печат", "логотип", "вышив", "остаток", "наличие", "тираж", "размер", "пол", "гендер")
    product_attributes = [
        attribute for attribute in product.attributes
        if isinstance(attribute, dict) and _text(attribute.get("name"), 300) and _text(attribute.get("value"), 1000)
    ] if isinstance(product.attributes, list) else []
    for requirement in _product_requirement_values(line):
        label = _text(requirement.get("label"), 300)
        value = _text(requirement.get("value"), 1000)
        label_normalized = _normalized(label)
        if not label_normalized or not value or any(marker in label_normalized for marker in handled_markers):
            continue
        if any(marker in label_normalized for marker in ("коммент", "примеч")):
            continue
        label_tokens = _meaningful_tokens(label_normalized)
        field = _requirement_field(label)
        related = [
            attribute for attribute in product_attributes
            if (
                field and field == _requirement_field(attribute.get("name"))
                or label_tokens and any(token in _normalized(attribute.get("name")) for token in label_tokens)
            )
        ]
        display_label = REQUIREMENT_DISPLAY_LABELS.get(field, label.rstrip(":"))
        if not related:
            unknown.append(f"{display_label} не указан в каталоге")
            continue
        offered_values = [_text(attribute.get("value"), 1000) for attribute in related]
        if any(_requirement_matches_attribute(field, requirement, attribute) for attribute in related):
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

    size_quantities = _requested_size_quantities(line)
    if size_quantities:
        stock_by_size = {}
        for variant in _product_variants(product):
            size = _normalized_size(variant.get("size"))
            if size:
                stock_by_size[size] = stock_by_size.get(size, 0) + max(0, _integer(variant.get("stock")))
        for size, needed in size_quantities.items():
            available = stock_by_size.get(size, 0)
            if available >= needed:
                matches.append(f"Размер {size}: доступно {available} шт., требуется {needed} шт.")
            else:
                mismatches.append(f"Размер {size}: доступно {available} из {needed} шт.")

    return matches, mismatches, unknown


def _catalog_product_eligibility(product, line, effective_line, anchors, quantity, intent, from_selected_category=False, name_anchors=()):
    matches, mismatches, unknown = _fit_product(
        product, effective_line, anchors, quantity, intent=intent,
        from_selected_category=from_selected_category, name_anchors=name_anchors,
    )
    hard_reasons, hard_codes, partial_reasons = [], [], []
    if "Не совпадает тип товара" in mismatches:
        hard_reasons.append("Не совпадает тип товара")
        hard_codes.append("product_type")
    colour_reason = next((value for value in mismatches if value.startswith("Цвет не подходит")), "")
    if colour_reason:
        hard_reasons.append(colour_reason)
        hard_codes.append("colour")
    if quantity > 0 and product.total_stock < quantity:
        hard_reasons.append(f"Недостаточный общий остаток: требуется {quantity}, доступно {product.total_stock}")
        hard_codes.append("insufficient_total_stock")

    allowed_sources = {
        _normalized(value) for value in intent.get("allowed_sources", [])
        if _normalized(value)
    } if isinstance(intent, dict) and intent.get("_source_only_confirmed") else set()
    if allowed_sources and not ({_normalized(product.supplier.code), _normalized(product.supplier.name)} & allowed_sources):
        hard_reasons.append("Источник запрещён подтверждённым правилом source_only")
        hard_codes.append("source")

    requirement_keys = {_requirement_identity(value) for value in _product_requirement_values(effective_line)}
    constraint_matches, constraint_mismatches, constraint_unknown, _ = _evaluate_structured_constraints(
        product, intent, requirement_keys=requirement_keys,
    )
    matches.extend(constraint_matches)
    mismatches.extend(constraint_mismatches)
    unknown.extend(constraint_unknown)

    for constraint in _deduplicated_structured_constraints(intent):
        single_matches, single_mismatches, _, _ = _evaluate_structured_constraints(
            product, {"constraints": [constraint]},
        )
        if single_matches or not single_mismatches:
            continue
        operator = _normalized(constraint.get("operator")).replace(" ", "_")
        level = _normalized(constraint.get("level"))
        if operator in {"not_in", "not_contains"}:
            hard_reasons.extend(single_mismatches)
            hard_codes.append("forbidden")
        elif level == "required":
            # A required technical characteristic the catalogue does not answer
            # (or answers differently) keeps the product as a ranked alternative;
            # only wrong type, wrong colour, stock and explicit bans remove it.
            partial_reasons.extend(single_mismatches)

    required_fields = {
        _requirement_field(value.get("label")) for value in _product_requirement_values(line)
    } | {
        _criterion_key(value.get("label"))
        for value in _planner_requirements(intent) if value.get("group") == "required"
    }
    for mismatch in mismatches:
        if mismatch in hard_reasons or mismatch.startswith(("Недостаточный остаток", "На складе")):
            continue
        if mismatch.startswith("Размер ") or _criterion_key(mismatch) in required_fields:
            partial_reasons.append(mismatch)

    hard_reasons = list(dict.fromkeys(hard_reasons))
    partial_reasons = list(dict.fromkeys(partial_reasons))
    if hard_reasons:
        status = "rejected"
        reasons = hard_reasons
    elif partial_reasons:
        status = "partial_eligible"
        reasons = partial_reasons
    else:
        status = "exact_eligible"
        reasons = []
    return {
        "status": status,
        "reasons": reasons,
        "hard_codes": list(dict.fromkeys(hard_codes)),
        "matches": list(dict.fromkeys(matches)),
        "mismatches": list(dict.fromkeys(mismatches)),
        "unknown": list(dict.fromkeys(unknown)),
    }


# A well-scoped category never holds this many SKUs; the ceiling only stops a
# too-broad category (or a bare full-text query) from crawling Oasis for minutes
# at the client's 1s-per-page rate limit.
_OASIS_PAGE_CEILING = 6


def catalog_candidates_for_line(
    line, limit=3, supplier_code="oasis", intent=None, client=None, include_diagnostics=False,
    category_selector=None, excluded_category_tasks=None, force_full_text=False,
):
    """Return a relevance-ranked shortlist from live Oasis and cached suppliers."""
    try:
        quantity = int(Decimal(str(line.get("quantity") or 0).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        quantity = 0
    categories, category, category_map = [], None, {}
    category_tasks, category_usage, category_errors = [], {}, []
    fields = (
        "id,article,name,full_name,description,article_base,group_id,color_group_id,size,images,colors,"
        "categories,categories_array,brand,attributes,materials,branding,package,price,discount_price,"
        "total_stock,is_on_order,delivery_days,supply_terms,lead,defect,updated_at"
    )
    rows, seen_ids = [], set()
    planner_categories = _planner_categories(intent)
    semantic_entities = [
        (intent or {}).get("item", "") if isinstance(intent, dict) else "",
        *((intent or {}).get("synonyms", []) if isinstance(intent, dict) and isinstance((intent or {}).get("synonyms"), list) else []),
        (intent or {}).get("product_class", "") if isinstance(intent, dict) else "",
        *planner_categories,
        _text(line.get("name", ""), 300),
    ]
    search_aliases = tuple(dict.fromkeys(_text(value, 300) for value in semantic_entities if _text(value, 300)))
    aliases = tuple(planner_categories) or search_aliases or (_text(line.get("name", ""), 300),)
    specific_entities = [
        (intent or {}).get("item", "") if isinstance(intent, dict) else "",
        *((intent or {}).get("synonyms", []) if isinstance(intent, dict) and isinstance((intent or {}).get("synonyms"), list) else []),
        *planner_categories,
    ]
    anchors = tuple(dict.fromkeys(_text(value, 300) for value in specific_entities if _text(value, 300)))
    if not anchors:
        anchors = tuple(value for value in (
            (intent or {}).get("product_class", "") if isinstance(intent, dict) else "",
            _text(line.get("name", ""), 300),
        ) if _text(value, 300))
    name_anchors = tuple(dict.fromkeys(_text(value, 300) for value in [
        (intent or {}).get("item", "") if isinstance(intent, dict) else "",
        *((intent or {}).get("synonyms", []) if isinstance(intent, dict) and isinstance((intent or {}).get("synonyms"), list) else []),
    ] if _text(value, 300))) or anchors
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
        categories = _oasis_category_snapshot(client)
        category_map = {value["id"]: value["path"] or value["name"] for value in categories}
        gifts_categories = list(CatalogCategory.objects.filter(
            supplier__code="gifts", supplier__is_active=True, is_active=True,
        ).values("external_id", "parent_external_id", "name", "path", "embedding", "embedding_model"))
        category_options = _complete_category_options({
            "oasis": categories,
            "gifts": gifts_categories,
        }, line, intent or {}, excluded_tasks=excluded_category_tasks)
        if category_selector and not force_full_text:
            try:
                category_tasks, category_usage, category_errors = category_selector(
                    line, intent or {}, category_options, excluded_category_tasks or [],
                )
            except Exception:
                logger.exception("Catalog category selection failed; using backend priority")
                best_by_source = {}
                for option in category_options:
                    best_by_source.setdefault(option["source"], option)
                category_tasks = [
                    {**option, "priority": 1} for option in best_by_source.values()
                ]
                category_errors = [
                    "Не удалось выбрать категории через LLM; использован серверный приоритет."
                ]
            available_tasks = {
                (value["source"], value["category_id"]): value for value in category_options
            }
            category_tasks = [
                {**available_tasks[(str(value.get("source", "")), str(value.get("category_id", "")))],
                 "priority": value.get("priority", 1)}
                for value in category_tasks if isinstance(value, dict)
                and (str(value.get("source", "")), str(value.get("category_id", ""))) in available_tasks
            ][:8]
        elif not force_full_text:
            category = _live_category_for_intent(categories, intent or {}, line)
            if category:
                category_tasks = [{
                    "source": "oasis", "category_id": category["id"],
                    "name": category["name"], "path": category["path"], "priority": 1,
                }]
        selected_oasis_categories = [
            next((value for value in categories if value["id"] == task["category_id"]), None)
            for task in sorted(category_tasks, key=lambda value: value.get("priority", 1))
            if task.get("source") == "oasis"
        ]
        selected_oasis_categories = [value for value in selected_oasis_categories if value]
        category = selected_oasis_categories[0] if selected_oasis_categories else None
        # Use the category when available. Otherwise ask Oasis for a bounded
        # full-text page; final matching is still performed by _fit_product.
        search_terms = list(dict.fromkeys(_planner_source_terms(intent, "oasis") + list(search_aliases)))
        search_terms = search_terms[:8]
        oasis_search_categories = selected_oasis_categories or ([None] if not category_tasks else [])
        for selected_category in oasis_search_categories:
            offset = 0
            for _ in range(_OASIS_PAGE_CEILING):
                params = {
                    "format": "json", "limit": 500, "offset": offset,
                    "available": 1, "includeGroupId": 1, "fields": fields,
                }
                if selected_category:
                    params["category"] = selected_category["id"]
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
                offset += len(page)
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
    selected_gifts_ids = {
        str(value.get("category_id")) for value in category_tasks if value.get("source") == "gifts"
    } if category_selector else set()
    gifts_product_query = Q(supplier__code="gifts") & Q(is_active=True)
    if not selected_gifts_ids:
        gifts_product_query &= gifts_query
    if selected_gifts_ids:
        gifts_products = CatalogProduct.objects.filter(gifts_product_query).order_by("id")
        if connection.features.supports_json_field_contains:
            category_query = Q()
            for category_id in selected_gifts_ids:
                category_query |= Q(category_ids__contains=[category_id])
            cached_products = list(gifts_products.filter(category_query)[:1500])
        else:
            cached_products = []
            for value in gifts_products.iterator(chunk_size=1000):
                if selected_gifts_ids & {str(category_id) for category_id in value.category_ids}:
                    cached_products.append(value)
                    if len(cached_products) >= 1500:
                        break
    elif category_selector and category_tasks:
        cached_products = []
    else:
        cached_products = list(CatalogProduct.objects.filter(gifts_product_query).order_by("id")[:1500])
    pool.extend(cached_products)
    source_status["gifts"] = {
        "status": "success" if gifts_supplier_exists else "not_configured",
        "message": "" if gifts_supplier_exists else "Каталог Gifts ещё не загружен.",
        "received": len(cached_products),
    }
    ranked = []
    from_selected_category = bool(category_tasks) and not force_full_text
    rejections = {
        "out_of_stock": 0, "insufficient_total_stock": 0, "source": 0,
        "product_type": 0, "colour": 0, "forbidden": 0, "missing_required": 0,
    }
    eligibility_counts = {"exact_eligible": 0, "partial_eligible": 0, "rejected": 0}
    rejection_reasons, partial_reasons = {}, {}
    for product in pool:
        if product.total_stock <= 0:
            rejections["out_of_stock"] += 1
            eligibility_counts["rejected"] += 1
            rejection_reasons["Нулевой остаток"] = rejection_reasons.get("Нулевой остаток", 0) + 1
            continue
        eligibility = _catalog_product_eligibility(
            product, line, effective_line, anchors, quantity, intent,
            from_selected_category=from_selected_category, name_anchors=name_anchors,
        )
        if eligibility["status"] == "rejected":
            eligibility_counts["rejected"] += 1
            for code in eligibility["hard_codes"]:
                if code in rejections:
                    rejections[code] += 1
            for reason in eligibility["reasons"]:
                label = (
                    "Недостаточный общий остаток" if reason.startswith("Недостаточный общий остаток")
                    else reason
                )
                rejection_reasons[label] = rejection_reasons.get(label, 0) + 1
            continue
        eligibility_counts[eligibility["status"]] += 1
        if eligibility["status"] == "partial_eligible":
            for reason in eligibility["reasons"]:
                partial_reasons[reason] = partial_reasons.get(reason, 0) + 1
        mismatches = eligibility["mismatches"]
        unknown = [value for value in eligibility["unknown"] if value not in mismatches]
        ranked.append((
            product, eligibility["matches"], mismatches, unknown,
            eligibility["status"] == "partial_eligible", eligibility["status"], eligibility["reasons"],
        ))

    # A product's place is simply how far it is from the requirements: fewest
    # mismatches first, then fewest unknowns, then the cheaper one. No weights,
    # no supplier bonus, no name similarity.
    ranked.sort(key=lambda value: (
        value[4],
        len(value[2]),
        len(value[3]),
        value[0].effective_price is None,
        value[0].effective_price if value[0].effective_price is not None else Decimal("Infinity"),
        _normalized(value[0].full_name or value[0].name),
        _normalized(value[0].article),
    ))
    display_ranked = ranked
    selected, seen_groups = [], set()
    for product, matches, mismatches, unknown, _, eligibility_status, eligibility_reasons in display_ranked:
        group_key = product.group_id or product.external_id
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        price = product.effective_price
        product_url = product.product_url
        supplier_site = urlparse(product_url or product.supplier.base_url).netloc.lower()
        if supplier_site.startswith("www."):
            supplier_site = supplier_site[4:]
        normalized_requirements, normalized_product_values = _normalized_comparison_values(
            product, effective_line, quantity, intent=intent,
        )
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
            "eligibility": eligibility_status,
            "eligibility_reasons": eligibility_reasons,
            "synced_at": timezone.now().isoformat(),
            "category": (
                product.category_names[0]
                if isinstance(product.category_names, list) and product.category_names else
                (category["path"] or category["name"]) if category else "Поиск по названию и описанию"
            ),
            "sizes": product.raw_data.get("sizes", []) if isinstance(product.raw_data, dict) else [],
            "variant_ids": product.raw_data.get("variant_ids", []) if isinstance(product.raw_data, dict) else [],
            "color_group_id": product.color_group_id or product.external_id,
            "variants": _product_variants(product),
            "normalized_requirements": normalized_requirements,
            "normalized_product_values": normalized_product_values,
        })
        if len(selected) >= max(1, min(10, limit)):
            break
    if include_diagnostics:
        return {
            "candidates": selected,
            "sources": source_status,
            "attempts": [{
                "mode": "full_text" if force_full_text or not category_tasks else "selected_categories",
                "category_tasks": category_tasks,
                "categories": planner_categories,
                "terms": text_terms,
                "pool_count": len(pool),
                "rejections": rejections,
                "eligibility_counts": eligibility_counts,
                "rejection_reasons": rejection_reasons,
                "partial_reasons": partial_reasons,
                "exact_count": eligibility_counts["exact_eligible"],
                "partial_count": eligibility_counts["partial_eligible"],
                "candidate_count": len(selected),
            }],
            "category_usage": category_usage,
            "category_errors": category_errors,
        }
    return selected
