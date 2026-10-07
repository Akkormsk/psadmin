"""Presentation-only state for Tender document analysis.

The UI receives this resolved state and never reconstructs it from jobs, items or questions.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter

from .models import OwnerInteraction, TenderCommercialItem, TenderComputeJob, TenderSourceItem


@dataclass(frozen=True)
class TenderAnalysisStatus:
    tender: object
    code: str
    label: str
    kind: str
    question_count: int = 0


_STATUS = {
    "NOT_CHECKED": ("Не проверено", "not-checked"),
    "PROCESSING": ("Проверяем ТЗ…", "processing"),
    "CHECKED_NO_CHANGES": ("ТЗ проверено", "checked"),
    "ENRICHED": ("Позиции уточнены по ТЗ", "enriched"),
    "NEEDS_OWNER_INPUT": ("Нужно уточнить", "review"),
    "ANALYSIS_INCOMPLETE": ("Проверка ТЗ не завершена", "incomplete"),
}
_ACTIVE = {
    TenderComputeJob.Status.QUEUED,
    TenderComputeJob.Status.RUNNING,
    TenderComputeJob.Status.PREPARING_INPUT,
    TenderComputeJob.Status.ROUTING,
    TenderComputeJob.Status.PREPARING,
}
_TECHNICAL_OUTCOMES = {"system_extraction_failure", "validation_failure", "provider_failure", "retryable"}


def _is_technical_failure(job) -> bool:
    if job is None:
        return False
    if job.status in {TenderComputeJob.Status.PARTIAL, TenderComputeJob.Status.FAILED}:
        return True
    if job.status in _ACTIVE and bool(job.error):
        return True
    diagnostics = job.diagnostics if isinstance(job.diagnostics, dict) else {}
    if diagnostics.get("enrichment_state") in _TECHNICAL_OUTCOMES:
        return True
    return any(
        isinstance(entry, dict) and entry.get("outcome") in _TECHNICAL_OUTCOMES
        for entry in diagnostics.get("enrichment", [])
    )



def _characteristic_pairs(requirements: dict) -> set[tuple[str, str]]:
    pairs = set()
    for row in requirements.get("characteristics", []) if isinstance(requirements, dict) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("characteristicName") or "").strip().casefold()
        value = row.get("value") if row.get("value") not in (None, "") else row.get("characteristicValue")
        if name and value not in (None, "", [], {}):
            pairs.add((name, str(value).strip().casefold()))
    return pairs


def document_item_has_change(item: TenderSourceItem) -> bool:
    """A historical document row is visible as enrichment only if it changed usable data."""
    if item.source_type == "document_extraction":
        return True
    if item.source_type != "document_enrichment" or item.parent_id is None:
        return False
    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    document = requirements.get("document_requirements") if isinstance(requirements.get("document_requirements"), dict) else {}
    parent_requirements = item.parent.requirements if isinstance(item.parent.requirements, dict) else {}
    parent_pairs = _characteristic_pairs(parent_requirements)
    for name, value in document.items():
        if value not in (None, "", [], {}) and (str(name).strip().casefold(), str(value).strip().casefold()) not in parent_pairs:
            return True
    return item.quantity != item.parent.quantity or (bool(item.unit) and item.unit != item.parent.unit)

def _status_for(tender, job, question_count: int, has_enriched_items: bool) -> TenderAnalysisStatus:
    if job is None:
        code = "NOT_CHECKED"
    elif _is_technical_failure(job):
        code = "ANALYSIS_INCOMPLETE"
    elif question_count:
        code = "NEEDS_OWNER_INPUT"
    elif job.status in _ACTIVE:
        code = "PROCESSING"
    else:
        code = "ENRICHED" if has_enriched_items else "CHECKED_NO_CHANGES"
    label, kind = _STATUS[code]
    if code == "NEEDS_OWNER_INPUT":
        label = f"{label} · {question_count}"
    return TenderAnalysisStatus(tender=tender, code=code, label=label, kind=kind, question_count=question_count)


def resolve_tender_analysis_status(tender) -> TenderAnalysisStatus:
    return resolve_tender_analysis_statuses([tender])[tender.pk]


def resolve_tender_analysis_statuses(tenders) -> dict[int, TenderAnalysisStatus]:
    tenders = list(tenders)
    ids = [tender.pk for tender in tenders]
    jobs = {}
    for job in TenderComputeJob.objects.filter(tender_id__in=ids, version__startswith="v2").order_by("tender_id", "pk"):
        jobs[job.tender_id] = job
    counts = Counter(
        OwnerInteraction.objects.filter(tender_id__in=ids, status="open").values_list("tender_id", flat=True)
    )
    enriched_ids = {
        item.tender_id
        for item in TenderSourceItem.objects.filter(
            tender_id__in=ids, is_active=True, source_type__in=["document_extraction", "document_enrichment"]
        ).select_related("parent")
        if document_item_has_change(item)
    }
    enriched_ids.update(TenderCommercialItem.objects.filter(tender_id__in=ids, structure=TenderCommercialItem.Structure.COMPOSITE, status="active").values_list("tender_id", flat=True))
    return {tender.pk: _status_for(tender, jobs.get(tender.pk), counts[tender.pk], tender.pk in enriched_ids) for tender in tenders}
