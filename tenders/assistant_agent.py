from __future__ import annotations

import json
import os

from .assistant_tools import AssistantToolConfirmationRequired, AssistantToolError, available_tools, execute_tool, required_input_names, workflow_catalog
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


def reply(conversation, user, message, page_context=None):
    """Lets the model choose from the registry; execution remains backend-only."""
    tools = available_tools(user)
    planning_tools = _planning_tools(tools, user)
    workflows = workflow_catalog(user, tools)
    read_catalog = []
    if any(tool["id"] == "read" and tool["available"] for tool in tools):
        # This is the same metadata source the ORM executor validates against.
        from .assistant_read import catalogue
        read_catalog = catalogue()
    pending = conversation.context.get("pending_action", {})
    prompt = json.dumps(
        {
            # 2026-10-13: Старый промпт превращал AI в диспетчера инструмента;
            # теперь разрешаем самостоятельное обсуждение и уточнения, сохраняя
            # лимит в один инструмент за ход и все серверные проверки.
            # Было:
            # "task": "You are a concise Russian business assistant. Understand the user's request naturally. Choose at most one action only from tools. Select the tool for the direct request, not a preparatory lookup: a list/read tool is valid only when the user explicitly asks to view that list. Never claim an action happened until tool_result is supplied. When the user asks to start a tool with ui, select that tool immediately: its declared form collects the required inputs and the tool is not executed until that form is confirmed. Copy values explicitly stated by the user into ui action arguments; select an option only by its exact id from the supplied ui options. For another tool, if required inputs or confirmation are missing, explain what is needed and action must be null.",
            "task": (
                "You are PSADMIN's Russian-speaking business assistant. "
                "Your main goal is to understand the user's intent and help solve their task. "
                "You may reason about the problem, discuss alternatives, explain decisions, "
                "ask clarifying questions, and guide multi-step work naturally. "
                "Do not force tool usage during an ordinary conversation. "
                "Ask preferably one useful question at a time. "
                "Use the conversation history and provided context; do not ask again for information already given. "
                "You may propose a plan and continue discussing it over several user messages. "
                "Use tools only when you need real PSADMIN data or must execute an existing business operation. "
                "Choose at most one tool action per assistant response; "
                "the backend does not support multiple tool calls in one turn. "
                "For tools with a declared UI, select the tool and let its existing UI collect missing inputs. "
                "For other tools, when required arguments are missing, ask for clarification "
                "and return action=null until they are available. "
                "Never invent database records, prices, tool results, or completed actions. "
                "Never claim an operation succeeded without a real tool result. "
                "Only registered tools may be executed. "
                "Preserve all backend permission checks and required confirmations. "
                "Return valid JSON matching response_schema."
            ),
            "tools": planning_tools,
            "workflows": workflows,
            "read_catalog": read_catalog,
            "conversation_context": conversation.context.get("business_context", {}),
            "pending_action": pending,
            "page_context": page_context or {},
            "history": _history(conversation),
            "message": message,
            "response_schema": {"reply": "Russian text", "action": {"id": "tool id or null", "arguments": "object"}, "task_status": "continue|cancel"},
        },
        ensure_ascii=False,
    )
    preflight()
    plan = _request(prompt)
    if not isinstance(plan, dict) or not isinstance(plan.get("reply"), str):
        raise TenderAIError("Ассистент вернул неполный ответ. Попробуйте ещё раз.")
    if pending and plan.get("task_status") == "cancel":
        conversation.context.pop("pending_action", None)
        conversation.save(update_fields=["context"])
        return {"text": plan["reply"].strip() or "Задача отменена.", "kind": "text", "data": {}}
    action = plan.get("action")
    if not isinstance(action, dict) or not action.get("id"):
        if pending:
            return {"text": (plan["reply"].strip() or "Уточните недостающие параметры задачи."), "kind": "text", "data": {"pending_tool_id": pending.get("tool_id")}}
        return {"text": plan["reply"].strip(), "kind": "text", "data": {}}
    identifier = action.get("id")
    arguments = action.get("arguments", {})
    if not isinstance(identifier, str) or not isinstance(arguments, dict):
        raise TenderAIError("Ассистент вернул некорректное действие.")
    if identifier not in {tool["id"] for tool in tools}:
        raise TenderAIError("Ассистент запросил действие вне реестра.")
    if pending and pending.get("tool_id") == identifier and isinstance(pending.get("arguments"), dict):
        arguments = {**pending["arguments"], **arguments}
    selected_tool = next(tool for tool in tools if tool["id"] == identifier)
    if selected_tool.get("ui"):
        data = {"tool_id": identifier}
        for name, source in selected_tool["ui"].get("options", {}).items():
            values = execute_tool(source["tool"], user, {})
            data[name] = values.get(source["field"], [])
        for name, field_type in selected_tool["ui"].get("fields", {}).items():
            value = arguments.get(name)
            if field_type == "string" and isinstance(value, str) and value.strip():
                data[name] = value.strip()
            if field_type == "integer" and isinstance(value, int) and any(item.get("id") == value for item in data.get("stages", [])):
                data[name] = value
        return {"text": plan["reply"].strip(), "kind": selected_tool["ui"]["kind"], "data": data}
    missing = [name for name in required_input_names(identifier) if arguments.get(name) in (None, "", [])]
    if missing:
        conversation.context["pending_action"] = {"tool_id": identifier, "arguments": arguments}
        conversation.save(update_fields=["context"])
        labels = ", ".join(missing)
        text = plan["reply"].strip() or f"Уточните: {labels}."
        if labels not in text:
            text = f"{text}\nНужны параметры: {labels}."
        return {"text": text, "kind": "text", "data": {"pending_tool_id": identifier, "missing": missing}}
    if pending:
        conversation.context.pop("pending_action", None)
        conversation.save(update_fields=["context"])
    try:
        result = execute_tool(identifier, user, arguments)
    except AssistantToolConfirmationRequired:
        return {"text": plan["reply"].strip(), "kind": "confirmation_required", "data": {"tool_id": identifier, "arguments": arguments}}
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
