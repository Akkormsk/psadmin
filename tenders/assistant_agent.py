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


def reply(conversation, user, message):
    """Lets the model choose from the registry; execution remains backend-only."""
    tools = available_tools(user)
    prompt = json.dumps(
        {
            "task": "You are a concise Russian business assistant. Understand the user's request naturally. Choose at most one action only from tools. Select the tool for the direct request, not a preparatory lookup: a list/read tool is valid only when the user explicitly asks to view that list. Never claim an action happened until tool_result is supplied. When the user asks to start a tool with ui, select that tool immediately: its declared form collects the required inputs and the tool is not executed until that form is confirmed. For another tool, if required inputs or confirmation are missing, explain what is needed and action must be null.",
            "tools": tools,
            "context": conversation.context.get("business_context", {}),
            "history": _history(conversation),
            "message": message,
            "response_schema": {"reply": "Russian text", "action": {"id": "tool id or null", "arguments": "object"}},
        },
        ensure_ascii=False,
    )
    preflight()
    plan = _request(prompt)
    if not isinstance(plan, dict) or not isinstance(plan.get("reply"), str):
        raise TenderAIError("Ассистент вернул неполный ответ. Попробуйте ещё раз.")
    action = plan.get("action")
    if not isinstance(action, dict) or not action.get("id"):
        return {"text": plan["reply"].strip(), "kind": "text", "data": {}}
    identifier = action.get("id")
    arguments = action.get("arguments", {})
    if not isinstance(identifier, str) or not isinstance(arguments, dict):
        raise TenderAIError("Ассистент вернул некорректное действие.")
    if identifier not in {tool["id"] for tool in tools}:
        raise TenderAIError("Ассистент запросил действие вне реестра.")
    selected_tool = next(tool for tool in tools if tool["id"] == identifier)
    if selected_tool.get("ui"):
        data = {"tool_id": identifier}
        for name, source in selected_tool["ui"].get("options", {}).items():
            values = execute_tool(source["tool"], user, {})
            data[name] = values.get(source["field"], [])
        return {"text": plan["reply"].strip(), "kind": selected_tool["ui"]["kind"], "data": data}
    try:
        result = execute_tool(identifier, user, arguments)
    except AssistantToolConfirmationRequired:
        return {"text": plan["reply"].strip(), "kind": "confirmation_required", "data": {"tool_id": identifier}}
    except AssistantToolError as exc:
        return {"text": str(exc), "kind": "tool_error", "data": {"tool_id": identifier}}
    final = _request(
        json.dumps(
            {
                "task": "Answer in concise Russian using only tool_result. Do not invent data or promise further execution.",
                "draft": plan["reply"],
                "tool": selected_tool,
                "tool_result": result,
                "response_schema": {"reply": "Russian text"},
            },
            ensure_ascii=False,
        )
    )
    if not isinstance(final, dict) or not isinstance(final.get("reply"), str):
        raise TenderAIError("Ассистент не смог сформировать ответ по результату действия.")
    return {"text": final["reply"].strip(), "kind": "tool_result", "data": {"tool_id": identifier, "result": result}}
