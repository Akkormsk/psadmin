"""Validated next-step execution loop for the global PSADMIN assistant."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import logging
import os
import re

from django.conf import settings
from django.utils import timezone

from .assistant_tools import (
    AssistantToolError,
    available_tools,
    capability_tree,
    execute_tool,
    required_input_names,
)
from .gateway_budget import preflight, report_line, spend_rub
from .services import TenderAIError, _ai_gateway_json


logger = logging.getLogger(__name__)
MAX_AGENT_DECISIONS = 12
MAX_TOOL_CALLS = 8
MAX_REPAIR_ATTEMPTS = 2
_COMPUTE_OPERATIONS = {"count", "sum", "avg", "min", "max", "subtract", "divide", "percent_change"}
_REFERENCE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\]|\[\d+\]|\.[A-Za-z_][A-Za-z0-9_]*(?:\[\]|\[\d+\])?)*$")


def _history(conversation):
    events = list(conversation.feedback_events.filter(scope="assistant_conversation").order_by("-created_at", "-pk")[:20])
    return [
        {"role": (event.payload or {}).get("role", "user"), "text": event.raw_text}
        for event in reversed(events)
    ]


def _request(prompt, image_data_urls=None):
    model = os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "openai/gpt-4.1-mini")
    result, usage = _ai_gateway_json(
        prompt, model=model, max_tokens=1200, timeout=45, network_attempts=1,
        image_data_urls=image_data_urls,
    )
    cost = spend_rub(usage, model)
    logger.info("assistant gateway model=%s; %s%s", model, report_line(usage, {model: usage}), f"; call ~{cost:.4f} ₽" if cost is not None else "")
    return result


def _tool_catalogue(tools, *, identifiers=None):
    """Give the model only the contract it needs to choose a next action."""
    if identifiers is not None:
        tools = [tool for tool in tools if tool["id"] in identifiers]
    return [
        {
            key: tool[key]
            for key in ("id", "name", "description", "input_schema", "read_only", "requires_confirmation", "available")
        }
        for tool in tools
    ]


def _opened_contracts(state):
    """Return only contracts actually disclosed through the capability graph."""
    nodes = {}
    for item in state["results"]:
        node = item.get("result")
        if item.get("tool_id") != "assistant.navigate" or not isinstance(node, dict):
            continue
        key = node.get("entity")
        if not isinstance(key, str):
            tool = node.get("tool")
            key = tool.get("id") if isinstance(tool, dict) else tuple(node.get("path", ()))
        nodes[key] = node
    return list(nodes.values())


def _opened_action_ids(state):
    return {
        node["tool"]["id"]
        for node in _opened_contracts(state)
        if isinstance(node.get("tool"), dict) and isinstance(node["tool"].get("id"), str)
    }


def _opened_read_entities(state):
    return {
        node["entity"]
        for node in _opened_contracts(state)
        if isinstance(node.get("entity"), str)
    }


def _has_non_navigation_result(state):
    return any(item.get("tool_id") not in {"assistant.navigate"} for item in state["results"])


def _literal_context():
    now = timezone.localtime()
    return {
        "current_date": now.date().isoformat(),
        "current_datetime": now.isoformat(),
        "timezone": settings.TIME_ZONE,
    }


def _run_state(conversation, message):
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict) or not isinstance(state.get("task"), str):
        state = {"task": message, "results": []}
    state.setdefault("results", [])
    if not isinstance(state["results"], list):
        state["results"] = []
    state.setdefault("errors", [])
    if not isinstance(state["errors"], list):
        state["errors"] = []
    state.setdefault("trace", [])
    if not isinstance(state["trace"], list):
        state["trace"] = []
    state.setdefault("rejected_calls", [])
    if not isinstance(state["rejected_calls"], list):
        state["rejected_calls"] = []
    return state


def _save_state(conversation, state):
    conversation.context["agent_run"] = state
    conversation.save(update_fields=["context"])


def _finish_state(conversation):
    conversation.context.pop("agent_run", None)
    conversation.context.pop("pending_action", None)
    conversation.save(update_fields=["context"])


def _trace_value(value, depth=0):
    """Keep an inspectable but bounded and secret-safe execution trace."""
    from .assistant_read import _looks_like_secret, _sensitive

    if depth >= 4:
        return "[сокращено]"
    if isinstance(value, dict):
        return {
            str(key): "[скрыто]" if _sensitive(str(key)) else _trace_value(item, depth + 1)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return [_trace_value(item, depth + 1) for item in value[:20]] + (["[сокращено]"] if len(value) > 20 else [])
    if isinstance(value, str):
        return "[скрыто]" if _looks_like_secret(value) else value[:500] + ("…" if len(value) > 500 else "")
    return value


def _journal_value(value, depth=0):
    """Keep the full observable JSON protocol while redacting secrets."""
    from .assistant_read import _looks_like_secret, _sensitive

    if depth >= 20:
        return "[ограничена глубина]"
    if isinstance(value, dict):
        return {
            str(key): "[скрыто]" if _sensitive(str(key)) else _journal_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_journal_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return "[скрыто]" if _looks_like_secret(value) else value
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _debug_event(conversation, stage, *, event_input=None, event_output=None, error=""):
    """Persist the observable agent protocol, never hidden model reasoning."""
    from .assistant_conversations import add_debug_event

    add_debug_event(
        conversation, stage, event_input=_journal_value(event_input) if event_input is not None else None,
        event_output=_journal_value(event_output) if event_output is not None else None, error=error,
    )


def _trace(state, tool_id, status, *, conversation=None, tool_input=None, tool_output=None, error=""):
    entry = {"step": len(state["trace"]) + 1, "tool_id": tool_id, "status": status}
    if tool_input is not None:
        entry["input"] = _trace_value(tool_input)
    if tool_output is not None:
        entry["output"] = _trace_value(tool_output)
    if error:
        entry["error"] = error
    state["trace"].append(entry)
    if conversation is not None:
        _debug_event(
            conversation, f"backend.{tool_id}",
            event_input={"status": status, "arguments": tool_input},
            event_output=tool_output, error=error,
        )
    logger.info("assistant trace step=%s tool=%s status=%s%s", entry["step"], tool_id, status, f" error={error}" if error else "")


def _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejected_step=None):
    opened_actions = _opened_action_ids(state)
    prompt = json.dumps(
        {
            "task": (
                "You are PSADMIN's Russian-speaking business agent. Solve the user's actual request using verified "
                "PSADMIN capabilities and results. Work incrementally: decide only the next necessary step, wait for "
                "its real backend result, then decide again. Do not construct a complete technical plan first. "
                "For ordinary conversation that needs neither business data nor an operation, answer directly. "
                "For PSADMIN data or operations, use assistant.navigate to explore the capability graph. You may open "
                "an intermediate node or a known leaf directly. Read a detailed contract before using its fields or arguments. "
                "Never guess database field names, entity identifiers, action arguments or allowed values. Use only metadata "
                "returned by assistant.navigate. Treat an entity description and its business views as facts about data origin "
                "and pre-applied rules: never add a filter merely to restate them or invent a value for a field without choices. "
                "Do not ask for optional filters when the request is already executable. "
                "Ask only for a genuinely required parameter or an ambiguity of an opened read contract that cannot be obtained from the request, conversation, or available tools. "
                "Never ask for storage formats, timezones, numeric notation, or bounds of a named calendar period; normalize them "
                "using literal_context and the returned contract. Use read for application data, existing registered actions for "
                "changes, and compute only for listed arithmetic over completed_step_results. A tool call is not a completed task. "
                "A zero-record READ proves only that its executed query returned zero records, not that the whole entity is empty. "
                "Inspect the returned query and entity contract; if an optional filter was your unsupported assumption, you may safely "
                "retry without it, but never remove a condition explicitly requested by the user. Never invent successful execution or current facts. For changes, prepare the real registered action; backend requires "
                "confirmation. Treat a new user task as independent from completed or rejected query filters; use old results only when the user explicitly continues that task. When a tool is rejected, use its actual error and opened metadata to choose a correction; do not repeat "
                "the same invalid call. Finish a data task only from verified results, or say it cannot be performed. Return valid JSON "
                "matching response_schema."
            ),
            "registered_tools": _tool_catalogue(tools, identifiers=opened_actions | {"read", "assistant.navigate"}),
            "capability_tree": capability_tree(user) if not state["results"] else None,
            "literal_context": _literal_context(),
            "compute": {
                "tool_id": "compute",
                "operations": sorted(_COMPUTE_OPERATIONS),
                "arguments": {"operation": "one listed operation", "inputs": [{"step": "completed step number", "path": "safe result path"}]},
                "rule": "inputs must reference only completed_step_results; use [] to collect a list field",
            },
            "business_context": conversation.context.get("business_context", {}),
            "page_context": page_context or {},
            "attachments": attachments or [],
            "history": _history(conversation),
            "original_task": state["task"],
            "opened_capability_nodes": _opened_contracts(state),
            "latest_user_message": message,
            "completed_step_results": state["results"],
            "execution_errors": state.get("errors", [])[-3:],
            "rejected_step": rejected_step,
            "response_schema": {
                "reply": "concise Russian text",
                "status": "next|needs_input|final|cancel",
                "basis": "verified_steps|user_supplied|unavailable",
                "next_step": {"tool_id": "registered id or compute", "purpose": "why this next step is needed", "arguments": "object"},
                "missing_inputs": ["required human-readable parameter, only for needs_input"],
                "missing_for": "opened action id or opened read entity, only for needs_input",
            },
        },
        ensure_ascii=False,
    )
    preflight()
    _debug_event(conversation, "decision.request", event_input=json.loads(prompt))
    result = _request(prompt, **({"image_data_urls": image_data_urls} if image_data_urls else {}))
    _debug_event(conversation, "decision.response", event_output=result)
    if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
        raise TenderAIError("Ассистент вернул неполный ответ. Попробуйте ещё раз.")
    return result


def _next_step(decision, tools):
    status = decision.get("status", "")
    if status not in {"next", "needs_input", "final", "cancel"}:
        raise TenderAIError("Ассистент вернул некорректный статус шага.")
    step = decision.get("next_step")
    if status == "next":
        if not isinstance(step, dict) or not isinstance(step.get("tool_id"), str) or not isinstance(step.get("arguments"), dict):
            raise TenderAIError("Ассистент не указал корректный следующий шаг.")
        identifier = step["tool_id"]
        if identifier != "compute":
            tool = next((item for item in tools if item["id"] == identifier), None)
            if tool is None:
                raise TenderAIError("Ассистент запросил действие вне энциклопедии.")
            if not tool["available"]:
                raise TenderAIError("Ассистент запросил действие без прав доступа.")
        return identifier, step["arguments"], str(step.get("purpose", "")).strip()[:500]
    if step is not None:
        raise TenderAIError("Следующий шаг разрешён только со статусом next.")
    return None, None, ""


def _validate_step_access(identifier, arguments, state, user):
    """Require a graph contract before READ or an app action can run."""
    if identifier == "read":
        from .assistant_read import ReadPlanError, plan_contract, resolve_entity, validate_plan

        if not isinstance(arguments, dict):
            raise TenderAIError("Параметры инструмента read должны быть объектом.")
        unknown = set(arguments) - {"plan"}
        if unknown:
            raise TenderAIError("Недопустимые параметры инструмента read: " + ", ".join(sorted(unknown)))
        if "plan" not in arguments:
            raise TenderAIError("Для read обязателен параметр plan.")
        plan = arguments["plan"]
        if not isinstance(plan, dict):
            raise TenderAIError("Параметр read.plan должен быть объектом.")
        contract = plan_contract()["properties"]
        unknown_plan = set(plan) - set(contract)
        if unknown_plan:
            raise TenderAIError("Недопустимые параметры READ-плана: " + ", ".join(sorted(unknown_plan)))
        if "entity" not in plan or plan["entity"] in (None, ""):
            raise TenderAIError("В READ-плане обязателен параметр entity из открытого описания сущности.")
        entity = plan["entity"]
        opened_entities = _opened_read_entities(state)
        try:
            canonical_entity = resolve_entity(entity)
        except ReadPlanError as exc:
            opened = ", ".join(sorted(opened_entities)) or "нет"
            raise TenderAIError(
                f"Недопустимый идентификатор сущности {entity!r}. "
                f"Используйте идентификатор из открытого контракта: {opened}."
            ) from exc
        if entity != canonical_entity:
            raise TenderAIError(
                f"Недопустимый идентификатор сущности {entity!r}. "
                f"Используйте канонический идентификатор {canonical_entity!r} из открытого контракта."
            )
        if canonical_entity not in opened_entities:
            raise TenderAIError("Перед чтением нужно открыть описание выбранной сущности через карту возможностей.")
        try:
            validate_plan(user, plan)
        except ReadPlanError as exc:
            raise TenderAIError(str(exc)) from exc
    elif identifier not in {"assistant.navigate", "compute"} and identifier not in _opened_action_ids(state):
        raise TenderAIError("Перед действием нужно открыть его описание через карту возможностей.")


def _call_signature(identifier, arguments):
    payload = json.dumps({"tool_id": identifier, "arguments": arguments}, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(payload.encode()).hexdigest()


def _rejected_call_error(state, signature):
    for item in reversed(state["rejected_calls"]):
        if isinstance(item, dict) and item.get("signature") == signature:
            return item.get("error", "Вызов был отклонён")
        if item == signature:
            return "Вызов был отклонён"
    return None


def _remember_rejected_call(state, signature, error):
    if signature and _rejected_call_error(state, signature) is None:
        state["rejected_calls"].append({"signature": signature, "error": error})


def _cached_navigation_result(state, arguments):
    if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), list):
        return None
    path = arguments["path"]
    return next((node for node in _opened_contracts(state) if node.get("path") == path), None)


def _needs_input_is_grounded(decision, state):
    missing = decision.get("missing_inputs")
    if not isinstance(missing, list) or not missing or not all(isinstance(item, str) and item.strip() for item in missing):
        return False
    target = decision.get("missing_for")
    for node in _opened_contracts(state):
        tool = node.get("tool")
        if not isinstance(tool, dict):
            continue
        required = {
            name for name, kind in tool.get("input_schema", {}).items()
            if isinstance(kind, str) and not kind.endswith("?") and not (name == "type" and kind == "object")
        }
        if required and target == tool.get("id"):
            return True
    return False


def _reference_value(results, reference):
    if not isinstance(reference, dict) or set(reference) != {"step", "path"}:
        raise TenderAIError("Вычисление должно ссылаться на результат предыдущего шага.")
    step, path = reference["step"], reference["path"]
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= len(results):
        raise TenderAIError("Вычисление ссылается на несуществующий результат шага.")
    if not isinstance(path, str) or not _REFERENCE_PATH.fullmatch(path):
        raise TenderAIError("Некорректный путь к результату предыдущего шага.")
    values = [results[step - 1]["result"]]
    for part in path.split("."):
        list_mode = part.endswith("[]")
        index_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]", part)
        key = part[:-2] if list_mode else (index_match.group(1) if index_match else part)
        next_values = []
        for value in values:
            if not isinstance(value, dict) or key not in value:
                raise TenderAIError("Вычисление ссылается на отсутствующее поле результата.")
            selected = value[key]
            if list_mode:
                if not isinstance(selected, list):
                    raise TenderAIError("Вычисление ожидает список в результате шага.")
                next_values.extend(selected)
            elif index_match:
                index = int(index_match.group(2))
                if not isinstance(selected, list) or index >= len(selected):
                    raise TenderAIError("Вычисление ссылается на отсутствующий элемент результата.")
                next_values.append(selected[index])
            else:
                next_values.append(selected)
        values = next_values
    return values if len(values) != 1 or "[]" in path else values[0]


def _numbers(value):
    values = value if isinstance(value, list) else [value]
    try:
        return [Decimal(str(item)) for item in values]
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TenderAIError("Вычисление доступно только для числовых результатов.") from exc


def _decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _compute(arguments, results):
    if not isinstance(arguments, dict) or set(arguments) != {"operation", "inputs"}:
        raise TenderAIError("Некорректные параметры вычисления.")
    operation, inputs = arguments["operation"], arguments["inputs"]
    if operation not in _COMPUTE_OPERATIONS or not isinstance(inputs, list) or not inputs:
        raise TenderAIError("Некорректное вычисление.")
    values = [_reference_value(results, item) for item in inputs]
    flattened = []
    for value in values:
        flattened.extend(value if isinstance(value, list) else [value])
    if operation == "count":
        return {"operation": operation, "value": len(flattened)}
    numbers = _numbers(flattened)
    if operation == "sum":
        value = sum(numbers)
    elif operation == "avg":
        value = sum(numbers) / len(numbers)
    elif operation == "min":
        value = min(numbers)
    elif operation == "max":
        value = max(numbers)
    elif operation in {"subtract", "divide", "percent_change"}:
        if len(numbers) != 2:
            raise TenderAIError("Для этого вычисления нужны ровно два числовых значения.")
        if operation == "subtract":
            value = numbers[0] - numbers[1]
        elif operation == "divide":
            if numbers[1] == 0:
                raise TenderAIError("Деление на ноль запрещено.")
            value = numbers[0] / numbers[1]
        else:
            if numbers[0] == 0:
                raise TenderAIError("Процентное изменение от нуля не определено.")
            value = (numbers[1] - numbers[0]) / numbers[0] * 100
    return {"operation": operation, "value": _decimal_text(value)}


def _execute_loop(conversation, user, message, state, tools, page_context, attachments, image_data_urls):
    if not isinstance(state.get("errors"), list):
        state["errors"] = []
    if not isinstance(state.get("trace"), list):
        state["trace"] = []
    rejected_step = None
    tool_calls = 0
    repairs = 0
    for _ in range(MAX_AGENT_DECISIONS):
        identifier = arguments = signature = None
        decision = _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejected_step)
        status = decision.get("status")
        if status == "cancel":
            _finish_state(conversation)
            return {"text": decision["reply"].strip() or "Задача отменена.", "kind": "text", "data": {}}
        if status == "needs_input":
            if not _needs_input_is_grounded(decision, state):
                rejected_step = {"tool_id": "needs_input", "error": "Уточнение допустимо только для обязательного параметра открытого ACTION; READ нужно выполнить по доступному контракту без необязательных фильтров."}
                state["errors"].append(rejected_step)
                _trace(state, "needs_input", "rejected", conversation=conversation, error=rejected_step["error"])
                continue
            _save_state(conversation, state)
            return {"text": decision["reply"].strip() or "Уточните недостающие данные.", "kind": "text", "data": {"task_pending": True}}
        if status == "final":
            if state["results"] and not _has_non_navigation_result(state) and decision.get("basis") == "verified_steps":
                raise TenderAIError("Ассистент завершил задачу о данных до получения результата инструмента.")
            if not state["results"] and decision.get("basis") not in {"user_supplied", "unavailable"}:
                raise TenderAIError("Ассистент завершил задачу без проверенного результата.")
            _finish_state(conversation)
            return {"text": decision["reply"].strip(), "kind": "tool_result", "data": {"steps": state["results"]}}
        try:
            identifier, arguments, purpose = _next_step(decision, tools)
            signature = _call_signature(identifier, arguments)
            previous_error = _rejected_call_error(state, signature)
            if previous_error:
                raise TenderAIError(f"Этот вызов уже был отклонён: {previous_error}")
            _validate_step_access(identifier, arguments, state, user)
        except TenderAIError as exc:
            repairs += 1
            _remember_rejected_call(state, signature, str(exc))
            rejected_step = {
                "tool_id": identifier or "decision",
                "arguments": arguments if identifier == "read" and isinstance(arguments, dict) else {},
                "error": str(exc),
            }
            state["errors"].append(rejected_step)
            _trace(state, rejected_step["tool_id"], "rejected", conversation=conversation, tool_input=rejected_step["arguments"], error=str(exc))
            if repairs > MAX_REPAIR_ATTEMPTS:
                raise
            continue
        try:
            if identifier == "compute":
                result = _compute(arguments, state["results"])
            elif identifier == "assistant.navigate" and (cached_result := _cached_navigation_result(state, arguments)) is not None:
                _trace(state, identifier, "cached", conversation=conversation, tool_input=arguments, tool_output=cached_result)
                rejected_step = None
                repairs = 0
                message, attachments, image_data_urls = "", [], None
                continue
            else:
                tool = next(item for item in tools if item["id"] == identifier)
                missing = [name for name in required_input_names(identifier) if arguments.get(name) in (None, "", [])]
                if missing:
                    _save_state(conversation, state)
                    return {"text": decision["reply"].strip() or "Уточните: " + ", ".join(missing), "kind": "text", "data": {"task_pending": True, "missing": missing}}
                if tool["requires_confirmation"]:
                    state["waiting_confirmation"] = {"tool_id": identifier, "arguments": arguments, "purpose": purpose}
                    _trace(state, identifier, "confirmation", conversation=conversation, tool_input=arguments)
                    _save_state(conversation, state)
                    return {"text": decision["reply"].strip(), "kind": "confirmation_required", "data": {"tool_id": identifier, "arguments": arguments}}
                result = execute_tool(identifier, user, arguments)
        except (AssistantToolError, TenderAIError) as exc:
            _remember_rejected_call(state, signature, str(exc))
            state["errors"].append({"tool_id": identifier, "purpose": purpose, "error": str(exc)})
            _trace(state, identifier, "rejected", conversation=conversation, tool_input=arguments, error=str(exc))
            _save_state(conversation, state)
            rejected_step = {
                "tool_id": identifier,
                "arguments": arguments if identifier == "read" else {"argument_names": sorted(arguments)},
                "error": str(exc),
            }
            message, attachments, image_data_urls = "", [], None
            repairs += 1
            if repairs > MAX_REPAIR_ATTEMPTS:
                raise TenderAIError(str(exc)) from exc
            continue
        state["results"].append({"number": len(state["results"]) + 1, "tool_id": identifier, "purpose": purpose, "result": result})
        state["errors"] = []
        _trace(state, identifier, "completed", conversation=conversation, tool_input=arguments, tool_output=result)
        rejected_step = None
        repairs = 0
        tool_calls += 1
        _save_state(conversation, state)
        if tool_calls >= MAX_TOOL_CALLS:
            break
        message, attachments, image_data_urls = "", [], None
    raise TenderAIError("Превышен безопасный предел обработки за один запуск. Продолжите задачу следующим сообщением.")


def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    """Run the existing validated loop from the first LLM decision."""
    tools = available_tools(user)
    state = _run_state(conversation, message)
    _save_state(conversation, state)
    return _execute_loop(conversation, user, message, state, tools, page_context, attachments, image_data_urls)


def continue_after_confirmation(conversation, user, tool_id, result):
    """Continue the same verified task after a confirmed UI action."""
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict) or not isinstance(state.get("results"), list):
        return None
    waiting = state.pop("waiting_confirmation", {})
    if waiting.get("tool_id") != tool_id:
        return None
    state["results"].append({
        "number": len(state["results"]) + 1,
        "tool_id": tool_id,
        "purpose": waiting.get("purpose", ""),
        "result": result,
    })
    _save_state(conversation, state)
    return _execute_loop(conversation, user, "", state, available_tools(user), None, [], None)
