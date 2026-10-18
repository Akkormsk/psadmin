"""Standard OpenAI Function Calling loop for PSADMIN."""
from __future__ import annotations
import json, os
from urllib.request import Request, urlopen
from .assistant_tools import AssistantToolError, available_tools, execute_tool, openai_functions, validate_tool_call
from .assistant_read import present_result, reuse_result
from .gateway_budget import preflight
from .services import TenderAIError

MAX_TOOL_CALLS = 8
MAX_HISTORY_TOKENS = 1_200
MAX_MEMORY_STRING_CHARS = 240
MAX_MEMORY_ITEMS = 20

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
    items = [{"role": (x.payload or {}).get("role", "user"), "content": x.raw_text} for x in reversed(events)]
    selected, size = [], 0
    for item in reversed(items):
        item_size = max(1, len(str(item.get("content", ""))) // 4)
        if selected and size + item_size > MAX_HISTORY_TOKENS:
            break
        selected.append(item)
        size += item_size
    return list(reversed(selected))

def _new_state(conversation, message, attachments):
    history = _history(conversation)
    if not history or history[-1] != {"role": "user", "content": message}:
        history.append({"role": "user", "content": message})
    messages = [{"role": "system", "content": "You are PSADMIN's Russian-speaking business assistant. Reply naturally in Russian. Use functions for PSADMIN data or operations; never invent facts, fields or IDs. Call schema.describe before reading an entity and action.describe before preparing an action. Use compact conversation state only when the user's message continues that task; a new task supersedes old filters. Ask only necessary business clarifications in ordinary Russian. Do not expose technical parameters. Actions require explicit user confirmation."}]
    memory = conversation.context.get("agent_memory")
    if isinstance(memory, dict):
        messages.append({"role": "system", "content": "Compact conversation state: " + json.dumps(memory, ensure_ascii=False, default=str)})
    state = {"messages": [*messages, *history], "steps": [], "attachments": attachments or []}
    if isinstance(memory, dict):
        read = memory.get("last_read") if isinstance(memory.get("last_read"), dict) else {}
        schema = memory.get("last_schema") if isinstance(memory.get("last_schema"), dict) else {}
        state["read_entity"] = read.get("entity") or schema.get("entity")
        if read.get("owner_id") == conversation.context.get("owner_id") and read.get("records"):
            state["read_cache"] = read
    return state

def _save(conversation, state):
    conversation.context["agent_run"] = state
    conversation.save(update_fields=["context"])

def _remember_user_message(state, message):
    if not state["messages"] or state["messages"][-1] != {"role": "user", "content": message}:
        state["messages"].append({"role": "user", "content": message})


def _validate_action_text_sources(tool, arguments, state):
    """A free text write value must be copied from a user message, not invented."""
    user_texts = [item.get("content", "") for item in state["messages"] if item.get("role") == "user"]
    for name, declaration in tool["input_schema"].items():
        if not isinstance(declaration, str) or declaration.rstrip("?") != "string":
            continue
        value = arguments.get(name)
        if value not in (None, "") and not any(value in text for text in user_texts):
            raise AssistantToolError("Текст для действия нужно получить из сообщения пользователя")


def _compact_memory(value, depth=0):
    if isinstance(value, str):
        return value[:MAX_MEMORY_STRING_CHARS]
    if isinstance(value, list):
        return [_compact_memory(item, depth + 1) for item in value[:MAX_MEMORY_ITEMS]]
    if isinstance(value, dict):
        if depth >= 3:
            return {}
        return {
            str(key)[:80]: _compact_memory(item, depth + 1)
            for key, item in list(value.items())[:MAX_MEMORY_ITEMS]
        }
    return value


def _remember_read_state(state):
    for step in reversed(state["steps"]):
        if step["tool_id"] != "read" or not isinstance(step["result"], dict):
            continue
        result = step["result"]
        return {
            "entity": result.get("entity"), "query": _compact_memory(result.get("query", {})),
            "aggregates": _compact_memory(result.get("aggregates", {})), "total_count": result.get("total_count"),
            "returned_count": result.get("returned_count"), "sources": (result.get("sources") or [])[:MAX_MEMORY_ITEMS],
            "source_scope": result.get("source_scope"), "records": _compact_memory(result.get("records") or []),
            "owner_id": (state.get("read_cache") or {}).get("owner_id"),
        }
    return None


def _remember_schema_state(state):
    for step in reversed(state["steps"]):
        if step["tool_id"] != "schema.describe" or not isinstance(step["result"], dict):
            continue
        return {"entity": step["result"].get("entity")}
    return None


def _finish_turn(conversation, state):
    memory = {}
    last_read = _remember_read_state(state)
    last_schema = _remember_schema_state(state)
    if last_read is not None:
        memory["last_read"] = last_read
    if last_schema is not None:
        memory["last_schema"] = last_schema
    if memory:
        conversation.context["agent_memory"] = memory
    else:
        conversation.context.pop("agent_memory", None)
    conversation.context.pop("agent_run", None)
    conversation.save(update_fields=["context"])

def _finish(conversation):
    conversation.context.pop("agent_run", None)
    conversation.save(update_fields=["context"])

def _run(conversation, user, state):
    registry = {item["id"]: item for item in available_tools(user)}
    for _ in range(MAX_TOOL_CALLS + 1):
        definitions, names = openai_functions(user, read_entity=state.get("read_entity"), action_id=state.get("action_id"))
        preflight(); _debug(conversation, "function_call.request", input={"messages": state["messages"], "tools": definitions})
        answer = _gateway(state["messages"], definitions)
        usage = answer.pop("_gateway_usage", None)
        _debug(conversation, "function_call.response", output={"message": answer, "usage": usage})
        state["messages"].append(answer); calls = answer.get("tool_calls") or []
        if not calls:
            _finish_turn(conversation, state)
            factual = state.get("last_read_result")
            text = present_result(factual) if factual is not None else str(answer.get("content") or "Готово.")
            return {"text": text, "kind": "tool_result", "data": {"steps": state["steps"]}}
        if len(calls) != 1:
            raise TenderAIError("Одновременные действия сейчас не поддерживаются.")
        call = calls[0]; function = call.get("function", {}); identifier = names.get(function.get("name"))
        try:
            arguments = json.loads(function.get("arguments", "{}"))
            if not identifier or not isinstance(arguments, dict): raise AssistantToolError("Некорректный вызов функции")
            if registry[identifier]["requires_confirmation"]:
                validate_tool_call(identifier, user, arguments)
                _validate_action_text_sources(registry[identifier], arguments, state)
                state["waiting_confirmation"] = {"tool_id": identifier, "arguments": arguments, "tool_call_id": call["id"]}; _save(conversation, state)
                return {"text": str(answer.get("content") or "Подтвердите выполнение действия."), "kind": "confirmation_required", "data": {"tool_id": identifier, "arguments": arguments}}
            cached = state.get("read_cache") if identifier == "read" else None
            result = reuse_result(user, arguments["plan"], cached) if cached else None
            result = result if result is not None else execute_tool(identifier, user, arguments)
            state["steps"].append({"tool_id": identifier, "result": result})
            if identifier == "schema.describe":
                state["read_entity"] = result.get("entity")
            elif identifier == "action.describe":
                state["action_id"] = result.get("id")
            elif identifier == "read":
                state["last_read_result"] = result
                state["read_cache"] = {**result, "owner_id": conversation.context.get("owner_id")}
            content = json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)
            _debug(conversation, "function_call.tool", output={"tool_id": identifier, "ok": True})
        except (AssistantToolError, ValueError, TypeError, json.JSONDecodeError) as exc:
            content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            _debug(conversation, "function_call.tool", output={"tool_id": identifier or function.get("name"), "ok": False, "error": str(exc)})
        state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": content}); _save(conversation, state)
    _finish(conversation); return {"text": "Не удалось завершить задачу за допустимое число действий.", "kind": "text", "data": {}}

def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    previous = conversation.context.get("agent_run")
    if isinstance(previous, dict) and not previous.get("waiting_confirmation"):
        state = previous
        _remember_user_message(state, message)
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
