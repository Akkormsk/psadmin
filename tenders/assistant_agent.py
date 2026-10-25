"""Standard OpenAI Function Calling loop for PSADMIN."""
from __future__ import annotations
import json, os, re
from uuid import uuid4
from urllib.request import Request, urlopen
from django.utils import timezone
from .assistant_tools import (
    AssistantToolError, AssistantReferenceError, available_capability_map, available_areas, available_capabilities, available_resources, available_tools, capability_for_user, capability_map_text, confirmation_arguments,
    execute_tool, openai_functions, validate_tool_call, _reference_model, _reference_values, _plan_references,
)
from .assistant_read import ReadPlanError, reuse_result
from .gateway_budget import preflight
from .services import TenderAIError

MAX_TOOL_CALLS = 8
MAX_PROTOCOL_RECOVERIES = 1
MAX_MEMORY_STRING_CHARS = 240
MAX_MEMORY_ITEMS = 20
MAX_MEMORY_SELECTIONS = 5
MAX_MEMORY_IDS = 50
MAX_MEMORY_ACTIONS = 5
MAX_MEMORY_PREVIEWS = 10
MAX_MEMORY_BYTES = 8 * 1024

_SYSTEM_RULES = (
    "You are PSADMIN's Russian-speaking business assistant. Reply naturally in Russian. "
    "After every tool result, compare it with the user's whole request and the current plan. Continue with the next required step; "
    "update the plan only if the route materially changes. Use actual tool results, and use backend aggregates for money and quantities. "
    "Never invent facts, fields, IDs, values, or calculations. If several business interpretations or records are equally plausible, "
    "first use available data when it can resolve the ambiguity; otherwise clarify the user's intent through assistant.ask. "
    "Ask for business meaning such as a period, amount, person, object or intended operation, never for technical fields, entity IDs, codes or JSON. "
    "The capability map lists existing object references required by actions. If the intended object has no current verified ID in the authorized conversation context, "
    "plan permitted READ steps to resolve it from the user's identifying details before the ACTION. Never invent a placeholder ID, including zero. "
    "Reuse a verified ID only when its object, scope and permissions still match; the backend revalidates it. Creating a new record without an existing-object reference needs no preliminary READ. "
    "Resolve only references needed for the requested operation; omitted optional references do not require lookup. "
    "If READ returns multiple plausible objects, ask the user to choose in business terms; report absence only from an actual adequate READ result. "
    "A rejected object reference is not a search result. Revise the same task's plan with a concrete reason, resolve the reference using READ and continue; do not ask the user for internal IDs. "
    "Bind each ACTION reference in plan.reference_sources to a preceding READ step or saved source_id. Change sources only by replanning. Empty READ: continue searching or use assistant.ask; cancellation requires the user's agreement. Every READ/ACTION task MUST start with assistant.plan, BEFORE even a clarification question; arguments may still be unknown. "
    "Keep the same task and plan when collecting or correcting amounts, dates and other arguments. "
    "Revise the plan only for a changed goal, capability or route, with a specific revision_reason. Preserve completed work. "
    "For ordinary conversation and self-contained elementary arithmetic without business data or actions, use assistant.chat instead of plan/finish. "
    "If the user cancels or switches to an independent task, call assistant.cancel; set new_task=true only for switching. "
    "Do not use replanning to replace an independent task or to pretend cancellation is success. "
    "Compact conversation memory may contain earlier selections and objects. Treat it as verified references only: preserve a selection's saved IDs and scope when the user says ‘из них’ or returns to it; "
    "do not silently apply its filters to a new task. If a reference can name more than one saved object, ask a short ordinary Russian question. "
    "Actions require explicit user confirmation by the interface button; a text reply such as ‘да’ is not confirmation. "
    "When an action has all required values and verified reference sources, call it immediately to prepare the confirmation card; never ask for a separate textual confirmation first. "
    "READ results include raw records for backend continuity and a metadata-derived display view; use the display view's labels and values in user-facing answers, never raw choice codes. "
    "Use assistant.finish only for a final answer after the active plan's required steps have actually completed. "
    "Use assistant.ask for ongoing task dialogue: answer questions and clarify as needed, using only available evidence. Keep the task open. "
    "Never emit a bare text response: chat, ask and finish are different explicit function calls."
)


class AgentProtocolError(AssistantToolError):
    """The model attempted an invalid task lifecycle transition."""

def _debug(conversation, stage, **payload):
    from .assistant_conversations import add_debug_event
    add_debug_event(conversation, stage, event_input=payload.get("input"), event_output=payload.get("output"), error=payload.get("error", ""))


def _request_metrics(messages, tools):
    return {
        "message_chars": sum(len(str(item.get("content", ""))) for item in messages),
        "tool_schema_bytes": len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode()),
    }

def _gateway(messages, tools):
    key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    if not key:
        raise TenderAIError("AI Gateway не настроен.")
    body = {"model": os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "openai/gpt-4.1-mini"), "messages": messages, "tools": tools, "tool_choice": "required", "parallel_tool_calls": False, "max_tokens": 1200}
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

def _system_prompt(user, state):
    prompt = (
        _SYSTEM_RULES
        + f" The current application date is {timezone.localdate().isoformat()}; use this exact date when the user says today, never guess it. "
        + "For a free-text action value, you may fix clear spelling, casing, spacing and punctuation, but never add a fact or change its meaning."
    )
    if state.get("capability_map_pending"):
        purpose = "before the first plan" if not state.get("plan") else "before revising the current plan"
        return (
            prompt
            + " For business requests, inspect the compact capability map below " + purpose + ". "
            + "Then save a concise plan through assistant.plan that names the needed capability IDs before any business tool or clarification. "
            + "After the plan, call its current capability directly; its detailed schema opens automatically. "
            + "Do not claim that a capability is unavailable before checking this map. "
            + "Capability map: " + capability_map_text(available_capability_map(user)) + "."
        )
    context = []
    if state.get("plan"):
        context.append("Current plan: " + str(state["plan"]))
    if state.get("active_area"):
        context.append("Selected area: " + str(state["active_area"]))
    if state.get("active_capability"):
        context.append("Selected capability: " + str(state["active_capability"]))
    return prompt + (" " + ". ".join(context) + "." if context else "")


def _refresh_system_prompt(user, state):
    state["messages"] = [{"role": "system", "content": _system_prompt(user, state)}, *state["messages"][1:]]


def _new_state(conversation, user, message, attachments):
    memory = _load_memory(conversation.context.get("agent_memory"), conversation.context.get("owner_id"))
    state = {"messages": [], "steps": [], "status": "RUNNING", "attachments": attachments or [], "capability_map_pending": True, "memory": memory}
    if memory["recent_selections"]:
        cached = _selection_cache(memory["recent_selections"][-1])
        if cached is not None:
            state["read_cache"] = cached
    state["messages"] = [{"role": "system", "content": _system_prompt(user, state)}]
    memory_context = _memory_context(memory)
    if memory_context:
        state["messages"].append({"role": "system", "content": "Compact conversation memory: " + json.dumps(memory_context, ensure_ascii=False, default=str)})
    state["messages"].append({"role": "user", "content": message})
    return state

def _save(conversation, state):
    conversation.context["agent_run"] = json.loads(json.dumps(state, ensure_ascii=False, default=str))
    conversation.save(update_fields=["context"])

def _remember_user_message(state, message):
    if not state["messages"] or state["messages"][-1] != {"role": "user", "content": message}:
        state["messages"].append({"role": "user", "content": message})


_SOURCE_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def _source_tokens(value):
    return _SOURCE_TOKEN.findall(value.casefold())


def _edit_distance(left, right):
    """Small deterministic edit distance for one-token typo correction."""
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 1:
        return 2
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        current = [index]
        for column, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1, previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _normalised_text_is_grounded(value, user_texts):
    """Accept only a conservative, ordered normalisation of user wording.

    This is not fuzzy semantic matching: every output token must come from a
    user message in the same order.  Numbers and short words must match
    exactly; a single edit is allowed only for longer alphabetic words, which
    covers ordinary typos and grammatical endings without permitting a new
    amount, identifier, counterparty or condition.
    """
    candidate = _source_tokens(value)
    if not candidate:
        return False
    for text in user_texts:
        source = _source_tokens(text)
        position = 0
        for token in candidate:
            matched = False
            for index in range(position, len(source)):
                original = source[index]
                exact = token == original
                allowed_edits = 2 if min(len(token), len(original)) >= 7 else 1
                typo = (
                    token.isalpha() and original.isalpha()
                    and min(len(token), len(original)) >= 4
                    and 0 < _edit_distance(token, original) <= allowed_edits
                )
                if exact or typo:
                    position = index + 1
                    matched = True
                    break
            if not matched:
                break
        else:
            return True
    return False


def _is_free_user_text(declaration):
    if isinstance(declaration, dict):
        return declaration.get("source_from_user") is True
    return isinstance(declaration, str) and declaration.rstrip("?") == "string"


def _validate_action_text_sources(tool, arguments, state):
    """Keep free text grounded in user input while allowing safe normalisation."""
    user_texts = [item.get("content", "") for item in state["messages"] if item.get("role") == "user"]
    for name, declaration in tool["input_schema"].items():
        if not _is_free_user_text(declaration):
            continue
        value = arguments.get(name)
        if value not in (None, "") and (not isinstance(value, str) or not _normalised_text_is_grounded(value, user_texts)):
            raise AssistantToolError("Текст действия должен опираться на слова пользователя; новое существенное значение нужно уточнить")


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


def _record_preview(record):
    if not isinstance(record, dict):
        return {}
    # A preview is only for recognising a backend record again.  IDs and the
    # original query live separately and are never removed by generic nesting.
    keys = ["id", "purchase_number", "order_number", "name", "title", "reason", "operation_date", "amount", "status"]
    return {key: _compact_memory(record[key]) for key in keys if key in record}


def _selection_from_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("entity"), str):
        return None
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    ids = [item.get("id") for item in sources if isinstance(item, dict) and isinstance(item.get("id"), int)]
    records = result.get("records") if isinstance(result.get("records"), list) else []
    previews = [_record_preview(item) for item in records[:MAX_MEMORY_PREVIEWS] if isinstance(item, dict)]
    external_ids = [
        {key: record[key] for key in ("purchase_number", "order_number") if key in record}
        for record in records[:MAX_MEMORY_PREVIEWS] if isinstance(record, dict) and any(key in record for key in ("purchase_number", "order_number"))
    ]
    total = result.get("total_count")
    returned = result.get("returned_count")
    return {
        "source_id": result.get("source_id") or uuid4().hex,
        "entity": result["entity"],
        "record_ids": ids[:MAX_MEMORY_IDS],
        # ``query`` is already a validated, JSON-safe backend summary.  Keep
        # its complete filter list: losing one condition would make a saved
        # selection describe a wider scope than the actual result.
        "query": result.get("query") if isinstance(result.get("query"), dict) else {},
        "aggregates": _compact_memory(result.get("aggregates") if isinstance(result.get("aggregates"), dict) else {}),
        "total_count": total if isinstance(total, int) else None,
        "returned_count": returned if isinstance(returned, int) else None,
        "complete": bool(total == returned == len(ids)) and result.get("source_scope") == "all matching records",
        "source_scope": _compact_memory(result.get("source_scope")),
        "records": previews,
        "external_ids": external_ids,
    }


def _selection_cache(selection):
    """Reconstruct only a complete cached result; incomplete selections read ORM again."""
    if not isinstance(selection, dict) or not selection.get("complete") or selection.get("cache_invalidated"):
        return None
    ids = selection.get("record_ids")
    records = selection.get("records")
    if not isinstance(ids, list) or not isinstance(records, list) or len(ids) != len(records):
        return None
    return {
        "entity": selection.get("entity"), "query": selection.get("query", {}),
        "total_count": selection.get("total_count"), "returned_count": selection.get("returned_count"),
        "aggregates": selection.get("aggregates", {}), "records": records,
        "sources": [{"entity": selection.get("entity"), "id": value} for value in ids],
        "source_scope": "cached complete result",
    }


def _action_result(tool_id, result):
    if not isinstance(result, dict):
        return None
    ids = {key: value for key, value in result.items() if (key == "id" or key.endswith("_id")) and isinstance(value, int)}
    return {"tool_id": tool_id, "object_ids": ids, "result": _compact_memory(result)}


def _load_memory(raw, owner_id):
    """Read both the M7 format and the old one-turn memory without failing."""
    base = {"version": 2, "recent_selections": [], "recent_actions": []}
    if not isinstance(raw, dict):
        return base
    if raw.get("version") == 2:
        for key in ("active_task", "active_focus"):
            if isinstance(raw.get(key), dict):
                base[key] = _compact_memory(raw[key])
        if isinstance(raw.get("recent_selections"), list):
            base["recent_selections"] = [_normalise_selection(item) for item in raw["recent_selections"] if isinstance(item, dict)]
        if isinstance(raw.get("recent_actions"), list):
            base["recent_actions"] = [_compact_memory(item) for item in raw["recent_actions"] if isinstance(item, dict)]
        return base
    # M6 stored only last_read/location/task_plan.  Promote it once so old
    # conversations remain usable but do not keep the obsolete shape forever.
    last_read = raw.get("last_read")
    if isinstance(last_read, dict) and last_read.get("owner_id") in (None, owner_id):
        selection = _selection_from_result(last_read)
        if selection:
            base["recent_selections"] = [selection]
    plan = raw.get("task_plan")
    if isinstance(plan, str) and plan.strip():
        base["active_task"] = {"plan": plan[:MAX_MEMORY_STRING_CHARS]}
    return base


def _normalise_selection(raw):
    """Bound display details without ever shortening a retained ID/filter list."""
    result = dict(raw)
    result["source_id"] = raw.get("source_id") or uuid4().hex
    result["record_ids"] = [value for value in raw.get("record_ids", []) if isinstance(value, int)][:MAX_MEMORY_IDS]
    result["records"] = [_record_preview(item) for item in raw.get("records", [])[:MAX_MEMORY_PREVIEWS] if isinstance(item, dict)]
    result["external_ids"] = [
        _compact_memory(item) for item in raw.get("external_ids", [])[:MAX_MEMORY_PREVIEWS] if isinstance(item, dict)
    ]
    result["query"] = raw.get("query") if isinstance(raw.get("query"), dict) else {}
    result["aggregates"] = _compact_memory(raw.get("aggregates") if isinstance(raw.get("aggregates"), dict) else {})
    return result


def _memory_context(memory):
    if not isinstance(memory, dict):
        return {}
    # Do not send cached record payloads again.  The model gets IDs, a short
    # recognisable preview, query boundaries and the result scope.
    context = {"active_task": memory.get("active_task"), "active_focus": memory.get("active_focus"),
               "recent_selections": memory.get("recent_selections", []), "recent_actions": memory.get("recent_actions", [])}
    return {key: value for key, value in context.items() if value not in (None, [], {})}


def _bound_memory(memory):
    memory["version"] = 2
    memory["recent_selections"] = list(memory.get("recent_selections", []))[-MAX_MEMORY_SELECTIONS:]
    memory["recent_actions"] = list(memory.get("recent_actions", []))[-MAX_MEMORY_ACTIONS:]
    # Measure the representation Django will store, not an artificially
    # minified variant, so the documented 8 KiB bound remains true on disk.
    encoded = json.dumps(memory, ensure_ascii=False, default=str).encode()
    # Prefer removing the oldest optional selection/action over cutting IDs or
    # filter objects mid-structure.  This keeps memory truthful under pressure.
    while len(encoded) > MAX_MEMORY_BYTES and (memory["recent_selections"] or memory["recent_actions"]):
        if len(memory["recent_selections"]) >= len(memory["recent_actions"]):
            memory["recent_selections"].pop(0)
        else:
            memory["recent_actions"].pop(0)
        encoded = json.dumps(memory, ensure_ascii=False, default=str).encode()
    return memory


def _invalidate_read_selections(memory):
    # Actions can change related models too. Keep selected IDs/scope for
    # references, but no earlier selection may answer a fresh READ.
    for selection in memory.get("recent_selections", []):
        selection["cache_invalidated"] = True


def _merge_memory(conversation, state):
    memory = _load_memory(state.get("memory", conversation.context.get("agent_memory")), conversation.context.get("owner_id"))
    for step in state.get("steps", []):
        tool_id, result = step.get("tool_id"), step.get("result")
        if isinstance(tool_id, str) and tool_id.startswith("read:"):
            selection = _selection_from_result(result)
            if selection:
                memory["recent_selections"].append(selection)
                # A singleton read is an unambiguous active business object.
                if len(selection["record_ids"]) == 1:
                    record = selection["records"][0] if selection["records"] else {}
                    memory["active_focus"] = {"entity": selection["entity"], "id": selection["record_ids"][0], "label": _compact_memory(record), "external_ids": selection["external_ids"][:1]}
        elif isinstance(tool_id, str) and tool_id not in {"assistant.plan", "assistant.select_area", "assistant.select_capability"}:
            if step.get("status", "completed") == "completed" and not step.get("error"):
                _invalidate_read_selections(memory)
            action = _action_result(tool_id, result)
            if action:
                memory["recent_actions"].append(action)
    if isinstance(state.get("plan"), dict) and isinstance(state["plan"].get("goal"), str):
        memory["active_task"] = {"plan": state["plan"]["goal"][:MAX_MEMORY_STRING_CHARS], "status": state.get("outcome", "completed")}
    elif isinstance(state.get("plan"), str) and state["plan"].strip():
        memory["active_task"] = {"plan": state["plan"][:MAX_MEMORY_STRING_CHARS]}
    return _bound_memory(memory)


def _finish_turn(conversation, state):
    conversation.context["agent_memory"] = _merge_memory(conversation, state)
    conversation.context.pop("agent_run", None)
    conversation.save(update_fields=["context"])

def _finish(conversation):
    conversation.context.pop("agent_run", None)
    conversation.save(update_fields=["context"])


def _capability_ids(user):
    return {
        capability["id"]
        for area in available_areas(user)
        for capability in available_capabilities(user, area["id"])
    }


def _reference_results(state):
    """One namespace for retained READ results, across plans and turns."""
    results = {item["source_id"]: item for item in state.get("memory", {}).get("recent_selections", [])
               if item.get("source_id")}
    for execution in state.get("steps", []):
        result = execution.get("result")
        if execution.get("tool_id", "").startswith("read:") and execution.get("status", "completed") == "completed" and isinstance(result, dict) and result.get("source_id"):
            results[result["source_id"]] = result
    return results


def _bind_reference_sources(steps, declarations, user, state, previous):
    previous_steps = {item["id"]: item for item in (previous or {}).get("steps", [])}
    results = _reference_results(state)
    preceding = {}
    for step, declaration in zip(steps, declarations):
        if step["capability_id"].startswith("read:"):
            old = previous_steps.get(step["id"], {})
            # Keep identity only for an unchanged search step. A revised search
            # cannot inherit the result of a different query/route.
            same = all(old.get(key) == step.get(key) for key in ("id", "capability_id", "objective", "expected_result"))
            step["source_id"] = old.get("source_id") if same and old.get("source_id") else uuid4().hex
            preceding[step["id"]] = step["source_id"]
            continue
        references = {item["parameter"]: item for item in _plan_references(user, step["capability_id"])}
        raw_sources = declaration.get("reference_sources") or []
        if not isinstance(raw_sources, list) or any(not isinstance(item, dict) or set(item) != {"parameter", "read_id"} or not isinstance(item["parameter"], str) for item in raw_sources):
            raise AssistantReferenceError("reference_sources должен быть списком parameter/read_id")
        sources = {item["parameter"]: item["read_id"] for item in raw_sources}
        if len(sources) != len(raw_sources) or any(key not in references for key in sources):
            raise AssistantReferenceError("reference_sources должен содержать только параметры reference этого ACTION")
        if any(item["required"] and name not in sources for name, item in references.items()):
            raise AssistantReferenceError("Укажите в плане источник READ для каждого обязательного reference ACTION")
        bound = {}
        for name, source in sources.items():
            if not isinstance(source, str) or not source:
                raise AssistantReferenceError("Источник reference должен быть идентификатором READ")
            source_id = preceding.get(source, source)
            if source_id not in preceding.values() and source_id not in results:
                raise AssistantReferenceError("Источник reference отсутствует: укажите предыдущий шаг READ или сохранённый source_id")
            result = results.get(source_id)
            models = references[name]["model"]
            models = {models} if isinstance(models, str) else {item["model"] for item in models}
            if result and result.get("entity") not in models:
                raise AssistantReferenceError("Модель источника READ не соответствует reference ACTION")
            if not result:
                source_step = next(item for item in steps if item.get("source_id") == source_id)
                resource = next((r for r in available_resources(user) if "read:" + r["id"] == source_step["capability_id"]), None)
                if resource is None or resource["entity"] not in models:
                    raise AssistantReferenceError("Модель шага READ не соответствует reference ACTION")
            bound[name] = source_id
        if references:
            step["reference_sources"] = [{"parameter": name, "read_id": source} for name, source in bound.items()]
    # An unresolved action stays pending until the user agrees to cancel.
    # Replanning may replace its search, not silently erase the obligation.
    for old in previous_steps.values():
        if old["id"] in _completed_plan_steps(state):
            continue
        if not any(item["id"] == old["id"] and item["capability_id"] == old["capability_id"] for item in steps):
            sources = [item["read_id"] for item in old.get("reference_sources", [])]
            if any(source in results and results[source].get("total_count") == 0 for source in sources):
                raise AssistantReferenceError("Пустой READ не отменяет ACTION: уточните задачу через assistant.ask; закрытие без результата — только с согласия пользователя через assistant.cancel")


def _validate_reference_sources(tool, arguments, state):
    step = next((item for item in state.get("plan", {}).get("steps", [])
                 if item["id"] == state.get("active_plan_step_id")), {})
    results = _reference_results(state)
    for name, declaration in tool["input_schema"].items():
        if not isinstance(declaration, dict) or not isinstance(declaration.get("reference"), dict) or arguments.get(name) is None:
            continue
        source_id = next((item["read_id"] for item in step.get("reference_sources", []) if item["parameter"] == name), None)
        result = results.get(source_id)
        if result is None:
            raise AssistantReferenceError("Источник reference не указан в плане или READ ещё не выполнен; исправьте план и выполните READ")
        model = _reference_model(declaration["reference"], arguments)
        if result.get("entity", "").lower() != model._meta.label.lower():
            raise AssistantReferenceError("Источник READ относится к другой модели")
        ids = result.get("record_ids") if "record_ids" in result else [item.get("id") for item in result.get("sources", []) if isinstance(item, dict)]
        wanted = _reference_values(model, declaration, arguments[name])
        if not ids or any(str(value) not in {str(item) for item in ids} for value in wanted):
            raise AssistantReferenceError("READ указанного источника не содержит выбранный объект. Продолжите поиск или уточните запрос через assistant.ask; не используйте другой источник без пересмотра плана")
        if len(ids) > 1:
            # A fresh ambiguous result needs a user reply before selection.
            # The semantic meaning of that reply remains the model's decision.
            messages = state.get("messages", [])
            last_user = max((i for i, message in enumerate(messages) if message.get("role") == "user"), default=-1)
            read_message = max((i for i, message in enumerate(messages) if message.get("role") == "tool" and source_id in str(message.get("content", ""))), default=-1)
            if last_user <= read_message:
                raise AssistantReferenceError("READ содержит несколько объектов: уточните выбор через assistant.ask")


def _normalise_plan(value, user, previous=None, state=None):
    """Validate the compact plan against the same registry shown to the model."""
    if not isinstance(value, dict):
        raise AssistantToolError("План должен содержать цель и шаги")
    goal, steps = value.get("goal"), value.get("steps")
    if not isinstance(goal, str) or not goal.strip() or not isinstance(steps, list) or not steps:
        raise AssistantToolError("План должен содержать непустую цель и хотя бы один шаг")
    allowed = _capability_ids(user)
    normalised, seen = [], set()
    for step in steps:
        if not isinstance(step, dict):
            raise AssistantToolError("Шаг плана имеет неверный формат")
        identifier, capability_id, objective = step.get("id"), step.get("capability_id"), step.get("objective")
        expected_result = step.get("expected_result", objective)
        if not all(isinstance(item, str) and item.strip() for item in (identifier, capability_id, objective, expected_result)):
            raise AssistantToolError("У каждого шага плана нужны идентификатор, возможность, цель и ожидаемый результат")
        if identifier in seen or capability_id not in allowed:
            raise AssistantToolError("План содержит недоступную или повторяющуюся возможность")
        seen.add(identifier)
        normalised.append({"id": identifier, "capability_id": capability_id, "objective": objective, "expected_result": expected_result})
    _bind_reference_sources(normalised, steps, user, state or {}, previous)
    reason = value.get("revision_reason")
    if previous and (not isinstance(reason, str) or not reason.strip()):
        raise AgentProtocolError("Пересмотр плана требует конкретной причины изменения цели или маршрута; уточнение аргументов продолжает прежний план")
    if previous and not previous.get("legacy") and goal.strip() == previous.get("goal") and normalised == previous.get("steps"):
        raise AgentProtocolError("План не изменился; продолжите его без повторного планирования")
    return {"version": int((previous or {}).get("version", 0)) + 1, "goal": goal.strip(), "steps": normalised,
            "revision_reason": reason.strip() if isinstance(reason, str) else None}


def _completed_plan_steps(state):
    plan = state.get("plan")
    if not isinstance(plan, dict) or plan.get("legacy"):
        return set()
    return {
        step["id"] for step in plan.get("steps", [])
        for index, execution in enumerate(state.get("steps", []))
        if execution.get("plan_step_id") == step["id"] and execution.get("tool_id") == step["capability_id"]
        and (execution.get("plan_version") == plan.get("version") or index in plan.get("carried_execution", []))
        and execution.get("status") == "completed"
        and _has_execution_evidence(execution.get("tool_id", ""), execution.get("result"))
    }


def _active_plan_step(state, capability_id):
    plan = state.get("plan")
    if not isinstance(plan, dict):
        return None
    completed = _completed_plan_steps(state)
    return next((item["id"] for item in plan.get("steps", [])
                 if item["capability_id"] == capability_id and item["id"] not in completed), None)


def _activate_next_plan_step(user, state):
    plan = state.get("plan")
    if not isinstance(plan, dict):
        return
    completed = _completed_plan_steps(state)
    next_step = next((item for item in plan.get("steps", []) if item["id"] not in completed), None)
    if next_step is None:
        state.pop("active_area", None)
        state.pop("active_capability", None)
        state.pop("active_plan_step_id", None)
        return
    area, capability = capability_for_user(user, next_step["capability_id"])
    if area is None or capability is None:
        raise AssistantToolError("План содержит недоступную возможность")
    state["active_area"] = area["id"]
    state["active_capability"] = capability["id"]
    state["active_plan_step_id"] = next_step["id"]


def _has_execution_evidence(capability_id, result):
    if not isinstance(result, dict):
        return False
    if capability_id.startswith("read:"):
        return isinstance(result.get("entity"), str) and all(
            key in result for key in ("records", "aggregates", "total_count")
        )
    return bool(result)


def _record_execution(state, identifier, result, *, status="completed", error=None):
    if not identifier.startswith("read:") and status == "completed" and error is None:
        state.pop("read_cache", None)
        _invalidate_read_selections(state.get("memory", {}))
    safe_result = json.loads(json.dumps(result, ensure_ascii=False, default=str)) if error is None else None
    state["steps"].append({
        "plan_version": state["plan"].get("version") if isinstance(state.get("plan"), dict) else None,
        "plan_step_id": state.pop("active_plan_step_id", None),
        "tool_id": identifier,
        "arguments": state.pop("active_arguments", None),
        "status": status,
        "result": safe_result,
        "error": error,
    })


def _missing_plan_steps(state):
    plan = state.get("plan")
    if not isinstance(plan, dict):
        return []
    if plan.get("legacy"):
        return ["обновлённый план"]
    completed = _completed_plan_steps(state)
    return [item["id"] for item in plan.get("steps", []) if item["id"] not in completed]


def _recover_protocol(conversation, user, state, error, recoveries):
    _debug(conversation, "agent.protocol_error", output={"task_id": state.get("task_id"), "status": state["status"]}, error=error)
    if not state.get("plan"):
        state["capability_map_pending"] = True
        _refresh_system_prompt(user, state)
    _save(conversation, state)
    if recoveries >= MAX_PROTOCOL_RECOVERIES:
        raise TenderAIError("Ассистент нарушил порядок выполнения. Исходная задача и её результаты сохранены; выполнение остановлено.")
    return recoveries + 1


def _run(conversation, user, state):
    state["status"] = "RUNNING"
    state.pop("waiting_user", None)
    if state.get("plan"):
        state.setdefault("task_id", uuid4().hex)
    _save(conversation, state)
    registry = {item["id"]: item for item in available_tools(user)}
    recoveries = 0
    for _ in range(MAX_TOOL_CALLS + 1):
        definitions, names = openai_functions(
            user, area_id=state.get("active_area"), capability_id=state.get("active_capability"),
            planned=bool(state.get("plan")),
        )
        preflight(); _debug(conversation, "function_call.request", input={
            "messages": state["messages"], "tools": definitions,
            "metrics": _request_metrics(state["messages"], definitions),
        })
        map_sent = bool(state.get("capability_map_pending"))
        answer = _gateway(state["messages"], definitions)
        if map_sent:
            state["capability_map_pending"] = False
            _refresh_system_prompt(user, state)
        usage = answer.pop("_gateway_usage", None)
        _debug(conversation, "function_call.response", output={"message": answer, "usage": usage})
        state["messages"].append(answer); calls = answer.get("tool_calls") or []
        if not calls:
            error = "Bare text is not a lifecycle transition. Use assistant.plan before any business clarification, assistant.chat only for ordinary conversation; inside a plan use assistant.ask for a question or assistant.finish after completing its steps."
            state["messages"].append({"role": "system", "content": error})
            recoveries = _recover_protocol(conversation, user, state, error, recoveries)
            continue
        if len(calls) != 1:
            _debug(conversation, "agent.protocol_error", error="Parallel tool calls are not an allowed lifecycle transition")
            raise TenderAIError("Одновременные действия сейчас не поддерживаются.")
        call = calls[0]; function = call.get("function", {}); identifier = names.get(function.get("name"))
        try:
            arguments = json.loads(function.get("arguments", "{}"))
            if not identifier or not isinstance(arguments, dict):
                raise AgentProtocolError("Некорректный вызов функции; бизнес-инструменты и вопросы доступны только после действующего плана")
            if identifier.startswith("read:") or identifier in registry:
                step_id = _active_plan_step(state, identifier)
                if step_id is None or state.get("active_capability") != identifier:
                    raise AgentProtocolError("Инструмент не соответствует текущему незавершённому шагу действующего плана")
                state["active_plan_step_id"] = step_id
            if identifier in {"assistant.chat", "assistant.ask", "assistant.cancel"}:
                key = "question" if identifier == "assistant.ask" else "answer"
                required_keys = {"reason", "answer", "new_task"} if identifier == "assistant.cancel" else {key}
                if set(arguments) != required_keys:
                    raise AgentProtocolError("Некорректные параметры служебного перехода")
                text = arguments.get(key)
                if not isinstance(text, str) or not text.strip():
                    raise AgentProtocolError("Для перехода требуется непустой текст")
                has_plan = isinstance(state.get("plan"), dict) and not state["plan"].get("legacy")
                if (identifier == "assistant.chat" and state.get("plan")) or (identifier != "assistant.chat" and not has_plan):
                    raise AgentProtocolError("Вопрос и отмена требуют действующего плана; свободный разговор допустим только вне задачи")
                if identifier == "assistant.cancel" and (not isinstance(arguments.get("reason"), str)
                        or not arguments["reason"].strip() or not isinstance(arguments.get("new_task"), bool)):
                    raise AgentProtocolError("Отмена требует причины и явного признака новой задачи")
                state["messages"].append({"role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps({"ok": True, "transition": identifier}, ensure_ascii=False)})
                _debug(conversation, "function_call.tool", output={"tool_id": identifier, "ok": True})
                if identifier == "assistant.ask":
                    state["status"] = "WAITING_USER"
                    state["waiting_user"] = True
                    _save(conversation, state)
                elif identifier == "assistant.chat":
                    _finish(conversation)
                else:
                    state["outcome"] = "cancelled"
                    _debug(conversation, "agent.task_cancelled", output={"task_id": state["task_id"], "reason": arguments["reason"]})
                    _finish_turn(conversation, state)
                    if arguments["new_task"]:
                        message = next(item["content"] for item in reversed(state["messages"]) if item["role"] == "user")
                        state = _new_state(conversation, user, message, state.get("attachments"))
                        _save(conversation, state)
                        continue
                return {"text": text, "kind": "tool_result", "data": {"steps": state["steps"]}}
            if identifier == "assistant.finish":
                text = arguments.get("answer")
                if set(arguments) != {"answer"} or not isinstance(text, str) or not text.strip():
                    raise AgentProtocolError("Финальный ответ должен быть непустым текстом без дополнительных параметров")
                if not isinstance(state.get("plan"), dict):
                    raise AgentProtocolError("Нельзя завершить бизнес-задачу без действующего плана")
                missing = _missing_plan_steps(state)
                if missing or state.get("waiting_confirmation"):
                    raise AgentProtocolError("Нельзя завершить задачу: не подтверждены шаги " + ", ".join(missing or ["действие"]) + ". Если READ не дал нужного объекта, продолжите поиск или диалог через assistant.ask; при смене маршрута явно пересмотрите план. Не подставляйте другой объект. Закрытие без результата требует согласия пользователя и assistant.cancel.")
                _debug(conversation, "function_call.tool", output={"tool_id": identifier, "ok": True})
                _debug(conversation, "agent.task_finished", output={"task_id": state["task_id"]})
                _finish_turn(conversation, state)
                return {"text": text, "kind": "tool_result", "data": {"steps": state["steps"]}}
            if identifier == "assistant.plan":
                previous_plan = state.get("plan") if isinstance(state.get("plan"), dict) else None
                plan = _normalise_plan(arguments.get("plan"), user, previous_plan, state)
                if previous_plan:
                    unchanged = [item["id"] for item in plan["steps"] if item in previous_plan.get("steps", [])]
                    completed = _completed_plan_steps(state)
                    plan["carried_execution"] = [
                        index for index, item in enumerate(state["steps"])
                        if item.get("plan_step_id") in unchanged and item.get("plan_step_id") in completed
                        and (item.get("plan_version") == previous_plan.get("version")
                             or index in previous_plan.get("carried_execution", []))
                    ]
                state["plan"] = plan
                state.setdefault("task_id", uuid4().hex)
                state.pop("waiting_user", None)
                _activate_next_plan_step(user, state)
                if not map_sent:
                    state["capability_map_pending"] = True
                _refresh_system_prompt(user, state)
                result = {"plan_saved": True}
                _debug(conversation, "agent.plan", output={"task_id": state["task_id"], "plan": state["plan"]})
            elif identifier == "assistant.select_area":
                area_id = arguments.get("area")
                area = next((item for item in available_areas(user) if item["id"] == area_id), None)
                if area is None:
                    raise AssistantToolError("Недоступный раздел PSADMIN")
                state["active_area"] = area_id
                state.pop("active_capability", None)
                _refresh_system_prompt(user, state)
                result = {"area": area}
            elif identifier == "assistant.select_capability":
                capability_id = arguments.get("capability")
                capability = next((item for item in available_capabilities(user, state.get("active_area")) if item["id"] == capability_id), None)
                if capability is None:
                    raise AssistantToolError("Недоступная возможность раздела")
                step_id = _active_plan_step(state, capability_id)
                if isinstance(state.get("plan"), dict) and state["plan"].get("steps") and step_id is None:
                    raise AssistantToolError("Выбранная возможность не относится к незавершённому шагу плана")
                state["active_capability"] = capability_id
                state["active_plan_step_id"] = step_id
                _refresh_system_prompt(user, state)
                result = {key: capability[key] for key in ("id", "kind", "name", "description", "requires_confirmation")}
            elif identifier.startswith("read:"):
                resource_id = identifier.split(":", 1)[1]
                resource = next((item for item in available_resources(user, state.get("active_area")) if item["id"] == resource_id), None)
                if resource is None or state.get("active_capability") != identifier:
                    raise AssistantToolError("Сначала выберите нужную возможность раздела")
                plan = {"entity": resource["entity"], **arguments.get("plan", {})}
                cached = state.get("read_cache")
                result = reuse_result(user, plan, cached) if cached else None
                result = result if result is not None else execute_tool("read", user, {"plan": plan})
                read_step = next(item for item in state["plan"]["steps"] if item["id"] == state["active_plan_step_id"])
                result = {**result, "source_id": read_step["source_id"]}
                state["last_read_result"] = result
                state["read_cache"] = {**result, "owner_id": conversation.context.get("owner_id")}
            elif registry[identifier]["requires_confirmation"]:
                _validate_reference_sources(registry[identifier], arguments, state)
                validate_tool_call(identifier, user, arguments)
                _validate_action_text_sources(registry[identifier], arguments, state)
                state["waiting_confirmation"] = {
                    "tool_id": identifier, "arguments": arguments, "tool_call_id": call["id"],
                    "plan_step_id": state.get("active_plan_step_id"),
                    "plan_version": state["plan"].get("version") if isinstance(state.get("plan"), dict) else None,
                }
                state["status"] = "WAITING_CONFIRMATION"
                _save(conversation, state)
                return {
                    "text": str(answer.get("content") or "Подтвердите выполнение действия."),
                    "kind": "confirmation_required",
                    "data": {
                        "tool_id": identifier,
                        "arguments": arguments,
                        "action": {
                            "name": registry[identifier]["name"],
                            "description": registry[identifier]["description"],
                            "arguments": confirmation_arguments(registry[identifier], arguments),
                        },
                    },
                }
            else:
                _validate_reference_sources(registry[identifier], arguments, state)
                result = execute_tool(identifier, user, arguments)
            if identifier.startswith("read:") or identifier in registry:
                state["active_arguments"] = arguments
                _record_execution(state, identifier, result)
                _activate_next_plan_step(user, state)
            else:
                state["steps"].append({"tool_id": identifier, "result": result, "status": "completed"})
            content = json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)
            _debug(conversation, "function_call.tool", output={"tool_id": identifier, "ok": True})
        except (AssistantToolError, ReadPlanError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if identifier == "assistant.plan" or isinstance(exc, AssistantReferenceError):
                state["capability_map_pending"] = True
                _refresh_system_prompt(user, state)
            content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            _debug(conversation, "function_call.tool", output={"tool_id": identifier or function.get("name"), "ok": False, "error": str(exc)})
            if isinstance(exc, AgentProtocolError):
                state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": content})
                recoveries = _recover_protocol(conversation, user, state, str(exc), recoveries)
                continue
        state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": content}); _save(conversation, state)
    _save(conversation, state)
    _debug(conversation, "agent.protocol_error", error="Agent execution limit reached without finish, ask or confirmation")
    raise TenderAIError("Достигнут лимит шагов. Незавершённая задача и её результаты сохранены.")

def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    previous = conversation.context.get("agent_run")
    if isinstance(previous, dict):
        state = previous
        waiting = state.get("waiting_confirmation")
        if waiting:
            state["messages"].append({"role": "tool", "tool_call_id": waiting["tool_call_id"],
                "content": json.dumps({"ok": False, "cancelled": True,
                    "error": "Пользователь прислал текст вместо подтверждения; прежняя карточка недействительна. Учтите уточнение и подготовьте новую карточку."}, ensure_ascii=False)})
            state.pop("waiting_confirmation")
            # The text is never permission to execute. The next preview must
            # validate the updated arguments and obtain its own button click.
            state.pop("active_arguments", None)
        if isinstance(state.get("plan"), str) and state["plan"].strip():
            state["plan"] = {
                "version": 0, "goal": state["plan"].strip(), "steps": [], "legacy": True,
            }
        state.pop("waiting_user", None)
        _remember_user_message(state, message)
    else:
        state = _new_state(conversation, user, message, attachments)
    return _run(conversation, user, state)

def continue_after_confirmation(conversation, user, tool_id, result):
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict): return None
    waiting = state.get("waiting_confirmation", {})
    if waiting.get("tool_id") != tool_id: return None
    state.pop("waiting_confirmation")
    state["active_plan_step_id"] = waiting.get("plan_step_id")
    state["active_arguments"] = waiting.get("arguments")
    _record_execution(state, tool_id, result)
    _activate_next_plan_step(user, state)
    state["messages"].append({"role": "tool", "tool_call_id": waiting["tool_call_id"], "content": json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)})
    _save(conversation, state); return _run(conversation, user, state)


def continue_after_cancellation(conversation, user, tool_id):
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict):
        return None
    waiting = state.get("waiting_confirmation", {})
    if waiting.get("tool_id") != tool_id:
        return None
    state.pop("waiting_confirmation")
    state["active_plan_step_id"] = waiting.get("plan_step_id")
    state["active_arguments"] = waiting.get("arguments")
    _record_execution(state, tool_id, None, status="cancelled", error="Действие отменено пользователем")
    _activate_next_plan_step(user, state)
    state["messages"].append({
        "role": "tool", "tool_call_id": waiting["tool_call_id"],
        "content": json.dumps({"ok": False, "cancelled": True, "error": "Действие отменено пользователем"}, ensure_ascii=False),
    })
    _save(conversation, state)
    return _run(conversation, user, state)
