"""Generic, persistent foundation for Calculation Engine V2."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
from typing import Any, Iterable, Protocol

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import TenderComputeJob, TenderComputeLine, TenderComputePreparation, TenderComputeWorkUnit


class CalculationEngine(Protocol):
    """Contract implemented by independently registered V2 engines."""
    key: str

    def build_work_input(self, line: TenderComputeLine) -> dict[str, Any]: ...
    def dedupe_key(self, work_input: dict[str, Any]) -> str: ...
    def plan_preparations(self, work_unit: TenderComputeWorkUnit) -> Iterable["PreparationPlan"]: ...


@dataclass(frozen=True)
class PreparationPlan:
    key: str
    freshness: str = ""


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, CalculationEngine] = {}

    def register(self, engine: CalculationEngine) -> CalculationEngine:
        if not engine.key:
            raise ValueError("Calculation engine key is required")
        if engine.key in self._engines:
            raise ValueError(f"Calculation engine is already registered: {engine.key}")
        self._engines[engine.key] = engine
        return engine

    def get(self, key: str) -> CalculationEngine:
        return self._engines[key]

    def keys(self) -> tuple[str, ...]:
        return tuple(self._engines)


engine_registry = EngineRegistry()


def _fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def enqueue_tender_compute(tender, *, trigger: str = "incoming_visible", version: str = "v2") -> TenderComputeJob | None:
    """Future Tender lifecycle boundary; it is intentionally not wired to V1 yet."""
    if not settings.CALCULATION_V2_ENABLED:
        return None
    return TenderComputeJob.objects.create(tender=tender, trigger=trigger, version=version)


def plan_work_units(job: TenderComputeJob, registry: EngineRegistry = engine_registry) -> list[TenderComputeWorkUnit]:
    """Group routed compute lines using only the engine selected by each line."""
    work_units: list[TenderComputeWorkUnit] = []
    for line in job.lines.select_related("source_item").order_by("pk"):
        if not line.engine_key:
            continue
        engine = registry.get(line.engine_key)
        work_input = engine.build_work_input(line)
        dedupe_key = engine.dedupe_key(work_input)
        work_unit, _ = TenderComputeWorkUnit.objects.get_or_create(
            job=job,
            engine_key=engine.key,
            dedupe_key=dedupe_key,
            defaults={"input_fingerprint": _fingerprint(work_input)},
        )
        work_unit.lines.add(line)
        if work_unit not in work_units:
            work_units.append(work_unit)
    return work_units


def plan_preparations(work_unit: TenderComputeWorkUnit, registry: EngineRegistry = engine_registry) -> list[TenderComputePreparation]:
    """Persist preparation requests without knowing what an engine prepares."""
    engine = registry.get(work_unit.engine_key)
    preparations: list[TenderComputePreparation] = []
    for plan in engine.plan_preparations(work_unit):
        preparation, _ = TenderComputePreparation.objects.get_or_create(
            work_unit=work_unit,
            engine_key=engine.key,
            preparation_key=plan.key,
            defaults={"freshness": plan.freshness},
        )
        preparations.append(preparation)
    return preparations


def claim_next_job() -> TenderComputeJob | None:
    """Atomically claim one queued job; SKIP LOCKED makes competing workers safe."""
    with transaction.atomic():
        job = (
            TenderComputeJob.objects.select_for_update(skip_locked=True)
            .filter(status=TenderComputeJob.Status.QUEUED)
            .order_by("created_at", "pk")
            .first()
        )
        if job is None:
            return None
        job.status = TenderComputeJob.Status.RUNNING
        job.attempt_count += 1
        job.started_at = timezone.now()
        job.save(update_fields=["status", "attempt_count", "started_at", "updated_at"])
        return job


def requeue_stale_jobs(*, now=None, lease: timedelta = timedelta(minutes=15)) -> int:
    """Make interrupted jobs retryable after a worker crash or restart."""
    now = now or timezone.now()
    return TenderComputeJob.objects.filter(
        status=TenderComputeJob.Status.RUNNING,
        started_at__lt=now - lease,
    ).update(status=TenderComputeJob.Status.QUEUED, started_at=None)