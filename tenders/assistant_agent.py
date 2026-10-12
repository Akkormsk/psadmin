from __future__ import annotations

import json
import os

from .assistant_tools import AssistantToolConfirmationRequired, AssistantToolError, available_tools, execute_tool
from .gateway_budget import preflight, spend_rub
from .services import TenderAIError, _ai_gateway_json


def _history(conversation):
    return [
        {"role": (event.payload or {}).get("role", "user"), "text": event.raw_text}
        for event in conversation.feedback_events.order_by("created_at", "pk")[:20]
    ]


def _request(prompt):
    model = os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", os.getenv("TIMEWEB_AI_MODEL", "gemini/gemini-3.1-flash-lite"))
    result, usage = _ai_gateway_json(prompt, model=model, max_tokens=700, timeout=45, network_attempts=1)
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


def reply(conversation, user, message, page_context=None):
    """Lets the model choose from the registry; execution remains backend-only."""
    tools = available_tools(user)
    planning_tools = _planning_tools(tools, user)
    from .models import KnowledgeRecord
    confirmed_knowledge = [
        {"id": item.pk, "scope_type": item.scope_type, "scope_context": item.scope_context, "applicability": item.applicability, "payload": item.payload}
        for item in KnowledgeRecord.objects.filter(status="confirmed").order_by("-updated_at", "-pk")[:20]
    ]
    prompt = json.dumps(
        {
            "task": "You are a concise Russian business assistant. Understand the user's request naturally and choose only registered capabilities. You may return up to four ordered actions when each later action can use only explicitly supplied arguments, not guessed values. Use data.query for arbitrary permitted analysis; it accepts only its documented JSON plan, never SQL. Never claim an action happened until tool_result is supplied. A mutating or confirmation-required capability must be selected only to prepare its server-side confirmation, never executed from text. When a tool has ui, select it immediately so its declared form collects parameters. If a needed value is unknown, ask one concrete question and return no actions.",
            "tools": planning_tools,
            "conversation_context": conversation.context.get("business_context", {}),
            "confirmed_knowledge": confirmed_knowledge,
            "page_context": page_context or {},
            "history": _history(conversation),
            "message": message,
            "response_schema": {"reply": "Russian text", "actions": [{"id": "tool id", "arguments": "object"}], "action": "legacy single action or null"},
        },
        ensure_ascii=False,
    )
    preflight()
    plan = _request(prompt)
    if not isinstance(plan, dict) or not isinstance(plan.get("reply"), str):
        raise TenderAIError("Ассистент вернул неполный ответ. Попробуйте ещё раз.")
    actions = plan.get("actions")
    if actions is None:
        action = plan.get("action")
        actions = [action] if isinstance(action, dict) and action.get("id") else []
    if not isinstance(actions, list) or len(actions) > 4:
        raise TenderAIError("Ассистент вернул некорректный план действий.")
    if not actions:
        return {"text": plan["reply"].strip(), "kind": "text", "data": {}}
    selected = []
    results = []
    for action in actions:
        identifier = action.get("id") if isinstance(action, dict) else None
        arguments = action.get("arguments", {}) if isinstance(action, dict) else None
        if not isinstance(identifier, str) or not isinstance(arguments, dict) or identifier not in {tool["id"] for tool in tools}:
            raise TenderAIError("Ассистент запросил действие вне реестра.")
        selected_tool = next(tool for tool in tools if tool["id"] == identifier)
        if identifier == "data.mutate":
            from .agent_crud import CrudError, preview

            try:
                data = preview(conversation, user, arguments)
            except CrudError as exc:
                return {"text": str(exc), "kind": "tool_error", "data": {"tool_id": identifier}}
            return {"text": plan["reply"].strip(), "kind": "confirmation", "data": data}
        if selected_tool.get("ui"):
            data = {"tool_id": identifier}
            for name, source in selected_tool["ui"].get("options", {}).items():
                data[name] = execute_tool(source["tool"], user, {}).get(source["field"], [])
            for name, field_type in selected_tool["ui"].get("fields", {}).items():
                value = arguments.get(name)
                if field_type == "string" and isinstance(value, str) and value.strip(): data[name] = value.strip()
                if field_type == "integer" and isinstance(value, int) and any(item.get("id") == value for item in data.get("stages", [])): data[name] = value
            return {"text": plan["reply"].strip(), "kind": selected_tool["ui"]["kind"], "data": data}
        try:
            result = execute_tool(identifier, user, arguments)
        except AssistantToolConfirmationRequired:
            return {"text": plan["reply"].strip(), "kind": "confirmation_required", "data": {"tool_id": identifier}}
        except AssistantToolError as exc:
            return {"text": str(exc), "kind": "tool_error", "data": {"tool_id": identifier}}
        selected.append(selected_tool)
        results.append({"tool_id": identifier, "result": result})
    final = _request(
        json.dumps(
            {
                "task": "Answer in concise Russian using only tool_results. Do not invent data or promise further execution. Cite returned record IDs when available.",
                "draft": plan["reply"],
                "tools": selected,
                "tool_results": results,
                "response_schema": {"reply": "Russian text"},
            },
            ensure_ascii=False,
        )
    )
    if not isinstance(final, dict) or not isinstance(final.get("reply"), str):
        raise TenderAIError("Ассистент не смог сформировать ответ по результату действия.")
    return {"text": final["reply"].strip(), "kind": "tool_result", "data": {"tool_id": results[-1]["tool_id"], "result": results[-1]["result"], "results": results}}
