from __future__ import annotations

from .models import ComponentRoutePlan, ProviderCalculatorBinding
from .provider_calculators import ProviderCalculatorError, calculate_provider


def calculate_routed_provider_lines(job):
    calculated = []
    for line in job.lines.select_related("component").filter(component__isnull=False).order_by("pk"):
        spec = (line.input_snapshot or {}).get("provider_calculator")
        if not isinstance(spec, dict):
            continue
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
        payload = {**spec, "quantity": str(line.component.effective_quantity)}
        try:
            quote = calculate_provider(bindings.first(), payload)
        except ProviderCalculatorError as exc:
            line.status = "needs_review"
            line.diagnostics = {**(line.diagnostics or {}), "provider_calculation_error": str(exc)}
            line.save(update_fields=["status", "diagnostics"])
            continue
        line.status = "ready" if quote["status"] == "ready" else quote["status"]
        line.result = {**(line.result or {}), "provider_quote": quote, "provider_binding_id": bindings.first().pk}
        line.save(update_fields=["status", "result"])
        calculated.append(line)
    return calculated
