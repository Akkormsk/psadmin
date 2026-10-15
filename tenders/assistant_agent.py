"""Small, validated execution loop for the global PSADMIN assistant.

The model chooses only the next step. The backend owns validation,
permissions, execution, confirmations and the hand-off of verified results to
the following step. There are no business-specific branches here.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
import os
import re

from .assistant_tools import (
    AssistantToolError,
    available_tools,
    execute_tool,
    required_input_names,
    workflow_catalog,
)
from .gateway_budget import preflight, report_line, spend_rub
from .services import TenderAIError, _ai_gateway_json


logger = logging.getLogger(__name__)
MAX_AGENT_STEPS = 8
_COMPUTE_OPERATIONS = {"count", "sum", "avg", "min", "max", "subtract", "divide", "percent_change"}
_REFERENCE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\]|\[\d+\]|\.[A-Za-z_][A-Za-z0-9_]*(?:\[\]|\[\d+\])?)*$")


def _history(conversation):
    events = list(conversation.feedback_events.order_by("-created_at", "-pk")[:20])
    return [
        {"role": (event.payload or {}).get("role", "user"), "text": event.raw_text}
        for event in reversed(events)
    ]


def _request(prompt, image_data_urls=None):
    model = os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", os.getenv("TIMEWEB_AI_MODEL", "gemini/gemini-3.1-flash-lite"))
    result, usage = _ai_gateway_json(
        prompt, model=model, max_tokens=1200, timeout=45, network_attempts=1,
        image_data_urls=image_data_urls,
    )
    cost = spend_rub(usage, model)
    logger.info("assistant gateway model=%s; %s%s", model, report_line(usage, {model: usage}), f"; call ~{cost:.4f} ₽" if cost is not None else "")
    return result


def _trace_block(direction, payload):
    """Plain-text diagnostic record of an already performed exchange.

    This is deliberately only a rendering helper: it neither changes a model
    prompt nor adds data to the agent state.
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return f"{direction}\n{text}"


def _trace_response(trace, *, kind="text", data=None):
    return {"text": "\n\n".join(trace), "kind": kind, "data": data or {}}


def _tool_catalogue(tools):
    """Give the model only the contract it needs to choose a next action."""
    return [
        {
            key: tool[key]
            for key in ("id", "name", "description", "input_schema", "read_only", "requires_confirmation", "available")
        }
        for tool in tools
    ]


def _run_state(conversation, message):
    state = conversation.context.get("agent_run")
    if not isinstance(state, dict) or not isinstance(state.get("task"), str):
        state = {"task": message, "results": []}
    state.setdefault("results", [])
    if not isinstance(state["results"], list):
        state["results"] = []
    return state


def _save_state(conversation, state):
    conversation.context["agent_run"] = state
    conversation.save(update_fields=["context"])


def _finish_state(conversation):
    conversation.context.pop("agent_run", None)
    conversation.context.pop("pending_action", None)
    conversation.save(update_fields=["context"])


def _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejection=None):
    read_catalog = []
    if any(tool["id"] == "read" and tool["available"] for tool in tools):
        from .assistant_read import catalogue
        read_catalog = catalogue(compact=True)
    prompt = json.dumps(
        {
            "task": (
                "You are PSADMIN's Russian-speaking agent. Solve the user's task to a verified final result. "
                "Choose exactly one next step at a time, see its real result, then choose the next step. "
                "Never invent tools, database values, calculations, or completed actions. "
                "Use read for application data and registered actions only for real site operations. "
                "For arithmetic over verified previous results, use compute with one of the supplied operations; it is not Python. "
                "If required information is absent, return status needs_input and one concise Russian question. "
                "If the result is complete, return status final and explain it using only completed_step_results. "
                "For a write action, propose it with its real arguments; backend will require confirmation before executing it. "
                "If previous_backend_rejection is present, correct that request before choosing another step. "
                "Return valid JSON matching response_schema."
            ),
            "registered_tools": _tool_catalogue(tools),
            "workflows": workflow_catalog(user, tools),
            "read_catalog": read_catalog,
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
            "latest_user_message": message,
            "completed_step_results": state["results"],
            "previous_backend_rejection": rejection,
            "response_schema": {
                "reply": "concise Russian text",
                "status": "next|needs_input|final|cancel",
                "basis": "verified_steps|user_supplied|unavailable",
                "next_step": {"tool_id": "registered id or compute", "purpose": "why this next step is needed", "arguments": "object"},
            },
        },
        ensure_ascii=False,
    )
    preflight()
    result = _request(prompt, **({"image_data_urls": image_data_urls} if image_data_urls else {}))
    return result, prompt


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
        raise TenderAIError("Шаг можно передавать только со статусом next.")
    return None, None, ""


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


def _execute_loop(conversation, user, message, state, tools, page_context, attachments, image_data_urls, trace=None):
    trace = list(trace or [])
    rejection = None
    for _ in range(MAX_AGENT_STEPS):
        decision, prompt = _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejection)
        trace.extend((_trace_block("BACKEND → LLM", prompt), _trace_block("LLM → BACKEND", decision)))
        if not isinstance(decision, dict) or not isinstance(decision.get("reply"), str):
            error = "Ассистент вернул неполный ответ."
            trace.append(_trace_block("BACKEND VALIDATION", {"error": error}))
            rejection = {"error": error, "response": decision}
            message, attachments, image_data_urls = "", [], None
            continue
        status = decision.get("status")
        if status == "cancel":
            _finish_state(conversation)
            return _trace_response(trace)
        try:
            identifier, arguments, purpose = _next_step(decision, tools)
        except TenderAIError as exc:
            error = str(exc)
            trace.append(_trace_block("BACKEND VALIDATION", {"error": error}))
            rejection = {"error": error, "response": decision}
            message, attachments, image_data_urls = "", [], None
            continue
        if status == "needs_input":
            _save_state(conversation, state)
            return _trace_response(trace, data={"task_pending": True})
        if status == "final":
            if not state["results"] and decision.get("basis") not in {"user_supplied", "unavailable"}:
                error = "Ассистент завершил задачу без проверенного результата."
                trace.append(_trace_block("BACKEND VALIDATION", {"error": error}))
                rejection = {"error": error, "response": decision}
                message, attachments, image_data_urls = "", [], None
                continue
            _finish_state(conversation)
            return _trace_response(trace, data={"steps": state["results"]})
        if identifier == "compute":
            trace.append(_trace_block("BACKEND → TOOL", {"tool_id": identifier, "arguments": arguments}))
            try:
                result = _compute(arguments, state["results"])
            except TenderAIError as exc:
                error = str(exc)
                trace.append(_trace_block("TOOL → BACKEND", {"error": error}))
                rejection = {"tool_id": identifier, "arguments": arguments, "error": error}
                message, attachments, image_data_urls = "", [], None
                continue
        else:
            tool = next(item for item in tools if item["id"] == identifier)
            missing = [name for name in required_input_names(identifier) if arguments.get(name) in (None, "", [])]
            if missing:
                _save_state(conversation, state)
                return _trace_response(trace, data={"task_pending": True, "missing": missing})
            if tool["requires_confirmation"]:
                state["waiting_confirmation"] = {"tool_id": identifier, "arguments": arguments, "purpose": purpose}
                _save_state(conversation, state)
                return _trace_response(trace, kind="confirmation_required", data={"tool_id": identifier, "arguments": arguments})
            try:
                trace.append(_trace_block("BACKEND → TOOL", {"tool_id": identifier, "arguments": arguments}))
                result = execute_tool(identifier, user, arguments)
            except AssistantToolError as exc:
                error = str(exc)
                trace.append(_trace_block("TOOL → BACKEND", {"error": error}))
                if identifier == "read":
                    rejection = {"tool_id": identifier, "arguments": arguments, "error": error}
                    message, attachments, image_data_urls = "", [], None
                    continue
                return _trace_response(trace)
        trace.append(_trace_block("TOOL → BACKEND", result))
        rejection = None
        state["results"].append({"number": len(state["results"]) + 1, "tool_id": identifier, "purpose": purpose, "result": result})
        _save_state(conversation, state)
        message, attachments, image_data_urls = "", [], None
    trace.append(_trace_block("BACKEND", {"error": f"Превышен безопасный предел {MAX_AGENT_STEPS} шагов за один запуск."}))
    _save_state(conversation, state)
    return _trace_response(trace, data={"task_pending": True})


def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    """Run a bounded sequence of registered, backend-validated steps."""
    tools = available_tools(user)
    state = _run_state(conversation, message)
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
    return _execute_loop(
        conversation, user, "", state, available_tools(user), None, [], None,
        trace=[_trace_block("TOOL → BACKEND", result)],
    )
