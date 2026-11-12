"""Direct EIS XML adapter. Missing registries are errors, never empty successes."""
import io
import zipfile
from collections import defaultdict
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from django.core.cache import cache

from . import eis_docs


def _name(node):
    return node.tag.rsplit("}", 1)[-1]


def _object(node):
    children = list(node)
    if not children:
        return (node.text or "").strip()
    groups = defaultdict(list)
    for child in children:
        groups[_name(child)].append(_object(child))
    return {key: values[0] if len(values) == 1 else values for key, values in groups.items()}


def parse_package(archives):
    records = []
    total = 0
    for data in archives:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise eis_docs.EisDocsError("ЕИС вернул некорректный архив XML") from exc
        with archive:
            for info in archive.infolist():
                if not info.filename.lower().endswith(".xml"):
                    continue
                total += info.file_size
                if total > eis_docs.MAX_ARCHIVE_BYTES:
                    raise eis_docs.EisDocsError("XML ЕИС превышает безопасный размер разбора")
                raw = archive.read(info)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise eis_docs.EisDocsError("Небезопасное объявление в XML ЕИС")
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError as exc:
                    raise eis_docs.EisDocsError("Некорректный XML в архиве ЕИС") from exc
                # Export wrappers contain individual versioned EIS records.
                nodes = list(root) if _name(root).lower() in {"export", "documents"} else [root]
                for node in nodes:
                    source = _object(node)
                    if not isinstance(source, dict):
                        continue
                    common = source.get("commonInfo") or {}
                    published = common.get("publishDTInEIS") or common.get("publishDate") or source.get("publishDate") or ""
                    records.append({"doc_type": _name(node), "published_at": published, "source": source})
    return records


def records_for(number):
    key = f"eis-xml-package:{number}"
    existing = cache.get(key)
    if existing is not None:
        return existing
    urls = eis_docs.fetch_archive_urls(number)
    archives = []
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".zakupki.gov.ru"):
            raise eis_docs.EisDocsError("ЕИС вернул архив за пределами доверенного домена")
        archives.append(eis_docs.download_archive(url))
    records = parse_package(archives)
    if not records:
        raise eis_docs.EisDocsError("Архив ЕИС не содержит структурированных записей; полнота данных не подтверждена")
    cache.set(key, records, timeout=60)
    return records


def notification(number):
    rows = [r for r in records_for(number) if "notification" in r["doc_type"].lower() and isinstance(r["source"].get("commonInfo"), dict)]
    if not rows:
        raise eis_docs.EisDocsError("ЕИС не вернул структурированное извещение; прежние данные сохранены")
    def version(row):
        source = row["source"]
        try:
            revision = int(source.get("versionNumber") or source.get("version") or 0)
        except (ValueError, TypeError):
            revision = 0
        return revision, str(row["published_at"])
    return max(rows, key=version)


def related(number, kind):
    terms = {"clarifications": ("clarification", "explanation"), "complaints": ("complaint",)}[kind]
    rows = [r for r in records_for(number) if any(term in r["doc_type"].lower() for term in terms)]
    if not rows:
        raise eis_docs.EisDocsError("Полнота раздела ЕИС не подтверждена; сохранённый раздел не очищен")
    if kind == "complaints":
        normalized = []
        for row in rows:
            source = row["source"]
            number = source.get("regNumber") or source.get("registrationNumber")
            if not isinstance(number, str) or not number:
                raise eis_docs.EisDocsError("Формат реестровой записи жалобы ЕИС ещё не подтверждён")
            normalized.append({"reg_number": number, "published_at": row["published_at"],
                               "object": source.get("object") if isinstance(source.get("object"), str) else "",
                               "docs": [row]})
        return normalized
    return rows


def purchase(number):
    from .notification import parse_notification
    payload = notification(number)
    card = parse_notification(payload)
    source = payload["source"]
    common = source.get("commonInfo") or {}
    actual_number = str(common.get("purchaseNumber") or source.get("purchaseNumber") or "")
    if actual_number != str(number):
        raise eis_docs.EisDocsError("ЕИС не подтвердил номер запрошенной закупки")
    deadline = (card.get("dates") or {}).get("collect_end")
    return {"purchase_number": actual_number, "object_info": card.get("title", ""),
            "max_price": (card.get("money") or {}).get("max_price"), "purchase_type": payload["doc_type"],
            "published_at": payload["published_at"],
            "collecting_finished_at": deadline.isoformat() if hasattr(deadline, "isoformat") else deadline,
            "customers": [(card.get("customer") or {}).get("inn")] if (card.get("customer") or {}).get("inn") else [],
            "okpd2": sorted({item["code"] for item in card.get("items", []) if item.get("code")})}


def organization(inn, law):
    from .models import Tender
    # Reuse authoritative organization details already received in notices.
    for payload in Tender.objects.filter(customer_inn=inn, law=law).exclude(notification_raw={}).values_list("notification_raw", flat=True):
        source = payload.get("source") or {}
        info = (source.get("purchaseResponsibleInfo") or {}).get("responsibleOrgInfo") or {}
        if str(info.get("INN") or "") == str(inn) and info.get("fullName"):
            return info
    raise eis_docs.EisDocsError("Карточка организации ещё не получена напрямую из ЕИС")


def contracts(params):
    # Broad historical sampling has no verified direct-EIS replacement yet.
    # Do not spend discovery quota or invent a contract from a bid protocol.
    raise eis_docs.EisDocsError("Реестр контрактов напрямую из ЕИС ещё не подтверждён; сохранённая статистика доступна")
