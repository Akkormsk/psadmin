"""Evidence-backed item annotations for the existing document review and risk flow."""
import json
import re
from copy import deepcopy

FIELDS = {
    "isProhibitionForeignPurchaseObjects": "ban",
    "isRestrictForeignPurchaseObjects": "restriction",
    "isPreferenseRFPurchaseObjects": "advantage",
}
LABELS = {"ban": ("red", "запрет", "Заказчик указал запрет на иностранные товары."),
          "restriction": ("yellow", "ограничение / подтверждение", "Заказчик указал ограничение или подтверждение происхождения/реестра."),
          "advantage": ("green", "преимущество", "Заказчик указал преимущество российским товарам."),
          "none": ("green", "без специальных требований", "Отсутствие специальных требований подтверждено источником."),
          "unknown": ("gray", "не указано", "В сохранённых сведениях требования по этому товару не указаны.")}


def annotation(status="unknown", basis=None, description=None):
    color, label, default = LABELS.get(status, LABELS["unknown"])
    return {"status": status if status in LABELS else "unknown", "color": color, "label": "1875: " + label,
            "description": description or default, "basis": basis or []}


def structured_item(raw, path, source_url=""):
    restrictions = raw.get("restrictionsInfo") or {}
    if not isinstance(restrictions, dict):
        return annotation()
    flags, basis = {}, []
    captions = {"ban": "Запрет на иностранные товары", "restriction": "Ограничение допуска иностранных товаров", "advantage": "Преимущество российским товарам"}
    for field, status in FIELDS.items():
        value = restrictions.get(field)
        text = str(value).casefold()
        if text not in {"true", "false"}:
            continue
        flags[status] = text == "true"
        basis.append({"kind": "notification", "path": path + ".restrictionsInfo." + field,
                      "value": value, "quote": field + " = " + json.dumps(value, ensure_ascii=False),
                      "human_text": captions[status] + (": да" if text == "true" else ": нет"),
                      "source_url": source_url if isinstance(source_url, str) and source_url.startswith("https://zakupki.gov.ru/") else ""})
    if flags.get("ban"):
        status = "ban"
    elif flags.get("restriction"):
        status = "restriction"
    elif flags.get("advantage"):
        status = "advantage"
    elif flags == {"ban": False, "restriction": False, "advantage": False} and not any(str(value).casefold() not in {"false", "none", "", "[]", "{}"} for key, value in restrictions.items() if key not in FIELDS):
        status = "none"
    else:
        status = "unknown"
    return annotation(status, basis)


def _text(value):
    return " ".join(str(value or "").split()).casefold().replace("ё", "е")


def document_item(value, documents, *, name, position=None, component=False):
    """Only literal, source-backed assertions; scanned-only quotes remain unverified."""
    if not isinstance(value, dict) or not isinstance(value.get("status"), str) or value.get("status") not in LABELS or value["status"] == "unknown":
        return annotation()
    status, accepted = value["status"], []
    for evidence in value.get("basis", []) if isinstance(value.get("basis"), list) else []:
        if not isinstance(evidence, dict):
            continue
        quote, url = str(evidence.get("quote") or ""), evidence.get("source_url")
        if not isinstance(url, str):
            continue
        source = documents.get(url)
        text = _text(quote)
        if not source or not 8 <= len(quote) <= 2500 or text not in _text(source.get("text")) or not re.search(r"\b1875\b", text):
            continue
        all_items = evidence.get("scope") == "all_items" and bool(re.search(r"\b(?:все|всех)\s+(?:(?:поставляем|закупаем)\w*\s+)?товар", text)) and not re.search(r"не\s+все", text)
        named = bool(_text(name)) and _text(name) in text
        numbered = not component and type(position) is int and evidence.get("item_position") == position and bool(re.search(r"позици[яию]\s*№?\s*" + str(position) + r"\b", text))
        if not (all_items or named or numbered):
            continue
        negative = bool(re.search(r"(?:не\s+(?:установлен\w*|применя\w*|предусмотрен\w*)|(?:запрет\w*|ограничени\w*|преимуществ\w*)\s+(?:не\s+|отсутству\w*))", text))
        supported = ((status == "ban" and "запрет" in text and not negative) or
                     (status == "restriction" and not negative and ("ограничени" in text or (re.search(r"требуется|должен|необходимо|подтверд|предостав", text) and re.search(r"происхожден|реестр", text)))) or
                     (status == "advantage" and "преимуще" in text and not negative) or
                     (status == "none" and bool(re.search(r"(?:требовани\w*\s+(?:по\s+)?(?:пп\s*)?1875\s+не\s+(?:установлен|применя)|национальн\w*\s+режим\w*\s+не\s+применя|запрет\w*,?\s+ограничени\w*\s+и\s+преимуществ\w*\s+не\s+установлен)", text))))
        if supported:
            accepted.append({"kind": "document", "quote": quote, "source_url": url,
                             "page_or_section": str(evidence.get("page_or_section") or "")[:300]})
    return annotation(status, accepted) if accepted else annotation()


def resolve_item(native=None, documented=None, *, component=False):
    native = native if isinstance(native, dict) and isinstance(native.get("status"), str) and native.get("status") in LABELS else annotation()
    documented = documented if isinstance(documented, dict) and isinstance(documented.get("status"), str) and documented.get("status") in LABELS else annotation()
    if component and native["status"] != "unknown":
        native = annotation(basis=native["basis"], description="Требование указано для исходного набора; применимость к составляющей требует проверки.")
    known = [item for item in (native, documented) if item["status"] != "unknown" and item.get("basis")]
    if not known:
        return native if native.get("basis") else annotation()
    if any(item["status"] == "none" for item in known) and any(item["status"] != "none" for item in known):
        return annotation(basis=[basis for item in known for basis in item["basis"]], description="Источники расходятся; требуется проверка.")
    priority = {"ban": 3, "restriction": 2, "advantage": 1, "none": 0}
    return annotation(max(known, key=lambda item: priority[item["status"]])["status"], [basis for item in known for basis in item["basis"]])


def notification_item_regime(source, card=None):
    """Use the saved source revision; legacy rows retain their existing reader."""
    from tenders.document_enrichment import notification_origin
    origin = notification_origin(source)
    if origin and "notification_national_regime" in (origin.metadata or {}):
        return deepcopy(origin.metadata["notification_national_regime"])
    from .notification import parse_notification
    card = card if card is not None else parse_notification(source.tender.notification_raw or {})
    index = (origin.metadata or {}).get("notification_index") if origin else None
    items = card.get("items", [])
    return items[index].get("national_regime") if type(index) is int and 0 <= index < len(items) else annotation()


def source_item_status(source, card=None):
    return resolve_item(notification_item_regime(source, card), source.metadata.get("national_regime"),
                        component=source.metadata.get("relationship") == "component")


def tender_items(tender, card=None):
    from .notification import parse_notification
    from tenders.calculation_v2_pipeline import active_calculation_items
    from tenders.document_enrichment import notification_origin
    card = card if card is not None else parse_notification(tender.notification_raw or {})
    original = card.get("items", [])
    rows, groups = [], set()
    for source in active_calculation_items(tender):
        origin = notification_origin(source)
        component = source.metadata.get("relationship") == "component"
        rows.append({"source_id": source.pk, "name": source.original_text, "regime": source_item_status(source, card)})
        if component and origin and origin.pk not in groups:
            groups.add(origin.pk)
            native = notification_item_regime(origin, card)
            rows.append({"group_id": origin.pk, "name": source.metadata.get("parent_display_name") or origin.original_text, "regime": native or annotation()})
    return rows or [{"position": index, "name": item.get("name", "Товар"), "regime": item.get("national_regime") or annotation()} for index, item in enumerate(original)]


def _national_review_complete(tender, assessment):
    """Confirm saved coverage for current inputs; never run document processing."""
    if assessment.get('stale') or assessment.get('degraded'):
        return False
    from .notification import parse_notification
    if not parse_notification(tender.notification_raw or {}).get('documents'):
        return False
    from tenders.models import TenderComputeJob
    from .services import _document_input_fingerprint
    job = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').first()
    if job is None:
        return False
    diagnostics = job.diagnostics or {}
    stages = diagnostics.get('stages') or {}
    if any(stages.get(name, {}).get('state') != 'complete'
           or stages.get(name, {}).get('outcome') == 'source_only'
           for name in ('documents', 'national_regime')):
        return False
    if any(gap.get('stage') in {'documents', 'source_reading', 'source_binding', 'national_regime'}
           for gap in diagnostics.get('analysis_gaps', [])):
        return False
    reading = diagnostics.get('source_reading') or {}
    if reading and not reading.get('complete'):
        return False
    return diagnostics.get('document_input_fingerprint') == _document_input_fingerprint(tender)


def risk_projection(tender, assessment=None, card=None):
    """Read-only overlay: no manual review decisions or accepted facts are rewritten."""
    data = deepcopy(assessment or {})
    items = tender_items(tender, card)
    checked = _national_review_complete(tender, data)
    missing = [item for item in items if item['regime']['status'] == 'unknown' and not item['regime'].get('basis')]
    conflicts = [item for item in items if item['regime']['status'] == 'unknown' and item['regime'].get('basis')]
    factors = [factor for factor in (data.get("risk_factors") or []) if factor.get("code") != "national_regime" and not str(factor.get("code", "")).startswith("item_1875_")]
    from .models import FilterSettings
    levels = FilterSettings.objects.filter(pk=1).values_list("risk_factor_levels", flat=True).first() or {}
    for status, code, level, caption in [("ban", "ban", levels.get("national_blocked", "high"), "Запрет заказчика"), ("restriction", "confirmation", levels.get("national_confirmation", "medium"), "Ограничение / подтверждение происхождения")]:
        selected = [item for item in items if item["regime"]["status"] == status and (status == "unknown" or item["regime"].get("basis"))]
        if selected:
            factors.append({"code": "item_1875_" + code, "level": level, "text": caption + ": " + "; ".join(item["name"] for item in selected), "items": selected})
    if conflicts:
        factors.append({'code': 'item_1875_unknown', 'level': 'unknown',
                        'text': 'Применимость требований 1875 требует уточнения: ' + '; '.join(item['name'] for item in conflicts),
                        'items': conflicts})
    rank = {"low": 0, "unknown": 0, "medium": 1, "high": 2}
    severity = max((factor["level"] for factor in factors), key=lambda value: rank.get(value, 0), default="low")
    if severity == "low" and (data.get('stale') or (conflicts or (missing and not checked)) or not items or str((data.get("risk_facts") or {}).get("documents_sufficient")).lower() != "true"):
        severity = "unknown"
    facts = deepcopy(data.get("risk_facts") or {})
    statuses = {item["regime"]["status"] for item in items}
    facts["national_regime"] = "blocked" if "ban" in statuses else "confirmation_required" if "restriction" in statuses else "unknown" if not statuses or "unknown" in statuses else "none"
    facts["national_regime_items"] = items
    data["risk_facts"] = facts
    labels = dict.fromkeys(item['regime']['label'].removeprefix('1875: ') for item in items if item['regime']['status'] != 'unknown')
    summary = '; '.join(labels)
    if missing or not items:
        absence = ('Требования по ПП №1875 в проверенных материалах не указаны.' if checked
                   else 'Сведения по ПП №1875 не указаны. Проверка документов не завершена.')
        if summary:
            absence = 'Для остальных товаров: ' + absence[0].lower() + absence[1:]
        summary = (summary + '. ' if summary else '') + absence
    if conflicts:
        summary = (summary + ' ' if summary else '') + 'Есть сведения, требующие уточнения.'
    data.update(risk_factors=factors, risk_level=severity, national_regime=summary,
                national_regime_checked=checked,
                national_regime_details=[item for item in items if item['regime'].get('basis')])

    return data
