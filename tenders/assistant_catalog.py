"""Runtime encyclopedia of the assistant's real PSADMIN entry points.

It deliberately derives model information from Django and action information from
the single ``AssistantTool`` registry.  Nothing here is an AI-specific business
implementation: entries either point to a real shared service/form or are not
published as executable actions at all.
"""
from __future__ import annotations

from django.apps import apps


SYSTEM_APPS = {"admin", "auth", "contenttypes", "sessions"}


def model_catalog():
    """Return all business models and their concrete fields for planning/read UI."""
    items = []
    for model in apps.get_models():
        if model._meta.app_label in SYSTEM_APPS or model._meta.auto_created:
            continue
        fields = []
        for field in model._meta.concrete_fields:
            if field.primary_key:
                continue
            fields.append({
                "name": field.name,
                "type": field.get_internal_type(),
                "relation": field.related_model._meta.label if field.is_relation and field.related_model else None,
                "nullable": field.null,
                "editable": field.editable,
            })
        items.append({"model": model._meta.label, "verbose_name": str(model._meta.verbose_name), "fields": fields})
    return sorted(items, key=lambda item: item["model"])


def action_catalog(user):
    # Late import avoids the registry/catalog import cycle.
    from .assistant_tools import available_tools

    return available_tools(user)


def catalog(user):
    return {"models": model_catalog(), "actions": action_catalog(user)}
