from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import unicodedata

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import CatalogSupplier, Step4DecisionCache


Step4Decision = Step4DecisionCache.Decision


@dataclass(frozen=True)
class Step4ProductCandidate:
    external_id: str
    candidate_name: str


@dataclass(frozen=True)
class Step4DecisionWrite:
    external_id: str
    candidate_name: str
    decision: str
    model_name: str


@dataclass(frozen=True)
class Step4DecisionLookup:
    hits: dict[str, Step4DecisionCache]
    stale_external_ids: set[str]


def normalize_target_signature(target: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(target or "")).casefold().split())


def get_step4_candidate_text(candidate_name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(candidate_name or "")).split())[:120]


def candidate_signature(candidate_name: str) -> str:
    return sha256(get_step4_candidate_text(candidate_name).encode("utf-8")).hexdigest()


def bulk_lookup_step4_decisions(*, target: str, supplier: CatalogSupplier, candidates: list[Step4ProductCandidate], contract_version: str | None = None, stale_days: int | None = None) -> Step4DecisionLookup:
    if not settings.STEP4_DECISION_CACHE_ENABLED or not candidates:
        return Step4DecisionLookup(hits={}, stale_external_ids=set())

    contract_version = contract_version or settings.STEP4_DECISION_CACHE_CONTRACT_VERSION
    supplier_id = getattr(supplier, "pk", supplier)
    stale_days = settings.STEP4_DECISION_CACHE_STALE_DAYS if stale_days is None else stale_days
    pairs = {(str(candidate.external_id), candidate_signature(candidate.candidate_name)) for candidate in candidates}
    records = Step4DecisionCache.objects.filter(target_signature=normalize_target_signature(target), supplier_id=supplier_id, contract_version=contract_version, product_external_id__in=[external_id for external_id, _signature in pairs], candidate_signature__in=[signature for _external_id, signature in pairs])
    stale_before = timezone.now() - timedelta(days=max(0, stale_days))
    hits, stale_external_ids, hit_ids = {}, set(), []
    for record in records:
        if (record.product_external_id, record.candidate_signature) not in pairs:
            continue
        if record.last_verified_at < stale_before:
            stale_external_ids.add(record.product_external_id)
            continue
        hits[record.product_external_id] = record
        hit_ids.append(record.pk)
    if hit_ids:
        Step4DecisionCache.objects.filter(pk__in=hit_ids).update(last_used_at=timezone.now(), hit_count=F("hit_count") + 1)
    return Step4DecisionLookup(hits=hits, stale_external_ids=stale_external_ids)


def bulk_store_step4_decisions(*, target: str, supplier: CatalogSupplier, decisions: list[Step4DecisionWrite], contract_version: str | None = None) -> None:
    if not settings.STEP4_DECISION_CACHE_ENABLED or not decisions:
        return

    contract_version = contract_version or settings.STEP4_DECISION_CACHE_CONTRACT_VERSION
    supplier_id = getattr(supplier, "pk", supplier)
    valid_decisions = set(Step4Decision.values)
    entries = {}
    for decision in decisions:
        if decision.decision not in valid_decisions:
            raise ValueError("Only PASS and REJECT Step 4 decisions may be cached")
        external_id, signature = str(decision.external_id), candidate_signature(decision.candidate_name)
        entries[(external_id, signature)] = decision

    target_signature, now = normalize_target_signature(target), timezone.now()
    Step4DecisionCache.objects.bulk_create([Step4DecisionCache(target_signature=target_signature, supplier_id=supplier_id, product_external_id=external_id, candidate_signature=signature, decision=decision.decision, model_name=decision.model_name, contract_version=contract_version, last_verified_at=now) for (external_id, signature), decision in entries.items()], ignore_conflicts=True)
    existing = Step4DecisionCache.objects.filter(target_signature=target_signature, supplier_id=supplier_id, contract_version=contract_version, product_external_id__in=[external_id for external_id, _signature in entries], candidate_signature__in=[signature for _external_id, signature in entries])
    to_update = []
    for record in existing:
        decision = entries.get((record.product_external_id, record.candidate_signature))
        if decision is None:
            continue
        record.decision, record.model_name = decision.decision, decision.model_name
        record.last_verified_at, record.updated_at = now, now
        to_update.append(record)
    if to_update:
        Step4DecisionCache.objects.bulk_update(to_update, ["decision", "model_name", "last_verified_at", "updated_at"])
