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
    pending_count: int = 0
    reason: str = ""
    revision: str = ""
    job_id: int = 0
    processing: bool = False
    issues: tuple[str, ...] = ()


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
_TECHNICAL_OUTCOMES = {"system_extraction_failure", "validation_failure", "provider_failure", "retryable", "document_reading_incomplete"}


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
    entries = diagnostics.get("enrichment") or []
    entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
    return any(
        isinstance(entry, dict) and entry.get("outcome") in _TECHNICAL_OUTCOMES
        for entry in entries
    )



_VALIDATION_MESSAGES = {
    "Решение не соответствует ссылкам на исходные факты": "В ответе смешаны новые и исходные характеристики. Изменения не применены.",
    "Некорректные или повторные ссылки на исходные факты": "Агент неверно связал характеристики с исходными данными. Изменения не применены.",
    "Сохранённый факт должен совпадать с исходным": "Агент изменил характеристику, которую отметил как неизменённую. Результат не применён.",
    "Не все исходные факты учтены в итоговом описании": "В новом описании пропущена часть исходных характеристик. Изменения не применены.",
}

_REJECTION_MESSAGES = {
    "invalid_parent_source_item_id": "Не удалось связать результат с исходной позицией.",
    "invalid_name_or_source": "Не подтверждены название товара или источник сведений.",
    "untrusted_compiled_reconciliation": "Ответ содержит неподтверждённый результат объединения.",
    "missing_fact_reconciliation": "В ответе не учтены ссылки на исходные характеристики.",
    "unsubstantiated_supply_structure": "Разбивка на товары не обоснована или противоречит типу поставки.",
    "invalid_characteristic_source": "У характеристики не подтверждён документ-источник.",
    "invalid_structure": "Не заполнены обязательные данные товара или количества.",
    "multiple_enrichments_for_one_item": "Для одного товара получены конкурирующие варианты вместо единого описания.",
    "aggregate_and_components_mixed": "В ответе одновременно указаны целая позиция и её составляющие.",
}

def analysis_issues(job) -> list[str]:
    """Explain saved diagnostics without calling a model or guessing missing evidence."""
    if job is None:
        return []
    diagnostics = job.diagnostics if isinstance(job.diagnostics, dict) else {}
    entries = diagnostics.get("enrichment") or []
    entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
    issues = []
    if job.error:
        error = job.error.get("message") or job.error.get("detail") or "Ошибка анализа; подробности в журнале." if isinstance(job.error, dict) else job.error
        issues.append(str(error))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("final_failure_reason"):
            issues.append(_VALIDATION_MESSAGES.get(str(entry["final_failure_reason"]), str(entry["final_failure_reason"])))
        if (entry.get("extraction") or {}).get("rejections"):
            for rejection in entry["extraction"]["rejections"]:
                issues.append(_REJECTION_MESSAGES.get(rejection.get("reason"), "Ответ по составу не прошёл проверку; изменения не применены."))
        if entry.get("outcome") == "budget_blocked":
            issues.append("Запрос превышает установленный бюджет анализа и не был отправлен модели.")
        for document in (entry.get("documents") or {}).get("considered", []):
            if document.get("reading_status") not in {"partial", "unreadable"}:
                continue
            reason = document.get("error") or "; ".join(document.get("warnings") or []) or ("Текст извлечён не полностью или с предупреждением; нужна проверка оригинала." if document.get("reading_status") == "partial" else "Не удалось извлечь текст.")
            issues.append(f"{document.get('name') or 'Документ'}: {reason}")
    if not issues and _is_technical_failure(job):
        issues.append("Разбор не завершён, но подробная причина в сохранённом журнале отсутствует. Повторите анализ.")
    return list(dict.fromkeys(issues))


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

def _status_for(tender, job, question_count: int, has_enriched_items: bool, pending_count: int = 0) -> TenderAnalysisStatus:
    if job is None:
        code = "NOT_CHECKED"
    elif _is_technical_failure(job):
        code = "ANALYSIS_INCOMPLETE"
    elif job.status in _ACTIVE:
        code = "PROCESSING"
    elif question_count or pending_count:
        code = "NEEDS_OWNER_INPUT"
    else:
        code = "ENRICHED" if has_enriched_items else "CHECKED_NO_CHANGES"
    label, kind = _STATUS[code]
    if code == "NEEDS_OWNER_INPUT":
        label = f"{label} · {question_count}" if question_count else f"Нужно проверить правки ТЗ · {pending_count}"
    issues = tuple(analysis_issues(job))
    reason = issues[0] if code == "ANALYSIS_INCOMPLETE" else ""
    stamp = job.updated_at.isoformat() if job and job.updated_at else ""
    revision = f"{job.pk if job else 0}:{stamp}:{code}:{question_count}:{pending_count}"
    return TenderAnalysisStatus(tender=tender, code=code, label=label, kind=kind, question_count=question_count, pending_count=pending_count, reason=reason, revision=revision, job_id=job.pk if job else 0, processing=bool(job and job.status in _ACTIVE), issues=issues)


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
    pending = Counter(TenderSourceItem.objects.filter(tender_id__in=ids, source_type="document_proposal", is_active=False).values_list("tender_id", flat=True))
    return {tender.pk: _status_for(tender, jobs.get(tender.pk), counts[tender.pk], tender.pk in enriched_ids, pending[tender.pk]) for tender in tenders}
