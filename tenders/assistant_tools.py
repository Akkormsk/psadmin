from __future__ import annotations

from dataclasses import dataclass
import re
from importlib import import_module
from django.utils.module_loading import import_string
from typing import Any, Callable

from django.apps import apps


from .assistant_read import input_schema as read_input_schema



class AssistantToolError(Exception):
    pass


class AssistantToolConfirmationRequired(AssistantToolError):
    pass


@dataclass(frozen=True)
class AssistantTool:
    identifier: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    requires_confirmation: bool
    requires_superuser: bool
    executor: Callable[[Any, dict[str, Any]], dict[str, Any]]
    ui: dict[str, Any] | None = None
    source: str = ""


_TOOLS: dict[str, AssistantTool] = {}
_DISCOVERED = False


def _discover_app_actions():
    """Load declarations published by installed business applications.

    The agent core never imports a concrete business app or enumerates its
    actions.  An app may expose ``assistant_capabilities`` next to its real
    services; importing that module only registers metadata and a reference to
    the shared executor used by the UI.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    for config in apps.get_app_configs():
        try:
            import_module(f"{config.name}.assistant_capabilities")
        except ModuleNotFoundError as exc:
            if exc.name != f"{config.name}.assistant_capabilities":
                raise
    _DISCOVERED = True


def register_tool(tool: AssistantTool) -> AssistantTool:
    if tool.identifier in _TOOLS:
        raise RuntimeError(f"Duplicate assistant tool: {tool.identifier}")
    _TOOLS[tool.identifier] = tool
    return tool


def available_tools(user) -> list[dict[str, Any]]:
    _discover_app_actions()
    return [
        {
            "id": tool.identifier,
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "read_only": tool.read_only,
            "requires_confirmation": tool.requires_confirmation,
            "available": not tool.requires_superuser or user.is_superuser,
            "ui": tool.ui,
            "source": tool.source or f"{tool.executor.__module__}.{tool.executor.__qualname__}",
        }
        for tool in _TOOLS.values()
    ]


def execute_tool(identifier: str, user, payload: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    _discover_app_actions()
    tool = _TOOLS.get(identifier)
    if not tool:
        raise AssistantToolError("Неизвестное действие ассистента")
    if tool.requires_superuser and not user.is_superuser:
        raise AssistantToolError("Недостаточно прав для этого действия")
    if tool.requires_confirmation and not confirmed:
        raise AssistantToolConfirmationRequired("Действие требует явного подтверждения")
    if not isinstance(payload, dict):
        raise AssistantToolError("Некорректные параметры действия")
    unknown = set(payload) - set(tool.input_schema)
    if unknown:
        raise AssistantToolError("Недопустимые параметры действия: " + ", ".join(sorted(unknown)))
    missing = [
        name for name in required_input_names(identifier)
        if name not in payload or payload[name] is None
        or (isinstance(payload[name], str) and not payload[name].strip())
    ]
    if missing:
        raise AssistantToolError("Не заполнены обязательные поля: " + ", ".join(missing))
    for name, declared in tool.input_schema.items():
        if name not in payload or payload[name] is None:
            continue
        value = payload[name]
        if isinstance(declared, dict):
            if declared.get("type") == "object" and not isinstance(value, dict):
                raise AssistantToolError(f"Поле {name} должно быть объектом")
            continue
        if not isinstance(declared, str):
            continue
        kind = declared.rstrip("?")
        if kind == "boolean" and not isinstance(value, bool):
            raise AssistantToolError(f"Поле {name} должно иметь значение true или false")
        if kind.endswith("[]") and not isinstance(value, list):
            raise AssistantToolError(f"Поле {name} должно быть списком")
        if kind in {"object"} and not isinstance(value, dict):
            raise AssistantToolError(f"Поле {name} должно быть объектом")
    return tool.executor(user, payload)


def _openai_type(declared):
    """Translate the registry contract; the registry remains the sole source."""
    optional = isinstance(declared, str) and declared.endswith("?")
    value = declared[:-1] if optional else declared
    if isinstance(value, dict):
        schema = {"type": value.get("type", "object")}
        if schema["type"] == "object":
            schema["additionalProperties"] = False
        return schema, optional, False
    if not isinstance(value, str):
        return {"type": "string"}, optional, False
    if value.endswith("[]"):
        item = value[:-2]
        item_type = {"integer": "integer", "number": "number", "boolean": "boolean"}.get(item, "string")
        return {"type": "array", "items": {"type": item_type}}, optional, True
    if "|" in value:
        return {"type": "string", "enum": value.split("|")}, optional, True
    return {"type": {"integer": "integer", "number": "number", "boolean": "boolean", "object": "object", "array": "array"}.get(value, "string")}, optional, value not in {"object", "array"}


def openai_functions(user):
    """Function definitions and reversible names generated from registered tools."""
    definitions, names = [], {}
    for tool in available_tools(user):
        if not tool["available"] or tool["id"] == "assistant.navigate":
            continue
        name = re.sub(r"[^A-Za-z0-9_-]", "_", tool["id"])
        if not name or name in names:
            raise RuntimeError("OpenAI function name collision")
        properties, required, strict = {}, [], True
        for key, declared in tool["input_schema"].items():
            schema, optional, compatible = _openai_type(declared)
            if optional:
                schema["type"] = [schema["type"], "null"]
            properties[key] = schema
            required.append(key)
            strict = strict and compatible
        parameters = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        function = {"name": name, "description": tool["description"], "parameters": parameters}
        if strict:
            function["strict"] = True
        definitions.append({"type": "function", "function": function})
        names[name] = tool["id"]
    return definitions, names


def capability_issues(user) -> list[dict[str, str]]:
    """Check declarations against importable shared executors, without running them."""
    _discover_app_actions()
    issues = []
    for tool in _TOOLS.values():
        if not callable(tool.executor):
            issues.append({"id": tool.identifier, "issue": "invalid_executor", "source": tool.source})
        if not tool.read_only and not tool.requires_confirmation:
            issues.append({"id": tool.identifier, "issue": "missing_confirmation", "source": tool.source})
        if not tool.source:
            continue
        try:
            source = import_string(tool.source)
        except (ImportError, AttributeError, ValueError):
            issues.append({"id": tool.identifier, "issue": "missing_executor", "source": tool.source})
            continue
        if not callable(source):
            issues.append({"id": tool.identifier, "issue": "not_callable", "source": tool.source})
    return issues


def required_input_names(identifier: str) -> list[str]:
    """Required top-level inputs declared by a real action contract."""
    _discover_app_actions()
    tool = _TOOLS.get(identifier)
    if tool is None:
        return []
    return [
        name for name, field_type in tool.input_schema.items()
        if ((isinstance(field_type, str) and not (name == "type" and field_type == "object") and not field_type.endswith("?"))
            or (isinstance(field_type, dict) and field_type.get("required") is True))
    ]


def _read_data(user, payload):
    from .assistant_read import ReadPlanError, execute

    try:
        return execute(user, payload.get("plan"))
    except ReadPlanError as exc:
        raise AssistantToolError(str(exc)) from exc


def workflow_catalog(user, actions=None):
    """One runtime encyclopedia assembled solely from registered actions."""
    actions = available_tools(user) if actions is None else actions
    groups = {}
    for action in actions:
        namespace = action["id"].split(".", 1)[0]
        groups.setdefault(namespace, []).append(action)
    return [
        {
            "id": namespace,
            "actions": [action["id"] for action in members],
            "available": any(action["available"] for action in members),
            "requires_confirmation": any(action["requires_confirmation"] for action in members),
        }
        for namespace, members in sorted(groups.items())
    ]


def catalog(user):
    from .assistant_read import catalogue

    actions = available_tools(user)
    return {"models": catalogue(), "actions": actions, "workflows": workflow_catalog(user, actions)}


def _catalog(user, _payload):
    return catalog(user)


register_tool(AssistantTool("assistant.capabilities", "Доступные возможности", "Показывает фактически зарегистрированные действия.", {"type": "object"}, True, False, False, lambda user, payload: {"tools": available_tools(user)}))
register_tool(AssistantTool("assistant.catalog", "Энциклопедия возможностей PSADMIN", "Показывает автоматически собранные бизнес-модели и зарегистрированные реальные действия.", {"type": "object"}, True, False, True, _catalog))
register_tool(AssistantTool("read", "Прочитать бизнес-данные", "Выполняет проверенный read-only ORM план по разрешённым моделям, полям, фильтрам, группировкам и агрегатам. Не принимает SQL.", read_input_schema(), True, False, True, _read_data))
