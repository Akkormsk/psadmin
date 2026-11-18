"""Presentation-only state for Tender document analysis.

The UI receives this resolved state and never reconstructs it from jobs, items or questions.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from decimal import Decimal

from .models import OwnerInteraction, TenderCommercialItem, TenderComputeJob, TenderSourceItem
from .intake_worker import interrupted_job_snapshot, intake_heartbeat_timeout


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
    progress: dict | None = None
    cost: dict | None = None


def analysis_cost(job) -> dict:
    data = _job_analysis_cost(job)
    root = (job.diagnostics or {}).get('refresh_root_id') if job else None
    if not root:
        return data
    from django.db.models import Q
    from .models import TenderComputeJob
    history = TenderComputeJob.objects.filter(Q(pk=root) | Q(diagnostics__refresh_root_id=root)).exclude(pk=job.pk)
    parts = [data] + [_job_analysis_cost(previous) for previous in history]
    total = sum((Decimal(part.get('known_rub') or '0') for part in parts), Decimal('0'))
    unknown = any(part.get('incomplete') for part in parts)
    formatted = f'{total:.2f}'.replace('.', ',')
    return {**data, 'amount_rub': str(total) if not unknown else None, 'known_rub': str(total),
        'incomplete': unknown, 'actual': not unknown and all(part.get('actual') for part in parts),
        'label': 'Стоимость уточняется' if unknown else f'≈ {formatted} ₽' if total else data.get('label', 'Без нового расхода')}


def _job_analysis_cost(job) -> dict:
    """Estimate this job's received model answers, including failed answers, not reservations."""
    from .gateway_budget import intake_spend_rub
    job = interrupted_job_snapshot(job)
    if not job:
        return {}
    diagnostics = job.diagnostics or {}
    staged = diagnostics.get("stages") or {}
    entries = diagnostics.get("enrichment") or []
    entries = [entries] if isinstance(entries, dict) else list(entries)
    if staged:
        entries = ([] if "structure" in staged else entries) + list(staged.values())
    else:
        entries.append(diagnostics.get("routing") or {})
    total, unknown, received, actual = Decimal("0"), bool(diagnostics.get("billing_uncertain")), False, True
    for entry in entries:
        usage = entry.get("usage") or (entry.get("extraction") or {}).get("usage") or {}
        if not usage:
            unknown = unknown or bool(entry.get("provider_call_happened"))
            continue
        received = True
        if entry.get('actual_cost_rub') is not None:
            total += Decimal(str(entry['actual_cost_rub']))
            continue
        actual = False
        amount = intake_spend_rub(usage, entry.get("model", ""))
        if amount is None:
            unknown = True
        else:
            # Preserve the tariff used when the job ran, if it was recorded.
            total += Decimal(str(entry.get("cost_rub") if entry.get("cost_rub") is not None else amount))
    if not received and job.total_cost:
        total = job.total_cost
    active = job.status in _ACTIVE
    formatted = f"{total:.2f}".replace(".", ",")
    label = "Стоимость уточняется" if unknown else "Стоимость после ответа" if active and not received else "Без нового расхода" if not total else f"≈ {formatted} ₽"
    if actual and received and not unknown:
        label = f"{formatted} ₽"
    if unknown and total:
        label = f"от {formatted} ₽ · часть стоимости неизвестна"
    elif not unknown and 0 < total < Decimal("0.01"):
        label = "< 0,01 ₽"
    return {"amount_rub": None if unknown else str(total), "known_rub": str(total),
        "incomplete": unknown, "pending": active, "label": label, "actual": actual and received and not unknown,
        "steps": {key: str(row.get('actual_cost_rub') if row.get('actual_cost_rub') is not None else row.get('cost_rub') or '0') for key, row in staged.items()},
        "description": "По начислениям Timeweb за это обновление, включая кэш." if actual and received and not unknown else "За это обновление, включая неудачные ответы. Оценка по токенам и сохранённым тарифам, включая запись и чтение кэша, если они указаны в ответе; не счёт Timeweb. Текущий запрос учтётся после ответа."}


_STATUS = {
    "NOT_CHECKED": ("Не проверено", "not-checked"),
    "PROCESSING": ("Проверяем ТЗ…", "processing"),
    "CHECKED_NO_CHANGES": ("Разбор завершён", "checked"),
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
    "missing_items_array": "Модель вернула некорректный формат списка товаров; прежние данные сохранены.",
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
    job = interrupted_job_snapshot(job)
    if job is None:
        return []
    diagnostics = job.diagnostics if isinstance(job.diagnostics, dict) else {}
    entries = diagnostics.get("enrichment") or []
    entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
    issues = []
    for gap in diagnostics.get('analysis_gaps', []):
        # Rejected individual facts stay in diagnostics; they are not an instruction to the owner.
        if gap.get('stage') == 'tz' and gap.get('key') and gap.get('name'):
            continue
        name = gap.get('name') or ''
        if gap.get('stage') == 'tz':
            reason = 'Не удалось дополнить ТЗ части товаров. Прежние данные сохранены. Повторите «Обновить».'
        else:
            reason = gap.get('reason') or 'Часть проверки не завершена. Повторите «Обновить».'
        issues.append(f"{name}: {reason}" if name else reason)
    if job.error:
        issues.append(job.error['message'] if job.error.get('class') == 'WorkerInterrupted' else 'Разбор ТЗ не завершён. Прежние данные сохранены. Повторите «Обновить».')
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("final_failure_reason"):
            issues.append(_VALIDATION_MESSAGES.get(str(entry["final_failure_reason"]), "Не удалось проверить предложения ТЗ. Прежние данные сохранены. Повторите «Обновить»."))
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
    job = interrupted_job_snapshot(job)
    issues = tuple(analysis_issues(job))
    if job is None:
        code = "NOT_CHECKED"
    elif _is_technical_failure(job):
        code = "ANALYSIS_INCOMPLETE"
    elif job.status in _ACTIVE:
        code = "PROCESSING"
    elif issues:
        code = "CHECKED_WITH_GAPS"
    elif question_count or pending_count:
        code = "NEEDS_OWNER_INPUT"
    else:
        code = "ENRICHED" if has_enriched_items else "CHECKED_NO_CHANGES"
    label, kind = ("Обновлено · есть замечания", "warning") if code == "CHECKED_WITH_GAPS" else _STATUS[code]
    if code == "NEEDS_OWNER_INPUT":
        label = f"{label} · {question_count}" if question_count else f"Нужно проверить правки ТЗ · {pending_count}"
    reason = issues[0] if code in {"ANALYSIS_INCOMPLETE", "CHECKED_WITH_GAPS"} and issues else ""
    from django.utils import timezone
    now = timezone.now()
    refresh = (job.diagnostics or {}).get("refresh", {}) if job else {}
    active = bool(job and job.status in _ACTIVE)
    if refresh and active:
        label = refresh.get("label") or label
    elif refresh and job.status == "ready":
        label = "Маршруты обновлены" if job.diagnostics.get("routes_only") else "Обновлено"
    elif refresh and code == "ANALYSIS_INCOMPLETE":
        label = "Обновление прервано" if job.error.get('class') == 'WorkerInterrupted' else "Обновление не завершено"
    if job and not active and job.diagnostics.get('awaiting_budget'):
        label = 'Прогресс сохранён · ожидает продолжения'
    started = (job.started_at or job.created_at) if job else None
    if job and job.diagnostics.get('refresh_started_at'):
        from datetime import datetime
        try:
            started = datetime.fromisoformat(job.diagnostics['refresh_started_at'])
        except (TypeError, ValueError):
            pass
    progress = {"started_at": started.isoformat() if started else None,
        "completed_at": job.completed_at.isoformat() if job and job.completed_at else None,
        "server_time": now.isoformat(), "elapsed_seconds": max(0, int(((job.completed_at or now) - started).total_seconds())) if started else 0,
        "heartbeat_at": job.updated_at.isoformat() if job else None,
        "worker_alive": not active or not job.started_at or (now - job.updated_at).total_seconds() < 45,
        "stage": refresh.get("stage", ""), "full_refresh": bool(refresh), "published_revision": (job.diagnostics or {}).get("published_revision", 0) if job else 0,
        "retryable": bool(active and now - job.updated_at > intake_heartbeat_timeout())}
    labels = {'notification': 'Извещение', 'documents': 'Документы', 'structure': 'Состав', 'national_regime': '1875', 'tz': 'ТЗ', 'routing': 'Маршруты'}
    ledger = (job.diagnostics or {}).get('stages', {}) if job else {}
    progress['steps'] = [{'id': key, 'label': title, 'state': ledger.get(key, {}).get('state', 'pending'), 'seconds': round(ledger[key]['duration_ms']/1000, 1) if ledger.get(key, {}).get('duration_ms') is not None else None, 'reused': bool(ledger.get(key, {}).get('reused_from'))} for key, title in labels.items()]
    gap_stages = {gap.get('stage') for gap in (job.diagnostics or {}).get('analysis_gaps', [])
                  if gap.get('affects_specification') is not False} if job else set()
    for step in progress['steps']:
        if step['id'] in gap_stages and step['state'] == 'complete':
            step['state'] = 'warning'
        if active and step['id'] == refresh.get('stage'):
            step['state'] = 'running'
    stamp = str(job.diagnostics["published_revision"]) if job and "published_revision" in job.diagnostics else job.updated_at.isoformat() if job and job.updated_at else ""
    revision = f"{job.pk if job else 0}:{stamp}:{code}:{question_count}:{pending_count}"
    return TenderAnalysisStatus(tender=tender, code=code, label=label, kind=kind, question_count=question_count, pending_count=pending_count, reason=reason, revision=revision, job_id=job.pk if job else 0, processing=active, issues=issues, progress=progress, cost=analysis_cost(job))


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
    from .document_enrichment import document_proposal_changes
    pending = Counter(item.tender_id for item in TenderSourceItem.objects.filter(tender_id__in=ids, source_type="document_proposal", is_active=False).select_related("parent") if document_proposal_changes(item))
    return {tender.pk: _status_for(tender, jobs.get(tender.pk), counts[tender.pk], tender.pk in enriched_ids, pending[tender.pk]) for tender in tenders}


def specification_status(tender, *, job=None):
    """Read-only coverage of available specifications, independent of UI diagnostics and routes."""
    from tender_selection.notification import parse_notification
    from tender_selection.services import _document_input_fingerprint
    if job is None:
        job = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').first()
    job = interrupted_job_snapshot(job)
    def result(state, details):
        labels = {'original': 'ТЗ исходное', 'partial': 'ТЗ частичное', 'checked': 'ТЗ проверено'}
        return {'state': state, 'label': labels[state], 'details': list(dict.fromkeys(details))}
    if job is None:
        return result('original', ['Доступны исходные сведения и файлы. Проверку можно запустить кнопкой «Обновить».'])
    diagnostics, details = job.diagnostics or {}, []
    stages = diagnostics.get('stages') or {}
    relevant = {'notification', 'documents', 'source_reading', 'source_binding', 'structure', 'tz', 'national_regime'}
    for gap in diagnostics.get('analysis_gaps', []):
        stage = gap.get('stage')
        if stage not in relevant or gap.get('affects_specification') is False:
            continue
        if stage == 'tz' and gap.get('name'):
            item = f'Товар «{gap["item_name"]}»: ' if gap.get('item_name') else ''
            details.append(f'{item}не удалось подтвердить характеристику «{gap["name"]}». Известные сведения сохранены.')
        elif stage == 'documents':
            name = f' «{gap["name"]}»' if gap.get('name') else ''
            details.append('Документ' + name + ' прочитан не полностью. Можно продолжить с полученными сведениями.')
        elif stage == 'structure':
            details.append('Состав части товаров требует уточнения.')
        elif stage == 'national_regime':
            details.append('Часть сведений о требованиях заказчика не удалось проверить.')
        else:
            details.append('Часть сведений ТЗ не прочитана или не привязана к товарам. Можно продолжить с имеющимися данными.')
    if any(name in stages and (stages[name].get('state') != 'complete' or stages[name].get('outcome') == 'source_only')
           for name in ('source_reading', 'source_binding', 'national_regime')):
        details.append('Часть проверки сведений ТЗ не завершена. Уже полученные данные доступны.')
    if diagnostics.get('structure_requires_review'):
        details.append('Состав части товаров требует уточнения.')
    reading = diagnostics.get('source_reading') or {}
    if reading and not reading.get('complete'):
        details.append('Документы прочитаны не полностью. Уже полученные сведения сохранены.')
    if not reading:
        prepared = (stages.get('documents') or {}).get('result')
        considered = prepared.get('documents', []) if isinstance(prepared, dict) else []
        if not considered or any(document.get('reading_status') != 'read' for document in considered):
            details.append('Полнота чтения документов пока не подтверждена. Уже полученные сведения доступны.')
    required = ('documents', 'structure', 'tz')
    completed = all(stages.get(name, {}).get('state') == 'complete'
                    and stages[name].get('outcome') != 'source_only' for name in required)
    if not completed:
        if not stages and not job.error and job.status in _ACTIVE and not TenderComputeJob.objects.filter(tender=tender, pk__lt=job.pk).exists():
            return result('original', ['Проверка выполняется. Пока доступны исходные сведения и файлы.'])
        details.append(job.error['message'] if job.error.get('class') == 'WorkerInterrupted' else 'Проверка ТЗ не завершена. Прежние данные доступны; повторить проверку можно кнопкой «Обновить».')
    if not parse_notification(tender.notification_raw or {}).get('documents'):
        details.append('Документы для проверки ТЗ ещё не получены.')
    if completed and not details and diagnostics.get('document_input_fingerprint') != _document_input_fingerprint(tender):
        details.append('Исходные сведения изменились после проверки. Уже полученное ТЗ сохранено; его можно обновить.')
    if details:
        return result('partial', details)
    return result('checked', ['Доступные материалы проверены. Значимых пропусков разбора не обнаружено. Статус не заменяет согласование предложенных правок.'])
