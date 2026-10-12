"""Runtime encyclopedia of the assistant's real PSADMIN entry points.

It deliberately derives model information from Django and action information from
the single ``AssistantTool`` registry.  Nothing here is an AI-specific business
implementation: entries either point to a real shared service/form or are not
published as executable actions at all.
"""
from __future__ import annotations

def model_catalog():
    """Return the exact read schema enforced by ``assistant_read``."""
    from .assistant_read import catalogue

    return catalogue()


def action_catalog(user):
    # Late import avoids the registry/catalog import cycle.
    from .assistant_tools import available_tools

    return available_tools(user)


def catalog(user):
    return {"models": model_catalog(), "actions": action_catalog(user)}
