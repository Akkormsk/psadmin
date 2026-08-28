import base64
import io
import json
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
        value = next((child.attrib.get(key) for key in ("src", "url", "href", "path") if child.attrib.get(key)), None) or child.text
        value = _text(value, 1000)
        if value:
            return value
    return ""


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


def parse_gifts_catalog(product_xml, tree_xml, stock_xml=None, category=None, limit=None):
    category = _normalized(category) if category else ""
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
        image_src = _gifts_image_src(product)
        image_url = image_src if image_src.startswith("http") else f"https://files.gifts.ru/{image_src.lstrip('/')}" if image_src else ""
        price_group = _gifts_child(product, "price")
        price_node = _gifts_child(price_group, "price") if price_group is not None else None
        price = _decimal(price_node.text if price_node is not None else None)
        stock_free = _integer(stock.get("free")) if stock is not None else 0
        dealer_price = _decimal(stock.get("dealerprice")) if stock is not None else None
        category_name = category_ids.get(str(product_id), "")
        search_text = _normalized(" ".join(filter(None, [name, article, material, size, brand, category_name, description, *colors])))[:20_000]
        result.append({
            "external_id": _text(str(product_id), 100), "article": article, "name": name, "full_name": name,
            "description": description, "category_ids": [], "category_names": [category_name] if category_name else [],
            "brand": brand, "size": size, "materials": [material] if material else [], "colors": colors, "attributes": [],
            "branding": [], "package": [], "price": price, "discount_price": dealer_price, "total_stock": stock_free,
            "stock_moscow": stock_free, "stock_remote": 0, "stock_transit": _integer(stock.get("inwayfree")) if stock is not None else 0,
            "is_on_order": _gifts_text(product, "ondemand").lower() == "true", "delivery_days": _integer(_gifts_text(product, "days")) or None,
            "image_url": image_url, "product_url": f"https://gifts.ru/catalog/{article}" if article else "https://gifts.ru",
            "supply_terms": _gifts_text(product, "demandtype"), "warning": _gifts_text(product, "alert"), "defect": "",
            "search_text": search_text, "source_updated_at": None, "sync_marker": "", "is_active": True, "raw_data": {"status": _gifts_text(product, "status")},
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
            with client.open("catalogue/product.xml") as product_xml:
                rows = parse_gifts_catalog(product_xml, io.BytesIO(b"<root />"), category=category, limit=limit)
        else:
            with client.open("catalogue/product.xml") as product_xml, client.open("catalogue/tree.xml") as tree_xml, client.open("catalogue/stock.xml") as stock_xml:
                rows = parse_gifts_catalog(product_xml, tree_xml, stock_xml, category=category)
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


PRODUCT_ANCHORS = {
    "футболка": ("футболка", "майка"),
    "майка": ("футболка", "майка"),
    "жилет": ("жилет", "безрукавка"),
    "жилетка": ("жилет", "безрукавка"),
    "поло": ("поло", "футболка поло"),
    "кепка": ("кепка", "бейсболка"),
    "бейсболка": ("бейсболка", "кепка"),
    "толстовка": ("толстовка", "худи", "свитшот"),
    "худи": ("худи", "толстовка"),
    "свитшот": ("свитшот", "толстовка"),
    "ручка": ("ручка",),
    "блокнот": ("блокнот",),
    "ежедневник": ("ежедневник",),
    "кружка": ("кружка",),
    "бутылка": ("бутылка",),
    "термокружка": ("термокружка",),
    "термос": ("термос",),
    "сумка": ("сумка",),
    "рюкзак": ("рюкзак",),
    "зонт": ("зонт",),
    "плед": ("плед",),
    "брелок": ("брелок",),
    "шнурок": ("шнурок", "ланъярд", "ремувка"),
    "флешка": ("флеш", "usb"),
    "аккумулятор": ("аккумулятор", "power bank", "пауэрбанк"),
}

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


COLOR_FAMILIES = {
    "lime": ("лайм", "лаймов", "салатов", "зеленое яблоко", "яблочно зелен", "кислотно зелен"),
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


def _product_anchors(line):
    name = _normalized(line.get("name") if isinstance(line, dict) else "")
    for token in name.split():
        if token in PRODUCT_ANCHORS:
            return PRODUCT_ANCHORS[token]
    return tuple(token for token in name.split() if len(token) >= 4)[:3]


CATALOG_CLASS_ALIASES = {
    "футболка": ("футболка", "майка", "тенниска", "t shirt", "tshirt"),
    "жилет": ("жилет", "жилетка", "безрукавка"),
    "поло": ("поло", "футболка поло"),
    "кепка": ("кепка", "бейсболка"),
    "толстовка": ("толстовка", "худи", "свитшот"),
}


def _canonical_catalog_class(value):
    normalized = _normalized(value)
    for canonical, aliases in CATALOG_CLASS_ALIASES.items():
        if any(_normalized(alias) in normalized for alias in aliases):
            return canonical
    return normalized.split()[0] if normalized else ""


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
    product_class = _canonical_catalog_class(intent.get("product_class") if isinstance(intent, dict) else "")
    if not product_class:
        product_class = _canonical_catalog_class(line.get("name", ""))
    aliases = list(CATALOG_CLASS_ALIASES.get(product_class, (product_class,)))
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


def _fit_product(product, line, anchors, quantity):
    matches, mismatches, unknown = [], [], []
    product_text = _normalized(product.search_text)
    anchor_hit = next((value for value in anchors if _normalized(value) in product_text), "")
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
    if _meaningful_tokens(color_text):
        color_matches, color_family = _colors_compatible(color_text, product_color_text)
        if color_matches:
            family_note = f" (семейство: {color_family})" if color_family else ""
            matches.append(f"Цвет: {', '.join(color_values)}{family_note}")
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

    if quantity > 0:
        if product.total_stock >= quantity:
            matches.append(f"Остаток достаточен: {product.total_stock} шт.")
        elif product.is_on_order:
            mismatches.append(f"На складе {product.total_stock} из {quantity} шт.; товар доступен только под заказ")
        else:
            mismatches.append(f"Недостаточный остаток: {product.total_stock} из {quantity} шт.")

    name_score = SequenceMatcher(None, _normalized(line.get("name", "")), _normalized(product.full_name or product.name)).ratio()
    score = name_score * 30 + len(matches) * 12 - len(mismatches) * 35 - len(unknown) * 8
    if not mismatches:
        score += 40
    if product.effective_price is not None:
        score += 4
    return score, matches, mismatches, unknown


def catalog_candidates_for_line(line, limit=3, supplier_code="oasis", intent=None, client=None):
    """Return a relevance-ranked shortlist from live Oasis and cached suppliers."""
    try:
        quantity = int(Decimal(str(line.get("quantity") or 0).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        quantity = 0
    client = client or OasisClient()
    categories = _live_category_data(client)
    category = _live_category_for_intent(categories, intent or {}, line)
    if not category:
        return []
    category_map = {value["id"]: value["path"] or value["name"] for value in categories}
    fields = (
        "id,article,name,full_name,description,article_base,group_id,color_group_id,size,images,colors,"
        "categories,categories_array,brand,attributes,materials,branding,package,price,discount_price,"
        "total_stock,is_on_order,delivery_days,supply_terms,lead,defect,updated_at"
    )
    rows, seen_ids = [], set()
    # Oasis orders category results independently of relevance. Read bounded
    # pages instead of ranking only the arbitrary first 500 products.
    for offset in range(0, 1000, 500):
        payload = client.get("/v4/products", {
            "format": "json", "category": category["id"], "limit": 500, "offset": offset,
            "available": 1, "includeGroupId": 1, "fields": fields,
        })
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
    pool = [value for value in (
        _product_from_payload(supplier, raw, category_map, marker)
        for raw in rows if isinstance(raw, dict)
    ) if value and value.is_active]
    pool = _aggregate_color_variants(pool)
    aliases = CATALOG_CLASS_ALIASES.get(_canonical_catalog_class((intent or {}).get("product_class") or line.get("name", "")), ())
    if aliases:
        gifts_query = Q()
        for alias in aliases:
            gifts_query |= Q(search_text__icontains=alias)
        cached_products = CatalogProduct.objects.filter(
            Q(supplier__code="gifts") & Q(is_active=True) & gifts_query
        ).order_by("id")[:1500]
        pool.extend(cached_products)
    canonical_class = _canonical_catalog_class((intent or {}).get("product_class") or line.get("name", ""))
    anchors = CATALOG_CLASS_ALIASES.get(canonical_class, (canonical_class,))
    ranked = []
    for product in pool:
        score, matches, mismatches, unknown = _fit_product(product, line, anchors, quantity)
        if "Не совпадает тип товара" in mismatches:
            continue
        ranked.append((score, product, matches, mismatches, unknown))
    ranked.sort(key=lambda value: (not value[3], value[0]), reverse=True)
    selected, seen_groups = [], set()
    for score, product, matches, mismatches, unknown in ranked:
        group_key = product.group_id or product.external_id
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        price = product.effective_price
        product_url = product.product_url
        supplier_site = urlparse(product_url or supplier.base_url).netloc.lower()
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
            "category": category["path"] or category["name"],
            "sizes": product.raw_data.get("sizes", []) if isinstance(product.raw_data, dict) else [],
            "variant_ids": product.raw_data.get("variant_ids", []) if isinstance(product.raw_data, dict) else [],
        })
        if len(selected) >= max(1, min(10, limit)):
            break
    return selected
