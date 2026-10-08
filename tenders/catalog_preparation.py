"""Ready-made supplier catalog adapter; the V2 core does not know catalog semantics.

It is intentionally mapped only to capabilities explicitly marked
``supplies_input``. Contractor capabilities need their own adapters; they are
not coerced into catalog search merely to create a preparation task.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import os
import time

from django.conf import settings

from .calculation_v2 import engine_registry
from .catalog import _text_search_pool
from .gateway_budget import spend_rub
from .models import CatalogSupplier
from .preparation import PreparationTask, fingerprint
from .services import _run_name_filter
from .step4_decision_cache import (
    Step4Decision,
    Step4DecisionWrite,
    Step4ProductCandidate,
    bulk_lookup_step4_decisions,
    bulk_store_step4_decisions,
    get_step4_candidate_text,
)


class CatalogPreparationEngine:
    key = "catalog-search-v1"

    def supports_preparation(self, step):
        return bool(step.process.supplies_input)

    def plan_tender_preparation(self, job, steps):
        grouped = defaultdict(list)
        for step in steps:
            component = step.route_plan.component
            if component is not None:
                grouped[(component.name.strip(), fingerprint(component.requirements))].append(step)
        tasks = []
        for (target, requirements_fingerprint), group in grouped.items():
            task_key = fingerprint({"target": target, "requirements": requirements_fingerprint})
            tasks.append(PreparationTask(
                engine_key=self.key,
                dedupe_key=task_key,
                fingerprint=task_key,
                operation_step_ids=tuple(step.pk for step in group),
                payload={"target": target, "requirements_fingerprint": requirements_fingerprint,
                         "operation_step_ids": [step.pk for step in group], "engine_version": self.key},
            ))
        return tasks

    def prepare(self, preparation):
        target = preparation.payload["target"]
        limit = max(1, settings.CALCULATION_V2_PREPARATION_MAX_CANDIDATES_PER_SUPPLIER)
        diagnostics = {"target": target, "suppliers": 0, "candidates": 0, "cache_hits": 0,
                       "cache_misses": 0, "cache_stale": 0, "gemini_candidates": 0,
                       "gemini_requests": 0, "ai_cost_rub": "0", "search_ms": 0, "cache_lookup_ms": 0}
        candidate_state, pending = [], []
        for supplier in CatalogSupplier.objects.filter(is_active=True).only("id", "code"):
            search_started = time.monotonic()
            products = _text_search_pool(supplier.code, [target])[:limit]
            diagnostics["search_ms"] += round((time.monotonic() - search_started) * 1000, 2)
            candidates = [Step4ProductCandidate(str(product.external_id), get_step4_candidate_text(product.full_name or product.name)) for product in products]
            lookup_started = time.monotonic()
            lookup = bulk_lookup_step4_decisions(target=target, supplier=supplier, candidates=candidates)
            diagnostics["cache_lookup_ms"] += round((time.monotonic() - lookup_started) * 1000, 2)
            misses = [candidate for candidate in candidates if candidate.external_id not in lookup.hits]
            diagnostics["suppliers"] += 1
            diagnostics["candidates"] += len(candidates)
            diagnostics["cache_hits"] += len(lookup.hits)
            diagnostics["cache_misses"] += len(misses)
            diagnostics["cache_stale"] += len(lookup.stale_external_ids)
            candidate_state.append({"supplier_id": supplier.pk, "candidate_ids": [candidate.external_id for candidate in candidates]})
            if misses:
                pending.append((supplier, misses))
        total_cost = Decimal("0")
        model = os.getenv("TIMEWEB_AI_MODEL_NAME_FILTER", "gemini/gemini-3.1-flash-lite")
        for supplier, misses in pending:
            if total_cost >= Decimal(str(settings.CALCULATION_V2_PREPARATION_MAX_AI_RUB)):
                diagnostics["budget_deferred"] = len(pending) - diagnostics["gemini_requests"]
                break
            diagnostics["gemini_candidates"] += len(misses)
            usage, writes = {"prompt_tokens": 0, "completion_tokens": 0}, []
            def save_valid_batch(batch, rejected):
                writes.extend(Step4DecisionWrite(str(external_id), candidate_name, Step4Decision.REJECT if external_id in rejected else Step4Decision.PASS, model) for external_id, candidate_name in batch)
            kept = _run_name_filter(target, [(candidate.external_id, candidate.candidate_name) for candidate in misses], usage=usage, model=model, on_valid_batch=save_valid_batch)
            if writes:
                bulk_store_step4_decisions(target=target, supplier=supplier, decisions=writes)
                diagnostics["gemini_requests"] += 1
            if kept is None:
                diagnostics["provider_failures"] = diagnostics.get("provider_failures", 0) + 1
            total_cost += Decimal(str(spend_rub(usage, model) or 0))
        diagnostics["ai_cost_rub"] = str(total_cost)
        diagnostics["search_ms"] = round(diagnostics["search_ms"], 2)
        diagnostics["cache_lookup_ms"] = round(diagnostics["cache_lookup_ms"], 2)
        preparation.payload = {**preparation.payload, "diagnostics": diagnostics, "candidate_state": candidate_state}
        preparation.cost = total_cost
        preparation.status = "ready" if not diagnostics.get("provider_failures") and not diagnostics.get("budget_deferred") else "partial"
        preparation.save(update_fields=["payload", "cost", "status"])
        return {"status": preparation.status, "diagnostics": diagnostics}


if CatalogPreparationEngine.key not in engine_registry.keys():
    engine_registry.register(CatalogPreparationEngine())
