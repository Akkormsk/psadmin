from __future__ import annotations

from .models import ComponentRoutePlan, ProviderCalculatorBinding
from .provider_calculators import ProviderCalculatorError, calculate_provider


def _semantic_values(value):
    if isinstance(value, dict):
        if isinstance(value.get("value"), str):
            yield value["value"].casefold().strip()
        for key, nested in value.items():
            if key != "value":
                yield from _semantic_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _semantic_values(nested)
    elif isinstance(value, str):
        yield value.casefold().strip()


def _resolved_variant(component, binding):
    pricing = (binding.knowledge_version.canonical_data or {}).get("pricing", {})
    variants = pricing.get("variants", {})
    values = set(_semantic_values(component.requirements or {}))
    matches = []
    for name, variant in variants.items():
        parts = [str(variant.get(key, "")).casefold().strip() for key in ("product", "cut", "fabric")]
        if all(part and part in values for part in parts):
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def calculate_routed_provider_lines(job):
    calculated = []
    for line in job.lines.select_related("component").filter(component__isnull=False).order_by("pk"):
        route = ComponentRoutePlan.objects.filter(component=line.component).prefetch_related("steps").first()
        if not route:
            continue
        bindings = ProviderCalculatorBinding.objects.filter(
            link__stage_id__in=[step.process_id for step in route.steps.all()],
            link__is_active=True, link__counterparty__is_active=True,
            is_active=True, is_default=True,
        ).select_related("link__counterparty", "knowledge_version").order_by("pk")
        if bindings.count() != 1:
            continue
        binding = bindings.first()
        spec = (line.input_snapshot or {}).get("provider_calculator")
        provenance = {}
        if not isinstance(spec, dict):
            variant = _resolved_variant(line.component, binding)
            if not variant:
                continue
            spec = {"variant": variant}
            provenance = {"provider_match": {"variant": variant, "source": "normalized_component_requirements", "knowledge_version_id": binding.knowledge_version_id}}
        payload = {**spec, "quantity": str(line.component.effective_quantity)}
        try:
            quote = calculate_provider(binding, payload)
        except ProviderCalculatorError as exc:
            line.status = "needs_review"
            line.diagnostics = {**(line.diagnostics or {}), "provider_calculation_error": str(exc)}
            line.save(update_fields=["status", "diagnostics"])
            continue
        line.status = "ready" if quote["status"] == "ready" else quote["status"]
        line.result = {**(line.result or {}), **provenance, "provider_quote": quote, "provider_binding_id": binding.pk}
        line.save(update_fields=["status", "result"])
        calculated.append(line)
    return calculated
