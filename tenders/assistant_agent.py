"""Standard OpenAI Function Calling loop for PSADMIN."""
from __future__ import annotations
import json, os
from urllib.request import Request, urlopen
from .assistant_tools import AssistantToolError, available_tools, execute_tool, openai_functions, validate_tool_call
from .gateway_budget import preflight
from .services import TenderAIError

MAX_TOOL_CALLS = 8
MAX_CONTEXT_MESSAGES = 40

def _debug(conversation, stage, **payload):
    from .assistant_conversations import add_debug_event
    add_debug_event(conversation, stage, event_input=payload.get("input"), event_output=payload.get("output"), error=payload.get("error", ""))

def _gateway(messages, tools):
    key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    if not key:
        raise TenderAIError("AI Gateway не настроен.")
    body = {"model": os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "openai/gpt-4.1-mini"), "messages": messages, "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 1200}
    request = Request(f"{os.getenv('TIMEWEB_AI_BASE_URL', 'https://api.timeweb.ai/v1').rstrip('/')}/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode())
            message = payload["choices"][0]["message"]
            if isinstance(payload.get("usage"), dict):
                message = {**message, "_gateway_usage": payload["usage"]}
            return message
    except Exception as exc:
        raise TenderAIError("Не удалось обратиться к AI Gateway.") from exc

def _history(conversation):
    events = list(conversation.feedback_events.filter(scope="assistant_conversation").order_by("-created_at", "-pk")[:20])
    return [{"role": (x.payload or {}).get("role", "user"), "content": x.raw_text} for x in reversed(events)]

def _new_state(conversation, message, attachments):
    history = _history(conversation)
    if not history or history[-1] != {"role": "user", "content": message}:
        history.append({"role": "user", "content": message})
    return {"messages": [{"role": "system", "content": "You are PSADMIN's Russian-speaking business assistant. Reply naturally in Russian. Use functions for PSADMIN data or operations; never invent facts, fields or IDs. Ask only necessary business clarifications in ordinary Russian. Do not expose technical parameters. Actions require explicit user confirmation."}, *history], "steps": [], "attachments": attachments or []}

def _save(conversation, state):
    conversation.context["agent_run"] = state
    conversation.save(update_fields=["context"])

def _remember_user_message(state, message):
    if not state["messages"] or state["messages"][-1] != {"role": "user", "content": message}:
        state["messages"].append({"role": "user", "content": message})

def _trim_messages(state):
    if state.get("waiting_confirmation") or len(state["messages"]) <= MAX_CONTEXT_MESSAGES + 1:
        return
    state["messages"] = [state["messages"][0], *state["messages"][-MAX_CONTEXT_MESSAGES:]]

def _finish(conversation):
    conversation.context.pop("agent_run", None)
    conversation.save(update_fields=["context"])

def _run(conversation, user, state):
    definitions, names = openai_functions(user)
    registry = {item["id"]: item for item in available_tools(user)}
    for _ in range(MAX_TOOL_CALLS + 1):
        preflight(); _debug(conversation, "function_call.request", input={"messages": state["messages"], "tools": definitions})
        answer = _gateway(state["messages"], definitions)
        usage = answer.pop("_gateway_usage", None)
        _debug(conversation, "function_call.response", output={"message": answer, "usage": usage})
        state["messages"].append(answer); calls = answer.get("tool_calls") or []
        if not calls:
            _trim_messages(state); _save(conversation, state)
            return {"text": str(answer.get("content") or "Готово."), "kind": "tool_result", "data": {"steps": state["steps"]}}
        if len(calls) != 1:
            raise TenderAIError("Одновременные действия сейчас не поддерживаются.")
        call = calls[0]; function = call.get("function", {}); identifier = names.get(function.get("name"))
        try:
            arguments = json.loads(function.get("arguments", "{}"))
            if not identifier or not isinstance(arguments, dict): raise AssistantToolError("Некорректный вызов функции")
            if registry[identifier]["requires_confirmation"]:
                validate_tool_call(identifier, user, arguments)
                state["waiting_confirmation"] = {"tool_id": identifier, "arguments": arguments, "tool_call_id": call["id"]}; _save(conversation, state)
                return {"text": str(answer.get("content") or "Подтвердите выполнение действия."), "kind": "confirmation_required", "data": {"tool_id": identifier, "arguments": arguments}}
            result = execute_tool(identifier, user, arguments); state["steps"].append({"tool_id": identifier, "result": result}); content = json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)
        except (AssistantToolError, ValueError, TypeError, json.JSONDecodeError) as exc:
            content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": content}); _save(conversation, state)
    _finish(conversation); return {"text": "Не удалось завершить задачу за допустимое число действий.", "kind": "text", "data": {}}

def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    previous = conversation.context.get("agent_run")
    if isinstance(previous, dict) and not previous.get("waiting_confirmation"):
        state = previous
        _remember_user_message(state, message)
        _trim_messages(state)
    else:
        state = _new_state(conversation, message, attachments)
    return _run(conversation, user, state)

def continue_after_confirmation(conversation, user, tool_id, result):
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict): return None
    waiting = state.pop("waiting_confirmation", {})
    if waiting.get("tool_id") != tool_id: return None
    state["steps"].append({"tool_id": tool_id, "result": result})
    state["messages"].append({"role": "tool", "tool_call_id": waiting["tool_call_id"], "content": json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)})
    _save(conversation, state); return _run(conversation, user, state)
