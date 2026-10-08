"""Durable, engine-owned preparation planning for Calculation Engine V2."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import time
from typing import Iterable, Protocol

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from tender_selection.models import Tender

from .calculation_v2 import engine_registry
from .calculation_v2_pipeline import is_visible_incoming
from .models import ComponentOperationStep, TenderComputeJob, TenderComputePreparation, TenderComputeWorkUnit


@dataclass(frozen=True)
class PreparationTask:
    engine_key: str
    dedupe_key: str
    fingerprint: str
    operation_step_ids: tuple[int, ...]
    payload: dict
    estimated_cost_rub: float = 0.0
    dependencies: tuple[str, ...] = ()


class PreparationEngine(Protocol):
    key: str

    def supports_preparation(self, step: ComponentOperationStep) -> bool: ...
    def plan_tender_preparation(self, job: TenderComputeJob, steps: list[ComponentOperationStep]) -> Iterable[PreparationTask]: ...
    def prepare(self, preparation: TenderComputePreparation) -> dict: ...


def fingerprint(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def is_preparation_eligible(tender: Tender) -> bool:
    """Business lifecycle policy, intentionally independent of UI columns."""
    if tender.status == Tender.DISMISSED:
        return False
    return is_visible_incoming(tender) or tender.review == Tender.INTERESTING or tender.status == Tender.PUSHED


def eligible_tenders(limit: int = 100, lifecycle: str = "all") -> list[Tender]:
    if limit < 1:
        return []
    if lifecycle not in {"all", "incoming", "working"}:
        raise ValueError("Unknown preparation lifecycle")
    eligible = []
    for tender in Tender.objects.exclude(status=Tender.DISMISSED).order_by("pk").iterator(chunk_size=100):
        incoming = is_visible_incoming(tender)
        working = tender.review == Tender.INTERESTING or tender.status == Tender.PUSHED
        selected = incoming if lifecycle == "incoming" else working if lifecycle == "working" else incoming or working
        if selected:
            eligible.append(tender)
            if len(eligible) == limit:
                break
    return eligible


def preparation_job_for(tender: Tender) -> TenderComputeJob | None:
    return (
        TenderComputeJob.objects.filter(
            tender=tender,
            status__in=[TenderComputeJob.Status.READY, TenderComputeJob.Status.NEEDS_REVIEW, TenderComputeJob.Status.PARTIAL],
        )
        .order_by("-pk")
        .first()
    )


def _preparation_engines():
    from . import catalog_preparation  # noqa: F401
    from . import provider_capabilities  # noqa: F401

    return [engine_registry.get(key) for key in engine_registry.keys() if hasattr(engine_registry.get(key), "supports_preparation")]


def planned_tasks(job: TenderComputeJob) -> list[PreparationTask]:
    steps = list(
        ComponentOperationStep.objects.filter(route_plan__commercial_item__job=job, status="planned")
        .select_related("process", "route_plan__component", "route_plan__commercial_item")
        .order_by("pk")
    )
    tasks: list[PreparationTask] = []
    for engine in _preparation_engines():
        supported = [step for step in steps if engine.supports_preparation(step)]
        tasks.extend(engine.plan_tender_preparation(job, supported))
    return tasks


def persist_preparation_tasks(job: TenderComputeJob, tasks: Iterable[PreparationTask]) -> tuple[list[TenderComputePreparation], int]:
    persisted, reused = [], 0
    with transaction.atomic():
        for task in tasks:
            first_step = task.operation_step_ids[0] if task.operation_step_ids else None
            work_unit, _ = TenderComputeWorkUnit.objects.get_or_create(
                job=job,
                engine_key=task.engine_key,
                dedupe_key=task.dedupe_key,
                defaults={"input_fingerprint": task.fingerprint, "operation_step_id": first_step, "status": "queued"},
            )
            if work_unit.input_fingerprint != task.fingerprint:
                work_unit.input_fingerprint = task.fingerprint
                work_unit.status = "queued"
                work_unit.save(update_fields=["input_fingerprint", "status"])
            work_unit.operation_steps.set(task.operation_step_ids)
            preparation, created = TenderComputePreparation.objects.get_or_create(
                work_unit=work_unit,
                engine_key=task.engine_key,
                preparation_key="prepare",
                defaults={"freshness": task.fingerprint, "payload": task.payload, "status": "queued"},
            )
            if not created and preparation.freshness == task.fingerprint and preparation.status == "ready":
                reused += 1
            elif not created:
                preparation.freshness = task.fingerprint
                preparation.payload = task.payload
                preparation.status = "queued"
                preparation.error = {}
                preparation.started_at = None
                preparation.completed_at = None
                preparation.save(update_fields=["freshness", "payload", "status", "error", "started_at", "completed_at"])
            persisted.append(preparation)
    return persisted, reused


def plan_tender_preparation(job: TenderComputeJob) -> tuple[list[TenderComputePreparation], int]:
    return persist_preparation_tasks(job, planned_tasks(job))


def is_task_current(job: TenderComputeJob, task: PreparationTask) -> bool:
    return TenderComputePreparation.objects.filter(
        work_unit__job=job,
        work_unit__engine_key=task.engine_key,
        work_unit__dedupe_key=task.dedupe_key,
        engine_key=task.engine_key,
        preparation_key="prepare",
        freshness=task.fingerprint,
        status="ready",
    ).exists()


def claim_next_preparation() -> TenderComputePreparation | None:
    with transaction.atomic():
        preparation = (
            TenderComputePreparation.objects.select_for_update(skip_locked=True)
            .filter(status__in=["queued", "stale"])
            .order_by("pk")
            .first()
        )
        if preparation is None:
            return None
        preparation.status = "running"
        preparation.attempt_count += 1
        preparation.started_at = timezone.now()
        preparation.save(update_fields=["status", "attempt_count", "started_at"])
        return preparation


def requeue_stale_preparations(*, now=None, lease: timedelta = timedelta(minutes=15)) -> int:
    now = now or timezone.now()
    return TenderComputePreparation.objects.filter(status="running", started_at__lt=now - lease).update(
        status="queued", started_at=None
    )


def refresh_work_unit_status(preparation: TenderComputePreparation) -> None:
    work_unit = preparation.work_unit
    statuses = set(work_unit.preparations.values_list("status", flat=True))
    if statuses and statuses <= {"ready"}:
        status = "ready"
    elif "failed" in statuses:
        status = "failed"
    elif "partial" in statuses:
        status = "partial"
    else:
        status = "queued"
    if work_unit.status != status:
        work_unit.status = status
        work_unit.save(update_fields=["status"])


def run_next_preparation() -> TenderComputePreparation | None:
    if not settings.CALCULATION_V2_PREPARATION_ENABLED:
        return None
    preparation = claim_next_preparation()
    if preparation is None:
        return None
    started = time.monotonic()
    try:
        engine = engine_registry.get(preparation.engine_key)
        result = engine.prepare(preparation)
        preparation.refresh_from_db()
        preparation.payload = {**preparation.payload, "duration_ms": round((time.monotonic() - started) * 1000, 2)}
        preparation.status = result.get("status", preparation.status if preparation.status != "running" else "ready")
        preparation.completed_at = timezone.now()
        preparation.save(update_fields=["payload", "status", "completed_at"])
        refresh_work_unit_status(preparation)
    except Exception as exc:
        preparation.status = "failed"
        preparation.error = {"class": exc.__class__.__name__, "message": str(exc)[:1000]}
        preparation.completed_at = timezone.now()
        preparation.save(update_fields=["status", "error", "completed_at"])
        refresh_work_unit_status(preparation)
    return preparation


def sweep_preparation(*, limit: int = 100, dry_run: bool = True, lifecycle: str = "all") -> dict:
    stats = {"eligible": 0, "missing_job": 0, "understanding_queued": 0, "planned": 0, "queued": 0, "already_warm": 0, "dry_run": dry_run, "lifecycle": lifecycle}
    for tender in eligible_tenders(limit, lifecycle=lifecycle):
        stats["eligible"] += 1
        job = preparation_job_for(tender)
        if job is None:
            stats["missing_job"] += 1
            if not dry_run:
                with transaction.atomic():
                    locked = Tender.objects.select_for_update().get(pk=tender.pk)
                    existing = TenderComputeJob.objects.filter(tender=locked, version="v2").order_by("-pk").first()
                    if existing is None:
                        TenderComputeJob.objects.create(
                            tender=locked, version="v2", status=TenderComputeJob.Status.QUEUED,
                            trigger="preparation_reconciliation",
                        )
                        stats["understanding_queued"] += 1
                    elif existing.status == TenderComputeJob.Status.FAILED:
                        existing.status = TenderComputeJob.Status.QUEUED
                        existing.error = {}
                        existing.save(update_fields=["status", "error", "updated_at"])
                        stats["understanding_queued"] += 1
            continue
        tasks = planned_tasks(job)
        stats["planned"] += len(tasks)
        if dry_run:
            stats["already_warm"] += sum(is_task_current(job, task) for task in tasks)
            stats["queued"] += sum(not is_task_current(job, task) for task in tasks)
            continue
        preparations, reused = persist_preparation_tasks(job, tasks)
        stats["queued"] += len(preparations) - reused
        stats["already_warm"] += reused
    return stats
