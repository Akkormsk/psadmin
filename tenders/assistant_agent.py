from __future__ import annotations

import json
import os
import logging
from uuid import uuid4

_logger = logging.getLogger("psadmin.assistant.read")

def _plan_shape(arguments):
    """Operational trace of generated READ structure, never filter values."""
    plan = arguments.get("plan") if isinstance(arguments, dict) else None
    if not isinstance(plan, dict):
        return {"plan_type": type(plan).__name__, "argument_keys": sorted(arguments) if isinstance(arguments, dict) else []}
    return {"entity": plan.get("entity") if isinstance(plan.get("entity"), str) else None,
            "keys": sorted(plan),
            "fields": plan.get("fields") if isinstance(plan.get("fields"), list) and all(isinstance(v, str) for v in plan["fields"]) else "invalid-or-omitted",
            "filter_fields": [v.get("field") for v in plan.get("filters", []) if isinstance(v, dict) and isinstance(v.get("field"), str)] if isinstance(plan.get("filters"), list) else "invalid-or-omitted",
            "filter_ops": [v.get("op") for v in plan.get("filters", []) if isinstance(v, dict) and isinstance(v.get("op"), str)] if isinstance(plan.get("filters"), list) else "invalid-or-omitted",
            "order_by": plan.get("order_by") if isinstance(plan.get("order_by"), str) else None,
            "limit_type": type(plan.get("limit")).__name__, "limit": plan.get("limit") if type(plan.get("limit")) is int else None}

from .assistant_tools import AssistantToolConfirmationRequired, AssistantToolError, available_tools, execute_tool, required_input_names
from .gateway_budget import preflight, spend_rub
from .services import TenderAIError, _ai_gateway_json


# 2026-10-13: Раньше брались первые 20 событий, поэтому новые сообщения терялись.
# Прежняя реализация (оставлена для истории):
# def _history(conversation):
#     return [
#         {"role": (event.payload or {}).get("role", "user"), "text": event.raw_text}
#         for event in conversation.feedback_events.order_by("created_at", "pk")[:20]
#     ]
def _history(conversation):
    events = list(
        conversation.feedback_events.order_by("-created_at", "-pk")[:20]
    )
    return [
        {
            "role": (event.payload or {}).get("role", "user"),
            "text": event.raw_text,
        }
        for event in reversed(events)
    ]


def _request(prompt):
    model = os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", os.getenv("TIMEWEB_AI_MODEL", "gemini/gemini-3.1-flash-lite"))
    # 2026-10-13: Увеличен предел ответа для более содержательного диалога.
    # Было: result, usage = _ai_gateway_json(prompt, model=model, max_tokens=700, timeout=45, network_attempts=1)
    result, usage = _ai_gateway_json(prompt, model=model, max_tokens=1800, timeout=45, network_attempts=1)
    spend_rub(usage, model)
    return result


def _planning_tools(tools, user):
    prepared = []
    for tool in tools:
        item = dict(tool)
        if tool.get("ui"):
            ui = dict(tool["ui"])
            options = {}
            for name, source in ui.get("options", {}).items():
                options[name] = execute_tool(source["tool"], user, {}).get(source["field"], [])
            ui["options"] = options
            item["ui"] = ui
        prepared.append(item)
    return prepared


MAX_AGENT_STEPS = 5


def _result_fallback(observations, trace_id):
    """Answer from verified adapter output even when the language model fails."""
    for item in reversed(observations):
        result = item.get("result", {})
        if isinstance(result, dict) and isinstance(result.get("display_text"), str) and result["display_text"].strip():
            return result["display_text"].strip()
    if observations:
        return "Данные получены, но оформить ответ не удалось. Код диагностики: " + trace_id + "."
    return "Не удалось выполнить запрос. Код диагностики: " + trace_id + "."


def reply(conversation, user, message, page_context=None):
    """A bounded multi-step agent over registered business actions only.

    The model chooses what to do; Django owns permissions, data, and confirmations.
    The legacy technical ORM 'read' plan is deliberately NOT advertised.
    """
    trace_id = uuid4().hex[:10]
    registry = [
        entry for entry in available_tools(user)
        if entry.get("available") and entry["id"] != "read"
    ]
    planning_tools = _planning_tools(registry, user)
    from .assistant_catalog import workflow_catalog
    workflow = workflow_catalog(user, registry)
    pending = conversation.context.get("pending_action") or {}
    observations = []
    last_read = conversation.context.get("last_agent_read")
    if isinstance(last_read, dict) and last_read.get("tool_id") in {t["id"] for t in registry if t["read_only"]}:
        observations.append({"tool_id": last_read["tool_id"], "result": last_read.get("result", {}), "prior_turn": True})
    user_message = message
    instruction = (
        "You are the Russian-language PSADMIN assistant. Talk naturally with the user. "
        "You can call several registered tools in sequence, observing each result before "
        "choosing whether another tool is needed. A tool call does not finish the turn. "
        "Choose tools by their business descriptions, using only their public input_schema. "
        "Never invent Django model names, ORM field names, SQL, or unregistered tools. "
        "The backend supplies every tool's result and all exact calculations. "
        "For data requests call the relevant business read tool, not a future-tense promise. "
        "After obtaining data, answer the complete user request briefly and clearly in Russian. "
        "Do not show internal metadata, code, execution details, or invented information. "
        "If data is missing, ask one useful clarification. "
        "Never claim a write happened unless confirmed by the backend. "
        "For changing operations choose a declared action and stop for user confirmation; "
        "never attempt to bypass confirmation. "
        "You must return a JSON object with reply (string), action (object or null), "
        "and optional task_status (continue|cancel). "
        "Use action=null when you are done answering or need clarification."
    )
    previous = set()
    failures = 0
    success_count = 0
    for step in range(MAX_AGENT_STEPS + 1):
        payload = {
            "task": instruction,
            "tools": planning_tools,
            "workflows": workflow,
            "conversation_context": conversation.context.get("business_context", {}),
            "pending_action": pending,
            "page_context": page_context or {},
            "history": _history(conversation),
            "message": user_message,
            "observations": observations[-MAX_AGENT_STEPS:],
            "remaining_tool_calls": MAX_AGENT_STEPS - step,
            "response_schema": {
                "reply": "Short Russian final response or clarification",
                "action": {"id": "registered tool id or null", "arguments": "object"},
                "task_status": "continue|cancel",
            },
        }
        try:
            preflight()
            decision = _request(json.dumps(payload, ensure_ascii=False))
        except Exception:
            _logger.exception("AGENT trace=%s step=%s failed to generate decision", trace_id, step)
            return {"text": _result_fallback([x for x in observations if not x.get("prior_turn")], trace_id),
                    "kind": "text", "data": {"trace_id": trace_id}}
        if not isinstance(decision, dict):
            _logger.warning("AGENT trace=%s step=%s invalid decision type", trace_id, step)
            return {"text": _result_fallback([x for x in observations if not x.get("prior_turn")], trace_id),
                    "kind": "text", "data": {"trace_id": trace_id}}
        if pending and decision.get("task_status") == "cancel":
            conversation.context.pop("pending_action", None)
            conversation.save(update_fields=["context"])
            return {"text": decision.get("reply") or "Действие отменено.", "kind": "text", "data": {}}
        action = decision.get("action")
        identifier = action.get("id") if isinstance(action, dict) else None
        if not identifier or step == MAX_AGENT_STEPS:
            final_text = decision.get("reply", "")
            # A registered adapter's verified rendering is authoritative on reads.
            fresh = [x for x in observations if not x.get("prior_turn")]
            if fresh and isinstance(fresh[-1].get("result", {}), dict) and fresh[-1]["result"].get("display_text"):
                final_text = fresh[-1]["result"]["display_text"]
            if not isinstance(final_text, str) or not final_text.strip():
                final_text = _result_fallback(fresh, trace_id)
            return {"text": final_text.strip(), "kind": "text", "data": {}}
        entry = next((t for t in registry if t["id"] == identifier), None)
        args = action.get("arguments")
        if not entry or not isinstance(args, dict):
            observations.append({"tool_id": str(identifier)[:80], "error": "Unknown tool or invalid arguments; use the published tool schema."})
            failures += 1
            if failures >= 2:
                return {"text": _result_fallback([x for x in observations if "result" in x and not x.get("prior_turn")], trace_id),
                        "kind": "text", "data": {"trace_id": trace_id}}
            continue
        if pending and pending.get("tool_id") == identifier and isinstance(pending.get("arguments"), dict):
            args = {**pending["arguments"], **args}
        if entry.get("ui"):
            data = {"tool_id": identifier}
            for name, source in entry["ui"].get("options", {}).items():
                values = execute_tool(source["tool"], user, {})
                data[name] = values.get(source["field"], [])
            for name, field_type in entry["ui"].get("fields", {}).items():
                value = args.get(name)
                if field_type == "string" and isinstance(value, str) and value.strip():
                    data[name] = value.strip()
                if field_type == "integer" and isinstance(value, int) and any(item.get("id") == value for item in data.get("stages", [])):
                    data[name] = value
            return {"text": decision.get("reply") or "Уточните параметры.", "kind": entry["ui"]["kind"], "data": data}
        missing = [name for name in required_input_names(identifier) if args.get(name) in (None, "", [])]
        if missing:
            if not entry["read_only"]:
                conversation.context["pending_action"] = {"tool_id": identifier, "arguments": args}
                conversation.save(update_fields=["context"])
                return {"text": decision.get("reply") or "Уточните: " + ", ".join(missing),
                        "kind": "text", "data": {"pending_tool_id": identifier, "missing": missing}}
            observations.append({"tool_id": identifier, "error": "Missing required input: " + ", ".join(missing)})
            failures += 1
            continue
        if not entry["read_only"]:
            # Never execute a mutation inside the autonomous loop.
            return {"text": decision.get("reply") or "Подтвердите действие.",
                    "kind": "confirmation_required",
                    "data": {"tool_id": identifier, "arguments": args}}
        signature = json.dumps([identifier, args], sort_keys=True, ensure_ascii=False, default=str)
        if signature in previous:
            observations.append({"tool_id": identifier, "error": "Repeated identical read; use existing result or choose another tool."})
            failures += 1
            if failures >= 2:
                return {"text": _result_fallback([x for x in observations if "result" in x and not x.get("prior_turn")], trace_id),
                        "kind": "text", "data": {"trace_id": trace_id}}
            continue
        previous.add(signature)
        try:
            result = execute_tool(identifier, user, args)
        except (AssistantToolError, ValueError, TypeError) as exc:
            _logger.warning("AGENT trace=%s tool=%s rejected reason=%s", trace_id, identifier, str(exc)[:180])
            observations.append({"tool_id": identifier, "error": str(exc)[:250]})
            failures += 1
            if failures >= 2:
                return {"text": _result_fallback([x for x in observations if "result" in x and not x.get("prior_turn")], trace_id),
                        "kind": "text", "data": {"trace_id": trace_id}}
            continue
        success_count += 1
        observations.append({"tool_id": identifier, "result": result})
        _logger.info("AGENT trace=%s read=%s completed step=%s", trace_id, identifier, step)
        # Retain one verified read for natural follow-up questions, never a write.
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            conversation.context["last_agent_read"] = {"tool_id": identifier, "result": result}
            conversation.save(update_fields=["context"])
    return {"text": _result_fallback([x for x in observations if "result" in x and not x.get("prior_turn")], trace_id),
            "kind": "text", "data": {"trace_id": trace_id}}
