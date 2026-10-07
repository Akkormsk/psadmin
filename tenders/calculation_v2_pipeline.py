"""Milestone 2: durable tender understanding and tender-wide batch routing."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re
import time
import os
from typing import Protocol

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from tender_selection.filtering import match_title, parse_terms
from tender_selection.models import DocumentPreview, FilterSettings, Tender
from tender_selection.notification import parse_notification

from .calculation_v2 import claim_next_job
from .document_enrichment import document_table_payload
from .models import (
    KnowledgeRecord,
    Lesson,
    ProcessDefinition,
    OwnerInteraction,
    TenderComputeJob,
    TenderComputeLine,
    TenderSourceItem,
    TenderCommercialItem,
    CalculationComponent,
    ComponentRoutePlan,
    ComponentOperationStep,
)


class BatchRouter(Protocol):
    def route(self, *, tender: Tender, items: list[CalculationComponent], processes: list[ProcessDefinition], knowledge: list[dict]) -> list["RouteDecision"]: ...


class DocumentEnricher(Protocol):
    def extract(self, *, tender: Tender, aggregate: TenderSourceItem, documents: list[dict]) -> list["ExtractedItem"]: ...


@dataclass(frozen=True)
class RouteDecision:
    component_id: int
    process_ids: tuple[int, ...]
    confidence: Decimal | None
    rationale: dict
    needs_review: bool = False
    question: str = ""


@dataclass(frozen=True)
class ExtractedItem:
    name: str
    quantity: Decimal | None
    unit: str
    requirements: dict
    provenance: dict
    confidence: Decimal | None
    relationship: str = "split"
    quantity_per_parent: Decimal = Decimal("1")


class ExistingKnowledgeBatchRouter:
    """Safe default: consumes existing route knowledge but never invents a route."""

    def route(self, *, tender, items, processes, knowledge):
        decisions = []
        active_by_id = {process.pk: process for process in processes}
        for item in items:
            normalized = _normal(item.name)
            matches = []
            for entry in knowledge:
                if entry["needle"] and entry["needle"] in normalized:
                    matches.extend(entry["process_ids"])
            process_ids = sorted({process_id for process_id in matches if process_id in active_by_id})
            if len(process_ids) == 1:
                decisions.append(RouteDecision(item.pk, (process_ids[0],), Decimal("0.9000"), {"source": "existing_knowledge"}))
            else:
                reason = "No applicable route knowledge" if not process_ids else "Conflicting route knowledge"
                decisions.append(RouteDecision(item.pk, (), Decimal("0.0000"), {"reason": reason}, True, "Уточните способ выполнения позиции."))
        return decisions


class NoopDocumentEnricher:
    def extract(self, *, tender, aggregate, documents):
        return []


class GatewayBatchRouter:
    """One structured gateway request for all active items in a Tender."""
    def __init__(self, model=None):
        self.model = model or os.getenv("TIMEWEB_AI_V2_ROUTER_MODEL", "gemini/gemini-3.1-flash-lite")
        self.usage = {}
        self.cost_rub = 0.0

    def route(self, *, tender, items, processes, knowledge):
        from .gateway_budget import spend_rub
        from .services import _ai_gateway_json
        payload = {
            "tender": {"id": tender.pk, "title": tender.title, "subject": tender.object_info},
            "items": [{"component_id": item.pk, "name": item.name, "quantity": str(item.effective_quantity), "unit": item.unit, "requirements": item.requirements} for item in items],
            "processes": [{"id": process.pk, "name": process.name, "description": process.description, "when_to_use": process.when_to_use, "when_not_to_use": process.when_not_to_use} for process in processes],
            "knowledge": knowledge,
        }
        prompt = """Route every tender item in one batch. Use only a listed process id. Do not search, price, or calculate. Return JSON only: {\"items\":[{\"source_item_id\":integer,\"process_id\":integer|null,\"confidence\":number 0..1,\"reason\":string,\"alternatives\":[integer],\"needs_review\":boolean,\"question\":string}]}. A question is allowed only for material uncertainty. Context data follows:\n""" + json.dumps(payload, ensure_ascii=False, default=str)
        raw, self.usage = _ai_gateway_json(prompt, model=self.model, max_tokens=4000, timeout=90, network_attempts=2)
        self.cost_rub = float(spend_rub(self.usage, self.model) or 0)
        rows = raw.get("items") if isinstance(raw, dict) else None
        rows = rows if isinstance(rows, list) else []
        process_ids = {process.pk for process in processes}
        output, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = row.get("component_id")
            if item_id not in {item.pk for item in items} or item_id in seen:
                continue
            seen.add(item_id)
            raw_process_ids = row.get("process_ids")
            if not isinstance(raw_process_ids, list):
                raw_process_ids = [row.get("process_id")]
            selected_process_ids = tuple(value for value in raw_process_ids if isinstance(value, int) and value in process_ids)
            valid = bool(selected_process_ids) and len(selected_process_ids) == len(raw_process_ids)
            confidence = _as_decimal(row.get("confidence"))
            confidence = confidence if confidence is not None and Decimal("0") <= confidence <= Decimal("1") else Decimal("0")
            needs_review = bool(row.get("needs_review")) or not valid
            output.append(RouteDecision(item_id, selected_process_ids if valid else (), confidence, {"reason": str(row.get("reason") or "")[:1000], "alternatives": row.get("alternatives") if isinstance(row.get("alternatives"), list) else []}, needs_review, str(row.get("question") or "Уточните способ выполнения позиции.")[:500]))
        for item in items:
            if item.pk not in seen:
                output.append(RouteDecision(item.pk, (), Decimal("0"), {"reason": "Model omitted item"}, True, "Нужна проверка маршрута позиции."))
        return output


class GatewayDocumentEnricher:
    """Triage cached document text, then extract structured items from relevant documents."""
    def __init__(self, model=None):
        self.model = model or os.getenv("TIMEWEB_AI_V2_EXTRACTION_MODEL", os.getenv("TIMEWEB_AI_MODEL", "gemini/gemini-3.1-pro-preview"))
        self.usage = {}
        self.cost_rub = 0.0
        self.diagnostics = {}

    def extract(self, *, tender, aggregate, documents):
        from .gateway_budget import spend_rub
        from .services import _ai_gateway_json
        started = time.monotonic()
        self.diagnostics = {}
        available = []
        for doc in documents:
            if not doc.get("url"):
                continue
            html = (DocumentPreview.objects.filter(url=doc["url"]).values_list("html", flat=True).first() or "")[:30000]
            available.append({"url": doc["url"], "name": doc.get("name", ""), "text": html, "tables": document_table_payload(html)})
        if not any(doc["text"] for doc in available):
            self.diagnostics = {"outcome": "system_extraction_failure", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [], "skipped_reason": "document_preview_missing"}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        triage_prompt = "Return JSON only {\"relevant_urls\":[string]}. Select only documents containing an item/specification/table. Documents:\n" + json.dumps(available, ensure_ascii=False)
        triage, triage_usage = _ai_gateway_json(triage_prompt, model=self.model, max_tokens=800, timeout=90, network_attempts=2)
        urls = set(triage.get("relevant_urls", [])) if isinstance(triage, dict) else set()
        selected = [doc for doc in available if doc["url"] in urls and doc["text"]]
        if not selected:
            self.usage = triage_usage
            self.cost_rub = float(spend_rub(triage_usage, self.model) or 0)
            self.diagnostics = {"outcome": "no_data", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "cost_rub": str(self.cost_rub), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [], "skipped_reason": "no_relevant_document_selected", "usage": triage_usage}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        prompt = """Extract only explicitly evidenced tender items. Return JSON only {\"items\":[{\"name\":string,\"quantity\":number|null,\"unit\":string,\"requirements\":object,\"confidence\":number,\"source_url\":string,\"page_or_section\":string,\"evidence\":string}]}. Do not invent missing facts. Preserve all explicit requirements. If documentation decomposes one aggregate into components, grades, variants, or separately named goods, emit one row for every distinct product; never collapse them back into one generic row. Do not copy a requirement that applies only to the aggregate into every child.\n""" + json.dumps({"aggregate": aggregate.original_text, "documents": selected}, ensure_ascii=False)
        raw, extract_usage = _ai_gateway_json(prompt, model=self.model, max_tokens=5000, timeout=120, network_attempts=2)
        self.usage = {key: (triage_usage.get(key, 0) or 0) + (extract_usage.get(key, 0) or 0) for key in {**triage_usage, **extract_usage}}
        self.cost_rub = float(spend_rub(self.usage, self.model) or 0)
        output, rejections = [], []
        allowed_urls = {doc["url"] for doc in selected}
        for index, row in enumerate(raw.get("items", []) if isinstance(raw, dict) else []):
            if not isinstance(row, dict):
                rejections.append({"index": index, "reason": "not_object"})
                continue
            if not str(row.get("name") or "").strip():
                rejections.append({"index": index, "reason": "missing_name"})
                continue
            if row.get("source_url") not in allowed_urls:
                rejections.append({"index": index, "reason": "invalid_source_url"})
                continue
            confidence = _as_decimal(row.get("confidence"))
            if confidence is None or not (Decimal("0") <= confidence <= Decimal("1")):
                rejections.append({"index": index, "reason": "invalid_confidence"})
                continue
            requirements = row.get("requirements")
            if not isinstance(requirements, dict):
                rejections.append({"index": index, "reason": "invalid_requirements"})
                continue
            relationship = str(row.get("relationship") or "split").lower()
            if relationship not in {"enrich", "split", "component"}:
                rejections.append({"index": index, "reason": "invalid_relationship"})
                continue
            per_parent = _as_decimal(row.get("quantity_per_parent")) or Decimal("1")
            if per_parent <= 0:
                rejections.append({"index": index, "reason": "invalid_quantity_per_parent"})
                continue
            output.append(ExtractedItem(str(row["name"])[:1000], _as_decimal(row.get("quantity")), str(row.get("unit") or "")[:64], requirements, {"document_url": row["source_url"], "page_or_section": str(row.get("page_or_section") or "")[:300], "evidence": str(row.get("evidence") or "")[:1000], "extraction_model": self.model, "extraction_version": "document-enrichment-v1"}, confidence, relationship, per_parent))
        outcome = "success" if output else ("validation_failure" if rejections else "system_extraction_failure")
        self.diagnostics = {"outcome": outcome, "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "cost_rub": str(self.cost_rub), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [doc["url"] for doc in selected], "usage": triage_usage}, "extraction": {"selected_urls": [doc["url"] for doc in selected], "raw_item_count": len(raw.get("items", [])) if isinstance(raw, dict) and isinstance(raw.get("items"), list) else 0, "accepted_count": len(output), "rejected_count": len(rejections), "rejections": rejections[:50], "accepted_summary": [{"name": entry.name, "quantity": str(entry.quantity or ""), "requirement_keys": sorted(entry.requirements), "document_url": entry.provenance["document_url"]} for entry in output], "usage": extract_usage}}
        return output


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().replace("ё", "е")).strip()


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def is_visible_incoming(tender: Tender, settings: FilterSettings | None = None) -> bool:
    settings = settings or FilterSettings.load()
    if tender.status != Tender.NEW or tender.review != Tender.UNREVIEWED:
        return False
    if settings.min_price and tender.max_price is not None and tender.max_price < settings.min_price:
        return False
    if tender.collecting_finished_at and tender.collecting_finished_at < timezone.now():
        return False
    return match_title(tender.title or tender.object_info, parse_terms(settings.include_words), parse_terms(settings.exclude_words))[0]


def trigger_visible_tender(tender: Tender, *, trigger: str = "incoming_visible", version: str = "v2") -> TenderComputeJob | None:
    """The only V2 intake boundary. It deliberately does not run a worker."""
    from django.conf import settings
    if not settings.CALCULATION_V2_ENABLED or not is_visible_incoming(tender):
        return None
    active = [TenderComputeJob.Status.QUEUED, TenderComputeJob.Status.RUNNING, TenderComputeJob.Status.PREPARING_INPUT, TenderComputeJob.Status.ROUTING, TenderComputeJob.Status.PREPARING]
    with transaction.atomic():
        Tender.objects.select_for_update().get(pk=tender.pk)
        existing = TenderComputeJob.objects.filter(tender=tender, version=version, status__in=active).order_by("pk").first()
        return existing or TenderComputeJob.objects.create(tender=tender, trigger=trigger, version=version)


def eligible_backfill_tenders(*, limit: int) -> list[Tender]:
    """Return unprocessed, currently eligible incoming tenders in stable newest-first order."""
    if limit < 1:
        return []
    prior_v2_job = TenderComputeJob.objects.filter(tender_id=OuterRef("pk"), version="v2")
    candidates = (
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED)
        .annotate(has_v2_job=Exists(prior_v2_job))
        .filter(has_v2_job=False)
        .order_by("-published_at", "pk")
    )
    eligible = []
    for tender in candidates.iterator(chunk_size=100):
        if is_visible_incoming(tender):
            eligible.append(tender)
            if len(eligible) == limit:
                break
    return eligible


def queue_visible_backfill(*, limit: int) -> list[TenderComputeJob]:
    """Durably queue a bounded backfill without reprocessing any existing V2 job."""
    from django.conf import settings
    if not settings.CALCULATION_V2_ENABLED:
        return []
    jobs = []
    for candidate in eligible_backfill_tenders(limit=limit):
        with transaction.atomic():
            tender = Tender.objects.select_for_update().get(pk=candidate.pk)
            if TenderComputeJob.objects.filter(tender=tender, version="v2").exists() or not is_visible_incoming(tender):
                continue
            jobs.append(TenderComputeJob.objects.create(tender=tender, trigger="incoming_backfill", version="v2"))
    return jobs


def _notification_rows(tender: Tender) -> list[dict]:
    if not tender.notification_raw:
        return []
    return parse_notification(tender.notification_raw).get("items", [])


def _source_key(row: dict, index: int) -> str:
    code = str(row.get("code") or "").strip()
    if code:
        return f"notification:code:{code}:{index}"
    identity = {"name": _normal(str(row.get("name") or "")), "quantity": str(row.get("quantity") or ""), "unit": str(row.get("unit") or "")}
    return f"notification:item:{_fingerprint(identity)[:20]}:{index}"


def _as_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def ingest_source_items(tender: Tender) -> list[TenderSourceItem]:
    """Append-only-ish notification ingestion; changed rows supersede, never overwrite source truth."""
    result = []
    for index, row in enumerate(_notification_rows(tender)):
        name = str(row.get("name") or "").strip()
        requirements = {"characteristics": row.get("characteristics") or []}
        payload = {"name": name, "quantity": row.get("quantity"), "unit": row.get("unit"), "requirements": requirements, "code": row.get("code") or ""}
        source_key = _source_key(row, index)
        fingerprint = _fingerprint(payload)
        current = TenderSourceItem.objects.filter(tender=tender, source_key__startswith=source_key, is_active=True).order_by("-pk").first()
        if current and current.metadata.get("source_fingerprint") == fingerprint:
            result.append(current)
            continue
        if current:
            current.is_active = False
            current.save(update_fields=["is_active"])
        key = source_key if current is None else f"{source_key}:v:{fingerprint[:12]}"
        item, _ = TenderSourceItem.objects.get_or_create(
            tender=tender, source_key=key,
            defaults={
                "source_type": "notification", "original_text": name, "quantity": _as_decimal(row.get("quantity")),
                "unit": str(row.get("unit") or ""), "requirements": requirements,
                "metadata": {"source_fingerprint": fingerprint, "notification_index": index, "code": row.get("code") or ""},
                "provenance": {"kind": "notification", "notification_index": index}, "supersedes": current,
            },
        )
        result.append(item)
    return result


def _diagnostic_document(document: dict) -> dict:
    value = document.get("date")
    return {
        "url": str(document.get("url") or ""),
        "name": str(document.get("name") or "Документ"),
        "kind": str(document.get("kind") or ""),
        "size_kb": document.get("size_kb"),
        "index": document.get("index"),
        "date": value.isoformat() if hasattr(value, "isoformat") else value,
    }


def assess_quality(tender: Tender, items: list[TenderSourceItem]) -> dict:
    reasons = []
    documents = [_diagnostic_document(document) for document in parse_notification(tender.notification_raw).get("documents", [])] if tender.notification_raw else []
    if not items:
        reasons.append("no_source_items")
    if len(items) == 1:
        item = items[0]
        text = _normal(item.original_text)
        if len(text) < 40:
            reasons.append("single_sparse_item")
        if item.quantity == 1 and len(text.split()) <= 6:
            reasons.append("single_aggregate_item")
        if _normal(tender.title or tender.object_info) == text:
            reasons.append("item_repeats_tender_subject")
        if not item.requirements.get("characteristics"):
            reasons.append("requirements_missing")
    if tender.max_price and tender.max_price >= Decimal("100000") and sum(len(item.original_text) for item in items) < 80:
        reasons.append("high_value_low_detail")
    useful_documents = [doc for doc in documents if doc.get("url")]
    if useful_documents:
        reasons.append("documents_available")
    score = min(1, Decimal("0.20") * len(reasons))
    return {"status": "suspicious" if reasons else "sufficient", "reasons": reasons, "confidence": str(Decimal("1.0000") - score), "enrichment_recommended": bool(useful_documents), "documents": useful_documents}


def active_calculation_items(tender: Tender) -> list[TenderSourceItem]:
    """Canonical deterministic active set: active derived rows replace their aggregate parent."""
    items = list(tender.v2_source_items.filter(is_active=True).order_by("source_key", "pk"))
    derived_parents = {item.parent_id for item in items if item.parent_id and item.source_type in {"document_enrichment", "document_extraction"}}
    return [item for item in items if item.pk not in derived_parents and item.source_type != "document_component"]


def _characteristic_values(requirements: dict) -> dict[str, str]:
    values = {}
    for entry in requirements.get("characteristics", []) if isinstance(requirements, dict) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("characteristicName") or "").strip()
        value = str(entry.get("value") or entry.get("characteristicValue") or "").strip()
        if name and value:
            values[_normal(name)] = value
    return values


def _enriched_requirements(source: TenderSourceItem, extracted: ExtractedItem) -> tuple[dict, dict, bool]:
    """Keep every notification fact; add only genuinely new document evidence."""
    base = source.requirements if isinstance(source.requirements, dict) else {}
    characteristics = []
    existing = set()
    for raw in base.get("characteristics") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("characteristicName") or "").strip()
        value = raw.get("value") if raw.get("value") not in (None, "") else raw.get("characteristicValue")
        if not name or value in (None, "", [], {}):
            continue
        row = {"name": name, "value": value, "source": raw.get("source") or "notification"}
        characteristics.append(row)
        existing.add((_normal(name), _normal(str(value))))

    field_provenance = {}
    changed = False
    for raw_name, value in extracted.requirements.items():
        if value in (None, "", [], {}):
            continue
        name = str(raw_name).strip()
        key = (_normal(name), _normal(str(value)))
        if key in existing:
            continue
        characteristics.append({"name": name, "value": value, "source": "document"})
        field_provenance[name] = {
            "value": value,
            "source": dict(extracted.provenance),
            "confidence": str(extracted.confidence or ""),
            "extraction_version": extracted.provenance.get("extraction_version", ""),
        }
        existing.add(key)
        changed = True
    if not changed:
        return base, {}, False
    return {"characteristics": characteristics, "document_requirements": extracted.requirements}, field_provenance, True

def _match_extracted_item(entry: ExtractedItem, candidates: list[TenderSourceItem]) -> TenderSourceItem | None:
    entry_name = _normal(entry.name)
    entry_values = {_normal(str(key)): _normal(str(value)) for key, value in entry.requirements.items() if value not in (None, "", [], {})}
    scored = []
    for source in candidates:
        score = 4 if _normal(source.original_text) == entry_name else 0
        if entry.quantity is not None and source.quantity == entry.quantity:
            score += 1
        source_values = {_normal(key): _normal(value) for key, value in _characteristic_values(source.requirements).items()}
        score += sum(1 for key, value in entry_values.items() if source_values.get(key) == value)
        scored.append((score, source.pk, source))
    if not scored:
        return None
    score, _, source = max(scored)
    return source if score >= 4 else None


def enrich_suspicious_tender(tender: Tender, quality: dict, enricher: DocumentEnricher) -> dict:
    """Document evidence can split or enrich a source item, never overwrite it."""
    if not quality["enrichment_recommended"]:
        return {"created": [], "state": "no_data", "diagnostics": {"outcome": "no_data", "reason": "not_recommended"}}
    active = active_calculation_items(tender)
    groups = {}
    for item in active:
        groups.setdefault(_normal(item.original_text), []).append(item)
    created, diagnostics, extracted_any = [], [], False
    for group in groups.values():
        extracted = enricher.extract(tender=tender, aggregate=group[0], documents=quality["documents"])
        detail = getattr(enricher, "diagnostics", {}) or {}
        diagnostics.append(detail)
        outcome = detail.get("outcome")
        if outcome in {"system_extraction_failure", "validation_failure"}:
            return {"created": created, "state": outcome, "diagnostics": diagnostics}
        if not extracted:
            continue
        extracted_any = True
        remaining = list(group)
        for entry in extracted:
            matched = _match_extracted_item(entry, remaining)
            if matched:
                requirements, field_provenance, requirements_changed = _enriched_requirements(matched, entry)
                quantity_changed = entry.quantity is not None and entry.quantity != matched.quantity
                unit_changed = bool(entry.unit and entry.unit != matched.unit)
                if not (requirements_changed or quantity_changed or unit_changed):
                    continue
                payload = {"parent": matched.pk, "requirements": requirements, "quantity": str(entry.quantity or matched.quantity or ""), "unit": entry.unit or matched.unit, "provenance": entry.provenance}
                item, _ = TenderSourceItem.objects.get_or_create(
                    tender=tender, source_key=f"enriched:{matched.pk}:{_fingerprint(payload)[:20]}",
                    defaults={"source_type": "document_enrichment", "original_text": matched.original_text,
                              "quantity": entry.quantity if entry.quantity is not None else matched.quantity,
                              "unit": entry.unit or matched.unit, "requirements": requirements,
                              "metadata": {"field_provenance": field_provenance},
                              "provenance": entry.provenance, "confidence": entry.confidence, "parent": matched},
                )
                matched.is_active = False
                matched.save(update_fields=["is_active"])
                remaining.remove(matched)
                created.append(item)
                continue
            payload = {"name": entry.name, "quantity": str(entry.quantity), "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance}
            parent = group[0]
            is_component = entry.relationship == "component"
            item, _ = TenderSourceItem.objects.get_or_create(
                tender=tender, source_key=f"{'component' if is_component else 'derived'}:{parent.pk}:{_fingerprint(payload)[:20]}",
                defaults={"source_type": "document_component" if is_component else "document_extraction", "original_text": entry.name, "quantity": entry.quantity,
                          "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance,
                          "metadata": {"quantity_per_parent": str(entry.quantity_per_parent)},
                          "confidence": entry.confidence, "parent": parent},
            )
            if not is_component:
                parent.is_active = False
                parent.save(update_fields=["is_active"])
            created.append(item)
    state = "success" if created else ("no_change" if extracted_any else "no_data")
    return {"created": created, "state": state, "diagnostics": diagnostics}

def _knowledge_for(items: list[TenderSourceItem]) -> list[dict]:
    knowledge = []
    for lesson in Lesson.objects.filter(scope__in=["route", "production_step"], is_active=True).only("item_word", "outcome"):
        route = lesson.outcome.get("route") if isinstance(lesson.outcome, dict) else {}
        ids = [int(step["process_id"]) for step in route.get("processes", []) if str(step.get("process_id", "")).isdigit()]
        if lesson.item_word and ids:
            knowledge.append({"needle": _normal(lesson.item_word), "process_ids": ids})
    for record in KnowledgeRecord.objects.filter(status="active").only("scope_type", "scope_context", "payload"):
        needle = _normal(str(record.applicability.get("item_text", "")))
        process_ids = [value for value in record.payload.get("process_ids", []) if isinstance(value, int)]
        if needle and process_ids:
            knowledge.append({"needle": needle, "process_ids": process_ids})
    return knowledge


def _commercial_requirements(source: TenderSourceItem) -> dict:
    return source.requirements if isinstance(source.requirements, dict) else {}


def build_commercial_items(job: TenderComputeJob) -> list[TenderCommercialItem]:
    """Turn immutable source evidence into logical sellable items and internal components."""
    tender = job.tender
    active = active_calculation_items(tender)
    result = []
    for source in active:
        component_sources = list(source.derived_items.filter(is_active=True, source_type="document_component").order_by("source_key", "pk"))
        structure = TenderCommercialItem.Structure.COMPOSITE if component_sources else TenderCommercialItem.Structure.SIMPLE
        commercial, _ = TenderCommercialItem.objects.update_or_create(
            tender=tender, source_key=f"commercial:{source.pk}",
            defaults={"job": job, "display_name": source.original_text, "quantity": source.quantity, "unit": source.unit,
                      "requirements": _commercial_requirements(source), "provenance": {"source_item_ids": [source.pk]},
                      "structure": structure, "status": "active"},
        )
        commercial.source_items.set([source])
        component_rows = component_sources or [source]
        for index, component_source in enumerate(component_rows):
            per_parent = _as_decimal((component_source.metadata or {}).get("quantity_per_parent")) or Decimal("1")
            CalculationComponent.objects.update_or_create(
                commercial_item=commercial, source_item=component_source,
                defaults={"name": component_source.original_text, "quantity_per_parent": per_parent, "unit": component_source.unit,
                          "requirements": _commercial_requirements(component_source), "provenance": component_source.provenance,
                          "status": "active", "sort_order": index},
            )
        result.append(commercial)
    return result


def _component_conflicts(commercial: TenderCommercialItem) -> list[str]:
    values: dict[str, set[str]] = {}
    for source in commercial.source_items.all():
        for name, value in _characteristic_values(_commercial_requirements(source)).items():
            values.setdefault(name, set()).add(_normal(str(value)))
    return [name for name, choices in values.items() if len(choices) > 1]


def route_tender_batch(job: TenderComputeJob, router: BatchRouter) -> list[RouteDecision]:
    commercials = build_commercial_items(job)
    items = list(CalculationComponent.objects.filter(commercial_item__in=commercials, status="active").select_related("commercial_item", "source_item").order_by("commercial_item_id", "sort_order", "pk"))
    processes = list(ProcessDefinition.objects.filter(is_active=True).order_by("pk"))
    decisions = router.route(tender=job.tender, items=items, processes=processes, knowledge=_knowledge_for([]))
    by_item = {decision.component_id: decision for decision in decisions}
    if set(by_item) != {item.pk for item in items}:
        raise ValueError("Batch router must return exactly one decision per calculation component")
    for item in items:
        decision = by_item[item.pk]
        line, _ = TenderComputeLine.objects.update_or_create(
            job=job, source_item=item.source_item, component=item,
            defaults={"commercial_item": item.commercial_item, "route_key": ",".join(map(str, decision.process_ids)), "route_confidence": decision.confidence,
                      "route_metadata": decision.rationale, "status": "needs_review" if decision.needs_review else "routed",
                      "input_snapshot": {"name": item.name, "quantity": str(item.effective_quantity), "requirements": item.requirements}},
        )
        plan, _ = ComponentRoutePlan.objects.update_or_create(
            commercial_item=item.commercial_item, component=item, scope=ComponentRoutePlan.Scope.COMPONENT,
            defaults={"status": "needs_review" if decision.needs_review else "planned", "metadata": decision.rationale},
        )
        plan.steps.all().delete()
        for position, process_id in enumerate(decision.process_ids, start=1):
            ComponentOperationStep.objects.create(route_plan=plan, process_id=process_id, position=position)
        conflicts = _component_conflicts(item.commercial_item)
        question = decision.question
        if conflicts:
            question = f"Уточните противоречивые характеристики: {', '.join(conflicts)}."
        if (decision.needs_review or conflicts) and not OwnerInteraction.objects.filter(compute_line=line, status="open").exists():
            OwnerInteraction.objects.create(tender=job.tender, source_item=item.source_item, compute_line=line, question=question, reason=decision.rationale.get("reason", ""), confidence=decision.confidence,
                                            context={"commercial_item_id": item.commercial_item_id, "component_id": item.pk, "conflicts": conflicts})
    return decisions

def run_next_tender_understanding_job(*, router: BatchRouter | None = None, enricher: DocumentEnricher | None = None) -> TenderComputeJob | None:
    router = router or GatewayBatchRouter()
    enricher = enricher or GatewayDocumentEnricher()
    job = claim_next_job()
    if job is None:
        return None
    started = time.monotonic()
    try:
        job.status = TenderComputeJob.Status.PREPARING_INPUT
        job.save(update_fields=["status", "updated_at"])
        source_items = ingest_source_items(job.tender)
        quality = assess_quality(job.tender, source_items)
        enrichment_started = time.monotonic()
        enrichment = enrich_suspicious_tender(job.tender, quality, enricher)
        if enrichment["state"] in {"system_extraction_failure", "validation_failure"}:
            job.status = TenderComputeJob.Status.PARTIAL
            job.completed_at = timezone.now()
            job.total_cost = Decimal(str(getattr(enricher, "cost_rub", 0) or 0))
            job.diagnostics = {"source_item_count": len(source_items), "active_item_count": len(active_calculation_items(job.tender)), "quality": quality, "enrichment": enrichment["diagnostics"], "enrichment_state": enrichment["state"], "questions_created": 0, "ai_cost_rub": str(job.total_cost), "total_ms": round((time.monotonic() - started) * 1000, 2)}
            job.save(update_fields=["status", "completed_at", "diagnostics", "total_cost", "updated_at"])
            return job
        derived = enrichment["created"]
        job.status = TenderComputeJob.Status.ROUTING
        job.save(update_fields=["status", "updated_at"])
        decisions = route_tender_batch(job, router)
        question_count = sum(1 for decision in decisions if decision.needs_review)
        job.status = TenderComputeJob.Status.NEEDS_REVIEW if question_count else TenderComputeJob.Status.READY
        job.completed_at = timezone.now()
        job.diagnostics = {"source_item_count": len(source_items), "active_item_count": len(active_calculation_items(job.tender)), "derived_item_count": len(derived), "documents_inspected": len(quality["documents"]), "enrichment_used": bool(derived), "quality": quality, "enrichment": enrichment["diagnostics"], "enrichment_state": enrichment["state"], "routing_item_count": len(decisions), "routing_batch_count": 1, "questions_created": question_count, "ai_cost_rub": str(getattr(router, "cost_rub", 0) + getattr(enricher, "cost_rub", 0)), "document_enrichment_ms": round((time.monotonic() - enrichment_started) * 1000, 2), "total_ms": round((time.monotonic() - started) * 1000, 2)}
        job.total_cost = Decimal(str(getattr(router, "cost_rub", 0) + getattr(enricher, "cost_rub", 0)))
        job.save(update_fields=["status", "completed_at", "diagnostics", "total_cost", "updated_at"])
    except Exception as exc:
        job.status = TenderComputeJob.Status.QUEUED
        job.error = {"class": type(exc).__name__, "message": str(exc)}
        job.save(update_fields=["status", "error", "updated_at"])
        raise
    return job