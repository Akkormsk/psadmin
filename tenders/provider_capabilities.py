"""V2 bridge to the existing ProcessDefinition ↔ Counterparty capability data."""
from __future__ import annotations

from dataclasses import dataclass

from .calculation_v2 import engine_registry
from .models import ComponentOperationStep, StageCounterpartyLink, TenderComputePreparation
from .preparation import PreparationTask, fingerprint


@dataclass(frozen=True)
class ProviderCandidate:
    counterparty_id: int
    counterparty_name: str
    link_id: int
    process_id: int
    priority: int
    price_source_type: str
    settings_fingerprint: str


def resolve_provider_candidates(component, operation_step: ComponentOperationStep, context=None) -> tuple[ProviderCandidate, ...]:
    """Return active providers for an external execution capability.

    The link, rather than a new provider registry, remains the source of
    provider-specific restrictions and future pricing/source configuration.
    """
    links = (
        StageCounterpartyLink.objects.filter(
            stage_id=operation_step.process_id,
            is_active=True,
            counterparty__is_active=True,
        )
        .select_related("counterparty")
        .order_by("priority", "counterparty__name", "pk")
    )
    return tuple(
        ProviderCandidate(
            counterparty_id=link.counterparty_id,
            counterparty_name=link.counterparty.name,
            link_id=link.pk,
            process_id=link.stage_id,
            priority=link.priority,
            price_source_type=link.price_source_type,
            settings_fingerprint=fingerprint(link.settings),
        )
        for link in links
    )


class CounterpartyCapabilityPreparationEngine:
    """Persist local provider candidates; it never sends a quote request."""

    key = "counterparty-capability-v1"

    def supports_preparation(self, step):
        return bool(resolve_provider_candidates(step.route_plan.component, step))

    def plan_tender_preparation(self, job, steps):
        tasks = []
        for step in steps:
            candidates = resolve_provider_candidates(step.route_plan.component, step)
            if not candidates:
                continue
            state = {
                "operation_step_id": step.pk,
                "component_id": step.route_plan.component_id,
                "process_id": step.process_id,
                "candidate_links": [
                    {"link_id": candidate.link_id, "counterparty_id": candidate.counterparty_id,
                     "settings_fingerprint": candidate.settings_fingerprint}
                    for candidate in candidates
                ],
            }
            task_key = fingerprint(state)
            tasks.append(PreparationTask(
                engine_key=self.key,
                dedupe_key=task_key,
                fingerprint=task_key,
                operation_step_ids=(step.pk,),
                payload={"operation_step_ids": [step.pk], "engine_version": self.key},
            ))
        return tasks

    def prepare(self, preparation: TenderComputePreparation):
        states = []
        for step_id in preparation.payload.get("operation_step_ids", []):
            step = ComponentOperationStep.objects.select_related("route_plan__component").get(pk=step_id)
            candidates = resolve_provider_candidates(step.route_plan.component, step)
            states.append({
                "operation_step_id": step.pk,
                "component_id": step.route_plan.component_id,
                "process_id": step.process_id,
                "candidates": [candidate.__dict__ for candidate in candidates],
            })
        preparation.payload = {**preparation.payload, "provider_candidates": states}
        preparation.status = "ready"
        preparation.save(update_fields=["payload", "status"])
        return {"status": "ready", "candidate_provider_count": sum(len(state["candidates"]) for state in states)}


if CounterpartyCapabilityPreparationEngine.key not in engine_registry.keys():
    engine_registry.register(CounterpartyCapabilityPreparationEngine())
