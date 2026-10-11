"""Build the single, reviewable capability-map artifact from live Django metadata."""
from __future__ import annotations

import json
from pathlib import Path

from django.apps import apps


MAP_PATH = Path(__file__).resolve().parents[1] / "capability_map.json"


def _field(field):
    related = getattr(field, "related_model", None)
    return {
        "name": field.name,
        "type": field.get_internal_type(),
        "null": field.null,
        "blank": field.blank,
        "primary_key": field.primary_key,
        "relation": f"{related._meta.app_label}.{related.__name__}" if related else None,
    }


def build_capability_map():
    # Import only after Django has initialised app models and registrations.
    from .assistant_tools import _TOOLS, discover_capabilities

    discover_capabilities()
    models = []
    for model in sorted(apps.get_models(), key=lambda item: (item._meta.app_label, item.__name__)):
        if model._meta.app_label in {"admin", "auth", "contenttypes", "sessions"}:
            continue
        models.append({
            "id": f"{model._meta.app_label}.{model.__name__}",
            "app": model._meta.app_label,
            "model": model.__name__,
            "db_table": model._meta.db_table,
            "fields": [_field(field) for field in model._meta.fields],
            "read": True,
            "standard_crud": {"create": True, "update": True, "delete": True, "confirmation_required": True},
        })
    operations = []
    for tool in sorted(_TOOLS.values(), key=lambda item: item.identifier):
        operations.append({
            "id": tool.identifier,
            "domain": tool.domain,
            "operation": tool.operation,
            "read_only": tool.read_only,
            "requires_confirmation": tool.requires_confirmation,
            "requires_superuser": tool.requires_superuser,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema or {},
            "python_path": f"{tool.executor.__module__}.{tool.executor.__name__}",
        })
    return {"version": 1, "models": models, "operations": operations}


def serialized_map():
    return json.dumps(build_capability_map(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_capability_map(path=MAP_PATH):
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(serialized_map(), encoding="utf-8")
    temporary.replace(target)
    return target
