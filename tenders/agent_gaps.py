from __future__ import annotations

import json
from pathlib import Path

from .models import AgentGap


GAPS_PATH = Path(__file__).resolve().parents[1] / "agent_gaps.jsonl"


def record_gap(request_summary, category, technical_reason, missing_capability, proposed_resolution=""):
    return AgentGap.objects.create(request_summary=str(request_summary)[:500], category=category, technical_reason=technical_reason, missing_capability=missing_capability, proposed_resolution=proposed_resolution)


def export_gaps(path=GAPS_PATH):
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for gap in AgentGap.objects.order_by("pk"):
            stream.write(json.dumps({"id": gap.pk, "request_summary": gap.request_summary, "category": gap.category, "technical_reason": gap.technical_reason, "missing_capability": gap.missing_capability, "proposed_resolution": gap.proposed_resolution, "status": gap.status}, ensure_ascii=False) + "\n")
    temporary.replace(target)
    return target
