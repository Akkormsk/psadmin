"""Разбор сырого извещения ЕИС (глубоко вложенный JSON) в плоскую структуру для карточки.

Формат отличается по типу закупки (аукцион / запрос котировок / конкурс) и часто
сериализует одноэлементные списки как объект — поэтому всё защищено `_dig` / `_as_list`.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dig(obj, *keys, default=None):
    for key in keys:
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def _dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # API отдаёт время без пометки зоны — это UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _person(info: dict) -> str:
    parts = [info.get("lastName"), info.get("firstName"), info.get("middleName")]
    return " ".join(p for p in parts if p)


def _characteristics(okpd_or_ktru: dict) -> list[dict]:
    chars = _dig(okpd_or_ktru, "characteristics", default={}) or {}
    out = []
    for group in ("characteristicsUsingReferenceInfo", "characteristicsUsingTextForm"):
        for ch in _as_list(chars.get(group)):
            name = ch.get("name", "")
            value = _dig(ch, "values", "value", "qualityDescription", default="")
            if not value:
                value = ", ".join(
                    v.get("qualityDescription", "") for v in _as_list(_dig(ch, "values", "value"))
                )
            if name or value:
                out.append({"name": name, "value": value})
    return out


def _items(source: dict) -> list[dict]:
    raw_items = _dig(
        source,
        "notificationInfo", "purchaseObjectsInfo", "notDrugPurchaseObjectsInfo", "purchaseObject",
        default=[],
    )
    items = []
    for it in _as_list(raw_items):
        classifier = it.get("OKPD2") or it.get("KTRU") or {}
        code = classifier.get("OKPDCode") or classifier.get("code") or ""
        code_name = classifier.get("OKPDName") or classifier.get("name") or ""
        items.append(
            {
                "name": it.get("name") or code_name,
                "code": code,
                "code_name": code_name,
                "quantity": _dig(it, "quantity", "value", default=""),
                "unit": _dig(it, "OKEI", "nationalCode", default=""),
                "price": it.get("price", ""),
                "sum": it.get("sum", ""),
                "characteristics": _characteristics(classifier),
            }
        )
    return items


def _documents(source: dict) -> list[dict]:
    docs = []
    for att in _as_list(_dig(source, "attachmentsInfo", "attachmentInfo", default=[])):
        url = att.get("url", "")
        if not url.startswith("https://zakupki.gov.ru/"):
            continue
        try:
            size_kb = round(int(att.get("fileSize", 0)) / 1024)
        except (TypeError, ValueError):
            size_kb = None
        docs.append(
            {
                "name": att.get("fileName") or att.get("docDescription") or "Документ",
                "kind": _dig(att, "docKindInfo", "name", default=""),
                "size_kb": size_kb,
                "date": _dt(att.get("docDate")),
                "url": url,
            }
        )
    return docs


def parse_notification(payload: dict) -> dict:
    """payload — весь ответ /notification ({doc_type, published_at, source})."""
    source = payload.get("source") or {}
    common = source.get("commonInfo") or {}
    resp_org = _dig(source, "purchaseResponsibleInfo", "responsibleOrgInfo", default={}) or {}
    resp_info = _dig(source, "purchaseResponsibleInfo", "responsibleInfo", default={}) or {}
    cr = _dig(source, "notificationInfo", "customerRequirementsInfo", "customerRequirementInfo", default={}) or {}
    proc = _dig(source, "notificationInfo", "procedureInfo", default={}) or {}
    contract_conditions = cr.get("contractConditionsInfo") or {}

    return {
        "doc_type": payload.get("doc_type", ""),
        "title": common.get("purchaseObjectInfo", ""),
        "eis_href": common.get("href", ""),
        "print_form_url": _dig(source, "printFormInfo", "url", default=""),
        "etp": {
            "name": _dig(common, "ETP", "name", default=""),
            "url": _dig(common, "ETP", "url", default=""),
        },
        "placing_way": _dig(common, "placingWay", "name", default=""),
        "customer": {
            "name": resp_org.get("fullName", "") or _dig(cr, "customer", "fullName", default=""),
            "short_name": resp_org.get("shortName", ""),
            "inn": resp_org.get("INN", ""),
            "kpp": resp_org.get("KPP", ""),
            "post_address": resp_org.get("postAddress", ""),
            "fact_address": resp_org.get("factAddress", ""),
            "contact_person": _person(_dig(resp_info, "contactPersonInfo", default={}) or {}),
            "email": resp_info.get("contactEMail", ""),
            "phone": resp_info.get("contactPhone", ""),
        },
        "dates": {
            "collect_start": _dt(_dig(proc, "collectingInfo", "startDT")),
            "collect_end": _dt(_dig(proc, "collectingInfo", "endDT")),
            "bidding": _dt(proc.get("biddingDate")),
            "summarizing": _dt(proc.get("summarizingDate")),
            "execution_end": _dt(
                _dig(contract_conditions, "contractExecutionPaymentPlan", "contractExecutionTermsInfo",
                     "notRelativeTermsInfo", "endDate")
            ),
        },
        "money": {
            "max_price": _dig(source, "notificationInfo", "contractConditionsInfo", "maxPriceInfo", "maxPrice")
            or _dig(contract_conditions, "maxPriceInfo", "maxPrice", default=""),
            "currency": _dig(source, "notificationInfo", "contractConditionsInfo", "maxPriceInfo", "currency", "name",
                             default="") or "",
            "app_guarantee_amount": _dig(cr, "applicationGuarantee", "amount", default=""),
            "app_guarantee_part": _dig(cr, "applicationGuarantee", "part", default=""),
            "contract_guarantee_part": _dig(cr, "contractGuarantee", "part", default=""),
            "treasury_support": _dig(
                contract_conditions, "bankSupportContractRequiredInfo", "treasurySupportContractInfo",
                "treasurySupportContractRequired", default=""
            ) == "true",
        },
        "delivery_address": _dig(
            contract_conditions, "deliveryPlacesInfo", "byGARInfo", "GARInfo", "GARAddress", default=""
        ) or _dig(contract_conditions, "deliveryPlacesInfo", "byKLADRInfo", "KLADRInfo", "KLADRAddress", default=""),
        "items": _items(source),
        "documents": _documents(source),
    }


# --- Разъяснения и жалобы (отдельные разделы карточки) -------------------------

# doc_type у разъяснения — сырой код ЕИС, префикс зависит от типа процедуры.
_CLAR_LABELS = (
    ("clarificationofdocumentation", "Разъяснение документации"),
    ("explanationsofprovisions", "Разъяснение положений извещения"),
    ("explanationofprovision", "Разъяснение положений извещения"),
    ("clarificationofresults", "Разъяснение результатов"),
    ("clarification", "Разъяснение"),
    ("explanation", "Разъяснение"),
)
_QUESTION_KEYS = ("question", "request", "questiontext", "questioncontent", "questionsubject", "subject", "topic")
_ANSWER_KEYS = (
    "answer", "explanation", "response", "clarification", "answertext", "answercontent",
    "explanationtext", "explanationcontent", "responsetext", "content", "text",
)


def _label(doc_type: str, table) -> str:
    low = (doc_type or "").lower()
    for key, label in table:
        if key in low:
            return label
    return "Разъяснение"


def _collect_texts(obj, min_len: int = 12):
    """Рекурсивно собрать (ключ в нижнем регистре, текст) по строковым листьям."""
    found: list[tuple[str, str]] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, value in node.items():
                walk(value, k)
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif isinstance(node, str):
            text = node.strip()
            if len(text) >= min_len and not text.lower().startswith(("http://", "https://")):
                found.append((key.lower(), text))

    walk(obj)
    return found


def _pick(texts, hints, avoid=()) -> str:
    for key, text in texts:
        if avoid and any(hint in key for hint in avoid):
            continue
        if any(hint in key for hint in hints):
            return text
    return ""


def parse_clarifications(items) -> list[dict]:
    """Сырой ответ /clarifications → список для карточки (новые сверху).

    Формат source зависит от типа документа и не документирован — вопрос/ответ ищем
    и по известным ключам, и общим сканом текста; всегда отдаём дату, тип и ссылки.
    """
    out = []
    for it in _as_list(items):
        if not isinstance(it, dict):
            continue
        src = it.get("source") or {}
        common = src.get("commonInfo") or {}
        texts = _collect_texts(src)
        question = _pick(texts, _QUESTION_KEYS)
        answer = _pick(texts, _ANSWER_KEYS, avoid=_QUESTION_KEYS)
        if not question and not answer:
            longs = [t for _, t in texts if len(t) > 40]
            if len(longs) == 1:
                answer = longs[0]
            elif len(longs) >= 2:
                question, answer = longs[0], longs[1]
        if answer and answer == question:
            question = ""
        out.append({
            "doc_type": it.get("doc_type", ""),
            "label": _label(it.get("doc_type", ""), _CLAR_LABELS),
            "published_at": _dt(it.get("published_at")),
            "number": common.get("docNumber", ""),
            "href": common.get("href", "") or _dig(src, "foundationDocInfo", "href", default=""),
            "print_url": _dig(src, "extPrintFormInfo", "url", default="")
            or _dig(src, "printFormInfo", "url", default=""),
            "question": question,
            "answer": answer,
        })
    out.sort(key=lambda c: c["published_at"].isoformat() if c["published_at"] else "", reverse=True)
    return out


_COMPLAINT_DOC_LABELS = {
    "complaint": "Жалоба",
    "complaintdecision": "Решение по жалобе",
    "complaintconsideration": "Рассмотрение жалобы",
    "complaintreturn": "Возврат жалобы",
    "complaintwithdrawal": "Отзыв жалобы",
    "complaintredirection": "Перенаправление жалобы",
    "tendersuspension": "Приостановка торгов",
    "unscheduledinspection": "Внеплановая проверка",
    "unscheduledinspectiondecision": "Решение по проверке",
}
_COMPLAINT_OBJECTS = {
    "purchase": "закупка",
    "order": "заказ",
    "sketchPlan": "эскизный проект",
    "tenderPlan": "план закупок",
}


def parse_complaints(items) -> list[dict]:
    """Сырой ответ /complaints → список для карточки (новые сверху).

    Источник отдаёт только метаданные: рег. номер, дата, тип документа, предмет —
    без текста жалобы и без файлов (их можно посмотреть только на ЕИС).
    """
    out = []
    for it in _as_list(items):
        if not isinstance(it, dict):
            continue
        kinds, seen = [], set()
        for doc in _as_list(it.get("docs")):
            code = (doc.get("doc_type") or "").lower()
            label = _COMPLAINT_DOC_LABELS.get(code, doc.get("doc_type") or "Документ")
            if label not in seen:
                seen.add(label)
                kinds.append(label)
        out.append({
            "reg_number": it.get("reg_number", ""),
            "published_at": _dt(it.get("published_at")),
            "updated_at": _dt(it.get("updated_at")),
            "object": _COMPLAINT_OBJECTS.get(it.get("object"), it.get("object") or ""),
            "kinds": kinds,
        })
    out.sort(key=lambda c: c["published_at"].isoformat() if c["published_at"] else "", reverse=True)
    return out
