"""Plan-first, validated execution loop for the global PSADMIN assistant."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
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


def _trace(state, tool_id, status, *, tool_input=None, tool_output=None, error=""):
    entry = {"step": len(state["trace"]) + 1, "tool_id": tool_id, "status": status}
    if tool_input is not None:
        entry["input"] = _trace_value(tool_input)
    if tool_output is not None:
        entry["output"] = _trace_value(tool_output)
    if error:
        entry["error"] = error
    state["trace"].append(entry)
    logger.info("assistant trace step=%s tool=%s status=%s%s", entry["step"], tool_id, status, f" error={error}" if error else "")


def _trace_text(entries):
    labels = {"completed": "выполнен", "rejected": "отклонён", "confirmation": "ожидает подтверждения"}
    lines = ["Диагностика шагов:"]
    for entry in entries:
        line = f"{entry['step']}. {entry['tool_id']} — {labels.get(entry['status'], entry['status'])}"
        if "input" in entry:
            line += "\n   вход: " + json.dumps(entry["input"], ensure_ascii=False)
        if "output" in entry:
            line += "\n   выход: " + json.dumps(entry["output"], ensure_ascii=False)
        if entry.get("error"):
            line += f": {entry['error']}"
        lines.append(line)
    return "\n".join(lines)


def _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejected_step=None):
    planned_ids = {step.get("tool_id") for step in state.get("plan", []) if isinstance(step, dict)}
    opened_nodes = [step["result"] for step in state["results"] if step.get("tool_id") == "assistant.navigate"]
    prompt = json.dumps(
        {
            "task": (
                "You are PSADMIN's Russian-speaking agent. Solve the user's task to a verified final result. "
                "Use execution_plan as the intended route: start with its first unfinished step, see its real result, then choose the next step. "
                "Change that route only when a verified result makes the planned step impossible or unnecessary. "
                "Never invent tools, database values, calculations, or completed actions. "
                "The execution plan already contains full capability paths selected from the complete capability tree. "
                "Use assistant.navigate only for the exact full path in the next planned navigation step; never navigate a root or intermediate node. "
                "After its verified result appears in opened_capability_nodes, use that detailed node for the planned read or action; never guess a field, model, action or its parameters. "
                "Use read for application data and registered actions only for real site operations. "
                "For read, select fields by their human label and help_text returned by the selected entity node; technical names are only request keys. "
                "Never show technical model or field names to the user: explain choices with their Russian human labels. "
                "Ask a question only when the user's wording genuinely fits multiple different human-labelled fields; if the user has already named the meaning, choose it and continue. "
                "Normalize dates, date-times, numbers, amounts, percentages and statuses yourself using literal_context and filter_contract returned by the selected entity node. "
                "Never ask the user for a storage format, timezone, numeric notation or bounds of a named calendar period. "
                "Resolve a named month or other calendar period from literal_context; use its current calendar year unless the user supplied another year. "
                "For arithmetic over verified previous results, use compute with one of the supplied operations; it is not Python. "
                "If required information is absent, return status needs_input and one concise Russian question. "
                "If the result is complete, return status final and explain it using only completed_step_results. "
                "Include next_step only when status is next. "
                "For a write action, propose it with its real arguments; backend will require confirmation before executing it. "
                "If rejected_step is present, correct that step using the selected graph node and its exact backend reason. "
                "Do not ask the user for information already in original_task merely because backend validation rejected a step. "
                "Do not use a failed result as input or finish successfully. "
                "Return valid JSON matching response_schema."
            ),
            "registered_tools": _tool_catalogue(tools, identifiers=planned_ids | {"read", "assistant.navigate"}),
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
            "execution_plan": state.get("plan", []),
            "opened_capability_nodes": opened_nodes,
            "latest_user_message": message,
            "completed_step_results": state["results"],
            "execution_errors": state.get("errors", [])[-3:],
            "rejected_step": rejected_step,
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
    if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
        raise TenderAIError("Ассистент вернул неполный ответ. Попробуйте ещё раз.")
    return result


def _validate_plan_steps(plan, tools):
    if not isinstance(plan, dict) or not isinstance(plan.get("reply"), str):
        raise TenderAIError("Ассистент не сформировал корректный план.")
    status = plan.get("status")
    if status not in {"ready", "needs_input", "final", "cancel"}:
        raise TenderAIError("Ассистент не указал состояние плана.")
    steps = plan.get("steps", [])
    if status in {"needs_input", "final", "cancel"}:
        if steps not in (None, []):
            raise TenderAIError("Завершённый план или план с уточняющим вопросом не должен содержать шаги.")
        return []
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_AGENT_STEPS:
        raise TenderAIError("План должен содержать от одного до восьми шагов.")
    known_ids = set()
    validated = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"id", "tool_id", "purpose", "arguments", "inputs", "outputs"}:
            raise TenderAIError("Шаг плана имеет неверную структуру.")
        step_id, identifier = step["id"], step["tool_id"]
        if not isinstance(step_id, str) or not step_id or step_id in known_ids:
            raise TenderAIError("У шагов плана должны быть уникальные идентификаторы.")
        if not isinstance(identifier, str) or not isinstance(step["purpose"], str) or not isinstance(step["arguments"], dict):
            raise TenderAIError("Шаг плана содержит неверную команду или параметры.")
        if identifier != "compute":
            tool = next((item for item in tools if item["id"] == identifier), None)
            if tool is None or not tool["available"]:
                raise TenderAIError("План ссылается на недоступную функцию.")
        if not isinstance(step["inputs"], list) or not step["inputs"] or not isinstance(step["outputs"], list) or not all(isinstance(value, str) and value for value in step["outputs"]):
            raise TenderAIError("У шага плана должны быть указаны входы и выходы.")
        for value in step["inputs"]:
            if not isinstance(value, dict) or set(value) != {"from", "value"} or not isinstance(value["value"], str):
                raise TenderAIError("Связь между шагами плана имеет неверную структуру.")
            source = value["from"]
            if source != "user" and source not in known_ids:
                raise TenderAIError("Шаг плана ссылается на ещё не полученный результат.")
        known_ids.add(step_id)
        validated.append(step)
    return validated


def _format_plan(reply, steps):
    lines = [reply.strip(), "", "План действий (исполнение пока отключено):"]
    for number, step in enumerate(steps, start=1):
        inputs = "; ".join(f"{value['from']}: {value['value']}" for value in step["inputs"]) or "нет"
        outputs = "; ".join(step["outputs"])
        lines.extend((
            f"{number}. {step['purpose']}",
            f"   Команда: {step['tool_id']}",
            f"   Вход: {inputs}",
            f"   Выход: {outputs}",
        ))
    return "\n".join(lines)


def _plan_task(conversation, user, message, tools, page_context, attachments, image_data_urls):

    prompt = json.dumps(
        {
            "task": (
                "You are PSADMIN's Russian-speaking agent. First decide whether the user's request needs current PSADMIN data, "
                "a PSADMIN action, or a verified calculation. If it does not, answer naturally in Russian with status final and an empty steps list; "
                "do not use tools or the capability graph. Do not invent current external facts: state their absence honestly. "
                "If it does, design a complete route to a verified final result. Use only registered actions, read, compute, and assistant.navigate. "
                "Do not invent functions, records, or results. capability_tree is the complete compact route map. "
                "Select a full path from it in one decision: never navigate the root or an intermediate node. "
                "For every READ or ACTION, first add assistant.navigate with its complete leaf path, then the corresponding read or registered action. "
                "For every step name the exact tool_id, known arguments, inputs and outputs. Every step must list at least one input: "
                "use from=user for conditions from the user's request, or the earlier step id for a prior result; never use an empty inputs list. "
                "For a read, first navigate to its entity, then use arguments.plan with entity (never model), fields, filters, order_by and limit. "
                "Choose read fields by their human label and help_text returned by the entity node. Technical names are request keys only: never include them in reply text. "
                "Ask the user only when several human-labelled fields genuinely match the requested meaning; use a meaning already supplied in the conversation without asking again. "
                "Normalize dates, date-times, numbers, amounts, percentages and statuses yourself using literal_context and filter_contract returned by the selected entity node. "
                "Never ask for a storage format, timezone, numeric notation or bounds of a named calendar period; resolve it from literal_context, using its current calendar year unless the user supplied another. "
                "Do not claim that any step was executed. If user data is necessary before planning, return needs_input with no steps. "
                "Return valid JSON matching response_schema."
            ),
            "registered_tools": _tool_catalogue(tools, identifiers={"assistant.navigate", "read"}),
            "capability_tree": capability_tree(user),
            "literal_context": _literal_context(),
            "compute": {"tool_id": "compute", "operations": sorted(_COMPUTE_OPERATIONS)},
            "business_context": conversation.context.get("business_context", {}),
            "page_context": page_context or {},
            "attachments": attachments or [],
            "history": _history(conversation),
            "user_message": message,
            "response_schema": {
                "reply": "concise Russian text",
                "status": "ready|needs_input|final|cancel",
                "rule": "ready requires one to eight tool steps; needs_input, final and cancel require an empty steps list",
                "steps": [{
                    "id": "unique step id",
                    "tool_id": "registered id, read, or compute",
                    "purpose": "why this step is needed",
                    "arguments": "known object arguments; use an empty object when a value must come from a previous step",
                    "inputs": [{"from": "user or earlier step id", "value": "what is received"}],
                    "outputs": ["what this step produces for a later step or final answer"],
                }],
            },
        },
        ensure_ascii=False,
    )
    preflight()
    plan = _request(prompt, **({"image_data_urls": image_data_urls} if image_data_urls else {}))
    steps = _validate_plan_steps(plan, tools)
    if plan["status"] == "needs_input":
        return {"text": plan["reply"].strip() or "Уточните недостающие данные.", "kind": "text", "data": {"task_pending": True}}
    _finish_state(conversation)
    if plan["status"] in {"final", "cancel"}:
        fallback = "Задача отменена." if plan["status"] == "cancel" else "Готово."
        return {"text": plan["reply"].strip() or fallback, "kind": "text", "data": {}}
    return {"text": _format_plan(plan["reply"], steps), "kind": "text", "data": {"plan": steps}}


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


def _is_capability_leaf(path):
    return (
        isinstance(path, list)
        and ((len(path) == 3 and path[0] in {"read", "action"}) or path == ["compute"])
    )


def _open_first_selected_capability(state, user):
    """Open the planner-selected leaf without another LLM navigation turn."""
    plan = state.get("plan", [])
    if not plan or not isinstance(plan[0], dict) or plan[0].get("tool_id") != "assistant.navigate":
        return
    arguments = plan[0].get("arguments", {})
    path = arguments.get("path") if isinstance(arguments, dict) else None
    if not _is_capability_leaf(path):
        raise TenderAIError("План выбрал неполный путь в карте возможностей.")
    result = execute_tool("assistant.navigate", user, arguments)
    state["results"].append({
        "number": len(state["results"]) + 1,
        "plan_step_id": plan[0].get("id"),
        "tool_id": "assistant.navigate",
        "purpose": plan[0].get("purpose", "Открыть возможность"),
        "result": result,
    })
    _trace(state, "assistant.navigate", "completed", tool_input=arguments, tool_output=result)


def _execute_loop(conversation, user, message, state, tools, page_context, attachments, image_data_urls):
    if not isinstance(state.get("errors"), list):
        state["errors"] = []
    if not isinstance(state.get("trace"), list):
        state["trace"] = []
    rejected_step = None
    for _ in range(MAX_AGENT_STEPS):
        decision = _decision(conversation, user, message, state, tools, page_context, attachments, image_data_urls, rejected_step)
        status = decision.get("status")
        if status == "cancel":
            _finish_state(conversation)
            return {"text": decision["reply"].strip() or "Задача отменена.", "kind": "text", "data": {}}
        identifier, arguments, purpose = _next_step(decision, tools)
        if status == "needs_input":
            _save_state(conversation, state)
            return {"text": decision["reply"].strip() or "Уточните недостающие данные.", "kind": "text", "data": {"task_pending": True}}
        if status == "final":
            if not state["results"] and decision.get("basis") not in {"user_supplied", "unavailable"}:
                raise TenderAIError("Ассистент завершил задачу без проверенного результата.")
            _finish_state(conversation)
            return {"text": decision["reply"].strip(), "kind": "tool_result", "data": {"steps": state["results"]}}
        try:
            if identifier == "compute":
                result = _compute(arguments, state["results"])
            else:
                tool = next(item for item in tools if item["id"] == identifier)
                missing = [name for name in required_input_names(identifier) if arguments.get(name) in (None, "", [])]
                if missing:
                    _save_state(conversation, state)
                    return {"text": decision["reply"].strip() or "Уточните: " + ", ".join(missing), "kind": "text", "data": {"task_pending": True, "missing": missing}}
                if tool["requires_confirmation"]:
                    state["waiting_confirmation"] = {"tool_id": identifier, "arguments": arguments, "purpose": purpose}
                    _trace(state, identifier, "confirmation", tool_input=arguments)
                    _save_state(conversation, state)
                    return {"text": decision["reply"].strip(), "kind": "confirmation_required", "data": {"tool_id": identifier, "arguments": arguments}}
                result = execute_tool(identifier, user, arguments)
        except (AssistantToolError, TenderAIError) as exc:
            state["errors"].append({"tool_id": identifier, "purpose": purpose, "error": str(exc)})
            _trace(state, identifier, "rejected", tool_input=arguments, error=str(exc))
            _save_state(conversation, state)
            rejected_step = {
                "tool_id": identifier,
                "arguments": arguments if identifier == "read" else {"argument_names": sorted(arguments)},
                "error": str(exc),
            }
            message, attachments, image_data_urls = "", [], None
            continue
        state["results"].append({"number": len(state["results"]) + 1, "tool_id": identifier, "purpose": purpose, "result": result})
        state["errors"] = []
        _trace(state, identifier, "completed", tool_input=arguments, tool_output=result)
        rejected_step = None
        _save_state(conversation, state)
        message, attachments, image_data_urls = "", [], None
    errors = state.get("errors", [])
    suffix = f" Последняя причина: {errors[-1]['error']}" if errors else ""
    trace = _trace_text(state["trace"][-MAX_AGENT_STEPS:]) if state["trace"] else ""
    raise TenderAIError(f"Превышен безопасный предел {MAX_AGENT_STEPS} шагов за один запуск. Продолжите задачу следующим сообщением.{suffix}\n\n{trace}")


def reply(conversation, user, message, page_context=None, attachments=None, image_data_urls=None):
    """Plan a route once, then execute it through the existing validated loop."""
    tools = available_tools(user)
    state = conversation.context.get("agent_run")
    if isinstance(state, dict) and isinstance(state.get("task"), str) and isinstance(state.get("results"), list):
        return _execute_loop(conversation, user, message, state, tools, page_context, attachments, image_data_urls)
    planned = _plan_task(conversation, user, message, tools, page_context, attachments, image_data_urls)
    steps = planned["data"].get("plan")
    if not isinstance(steps, list):
        return planned
    state = _run_state(conversation, message)
    state["plan"] = steps
    _open_first_selected_capability(state, user)
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
