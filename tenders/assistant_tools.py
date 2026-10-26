from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import re
import json
from importlib import import_module
from django.utils.module_loading import import_string
from typing import Any, Callable

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ValidationError


from .assistant_read import input_schema as read_input_schema



class AssistantToolError(Exception):
    pass


class AssistantToolConfirmationRequired(AssistantToolError):
    pass


class AssistantReferenceError(AssistantToolError):
    """An object reference needs resolution, not a claim that search failed."""
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
    areas: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssistantResource:
    """A user-facing read resource backed by the universal READ executor."""

    identifier: str
    name: str
    description: str
    entity: str
    areas: tuple[str, ...]
    requires_superuser: bool = True


_TOOLS: dict[str, AssistantTool] = {}
_RESOURCES: dict[str, AssistantResource] = {}
_DISCOVERED = False
_MODEL_HIDDEN_TOOLS = {"assistant.capabilities", "assistant.catalog"}


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


def register_resource(resource: AssistantResource) -> AssistantResource:
    if resource.identifier in _RESOURCES:
        raise RuntimeError(f"Duplicate assistant resource: {resource.identifier}")
    if not resource.areas:
        raise RuntimeError("Assistant resource must belong to a business area")
    _RESOURCES[resource.identifier] = resource
    return resource


def _tool_areas(tool: AssistantTool) -> tuple[str, ...]:
    if tool.areas:
        return tool.areas
    if tool.source:
        app_label = tool.source.split(".", 1)[0]
        try:
            apps.get_app_config(app_label)
            return (app_label,)
        except LookupError:
            pass
    return (tool.identifier.split(".", 1)[0],) if "." in tool.identifier else ()


def _area_name(identifier: str) -> str:
    try:
        return str(apps.get_app_config(identifier).verbose_name)
    except LookupError:
        return identifier.replace("_", " ").capitalize()


def _declared_business_areas() -> set[str]:
    """Return the site's own user-facing Django applications.

    A business application already declares its human name through
    ``AppConfig.verbose_name``. That declaration is the right place to make
    a new application visible to the assistant: it avoids a second central
    list, while framework apps and the internal ``core`` app (which have no
    explicit business name) stay out of the user-facing library.
    """
    return {
        config.label
        for config in apps.get_app_configs()
        if "verbose_name" in config.__class__.__dict__
        and not config.name.startswith("django.")
    }


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
            "areas": list(_tool_areas(tool)),
        }
        for tool in _TOOLS.values()
    ]


def available_areas(user) -> list[dict[str, Any]]:
    """Return every user-facing business application, even if it is empty.

    Capabilities enrich a section; they do not create it. This makes the
    first model choice meaningful during a vertical-slice rollout: an empty
    section is still a real alternative, rather than making the only populated
    section a guaranteed choice.
    """
    _discover_app_actions()
    identifiers = _declared_business_areas() | {
        area for tool in _TOOLS.values() for area in _tool_areas(tool)
    } | {
        area for resource in _RESOURCES.values() for area in resource.areas
    }
    return [
        {
            "id": identifier,
            "name": _area_name(identifier),
            "description": f"Данные и операции раздела «{_area_name(identifier)}».",
        }
        for identifier in sorted(identifiers)
        if identifier not in {"assistant"}
    ]


def available_resources(user, area_id: str | None = None) -> list[dict[str, Any]]:
    _discover_app_actions()
    return [
        {
            "id": resource.identifier,
            "name": resource.name,
            "description": resource.description,
            "entity": resource.entity,
            "areas": list(resource.areas),
        }
        for resource in _RESOURCES.values()
        if (not resource.requires_superuser or user.is_superuser)
        and (area_id is None or area_id in resource.areas)
    ]


def available_capabilities(user, area_id: str) -> list[dict[str, Any]]:
    """Return compact, user-facing entries from the existing registry.

    This is a view, not a second registry: a selected entry is resolved back
    to the same resource or action before its detailed Function schema opens.
    """
    resources = [
        {
            "id": f"read:{resource['id']}", "kind": "read", "name": resource["name"],
            "description": resource["description"], "requires_confirmation": False,
            "resource_id": resource["id"],
        }
        for resource in available_resources(user, area_id)
    ]
    actions = [
        {
            "id": tool["id"], "kind": "action", "name": tool["name"],
            "description": tool["description"],
            "requires_confirmation": tool["requires_confirmation"],
        }
        for tool in available_tools(user)
        if tool["available"] and tool["id"] not in _MODEL_HIDDEN_TOOLS | {"read"}
        and area_id in tool["areas"]
    ]
    return sorted([*resources, *actions], key=lambda item: (item["name"], item["id"]))


def _preview_field(tool, name):
    model_label = (tool.get("ui") or {}).get("model")
    if not model_label:
        return None
    try:
        field = apps.get_model(model_label)._meta.get_field(name)
        return field if getattr(field, "concrete", False) else None
    except (LookupError, ValueError, FieldDoesNotExist):
        return None


def _reference_model(reference, payload):
    model_label = reference["model"]
    if isinstance(model_label, list):
        model_label = next((item["model"] for item in model_label
                            if all(payload.get(key) == value for key, value in item["when"].items())), None)
        if model_label is None:
            raise AssistantReferenceError("Сначала уточните тип выбранного объекта")
    return apps.get_model(model_label)


def _reference_values(model, declaration, value):
    values = value if isinstance(value, list) else [value]
    if declaration.get("type", "").rstrip("?") in {"integer", "integer[]"} and any(type(item) is not int for item in values):
        raise AssistantReferenceError("Ссылка на запись должна содержать внутренний целочисленный ID")
    try:
        return [model._meta.pk.to_python(item) for item in values]
    except (ValidationError, ValueError, TypeError) as exc:
        raise AssistantReferenceError("Некорректная ссылка на запись") from exc


def _preview_label(name: str, declaration: Any, field=None, payload=None) -> str:
    if isinstance(declaration, dict) and declaration.get("label"):
        return str(declaration["label"])
    if field is not None:
        return str(field.verbose_name)
    reference = declaration.get("reference") if isinstance(declaration, dict) else None
    if isinstance(reference, dict):
        model = _reference_model(reference, payload or {})
        plural = str(declaration.get("type", "")).endswith("[]")
        return str(model._meta.verbose_name_plural if plural else model._meta.verbose_name)
    return name.replace("_", " ").capitalize()


def _preview_value(value: Any, field=None, choices=()) -> str:
    for raw, label in choices:
        if value == raw:
            return str(label)
    if field is not None:
        for raw, label in field.flatchoices:
            if value == raw:
                return str(label)
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            year, month, day = value.split("-")
            return f"{day}.{month}.{year}"
        return value
    if isinstance(value, list):
        return ", ".join(_preview_value(item) for item in value) if value else "Не выбрано"
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_preview_value(item)}" for key, item in value.items()) or "Не указано"
    return str(value)


def _reference_preview_items(declaration: Any, value: Any, payload=None) -> list[dict[str, str]] | None:
    if not isinstance(declaration, dict) or not isinstance(declaration.get("reference"), dict):
        return None
    reference = declaration["reference"]
    try:
        model = _reference_model(reference, payload or {})
    except (LookupError, ValueError):
        return None
    values = _reference_values(model, declaration, value)
    objects = model._default_manager.in_bulk(values)
    label_field = reference.get("label", "name")
    items = []
    for identifier in values:
        instance = objects.get(identifier)
        if instance is None:
            items.append({"text": "Объект не найден"})
            continue
        label_fields = label_field if isinstance(label_field, list) else [label_field]
        parts = []
        for name in label_fields:
            value = getattr(instance, name, None)
            if value is None or value == "":
                continue
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist:
                field = None
            parts.append(_preview_value(value, field))
        label = " — ".join(parts) or str(instance)
        items.append({"text": str(label)})
    return items


def _preview_items(declaration: Any, value: Any, payload=None, field=None) -> list[dict[str, str]] | None:
    reference_items = _reference_preview_items(declaration, value, payload)
    if reference_items is not None:
        return reference_items
    if isinstance(value, list):
        choices = declaration.get("choices", ()) if isinstance(declaration, dict) else ()
        return [{"text": _preview_value(item, field, choices)} for item in value]
    return None


def confirmation_arguments(tool: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Present validated action arguments for a human confirmation card."""
    arguments = []
    for name, declaration in tool["input_schema"].items():
        if name not in payload or payload[name] in (None, "", [], {}):
            continue
        field = _preview_field(tool, name)
        items = _preview_items(declaration, payload[name], payload, field)
        label = _preview_label(name, declaration, field, payload)
        choices = declaration.get("choices", ()) if isinstance(declaration, dict) else ()
        arguments.append(
            {"label": label, "items": items}
            if items is not None else {"label": label, "value": _preview_value(payload[name], field, choices)}
        )
    return arguments


def _plan_references(user, identifier):
    tool = _TOOLS.get(identifier)
    if tool is None:
        return []
    resources = available_resources(user)
    required = required_input_names(identifier)
    references = []
    for name, declaration in tool.input_schema.items():
        reference = declaration.get("reference") if isinstance(declaration, dict) else None
        if not isinstance(reference, dict):
            continue
        model = reference["model"]
        models = {model} if isinstance(model, str) else {item["model"] for item in model}
        references.append({"parameter": name, "model": model, "required": name in required,
                           "read": ["read:" + r["id"] for r in resources if r["entity"] in models]})
    return references


def available_capability_map(user) -> list[dict[str, Any]]:
    """Compact discovery view of the existing registry, grouped by area."""
    return [
        {
            "id": area["id"],
            "name": area["name"],
            "description": area["description"],
            "capabilities": [
                {
                    **{
                        key: capability[key]
                        for key in ("id", "kind", "name", "description", "requires_confirmation")
                    },
                    **({"references": references} if (references := _plan_references(user, capability["id"])) else {}),
                }
                for capability in available_capabilities(user, area["id"])
            ],
        }
        for area in available_areas(user)
    ]


def capability_for_user(user, capability_id):
    for area in available_areas(user):
        capability = next((item for item in available_capabilities(user, area["id"])
                           if item["id"] == capability_id), None)
        if capability is not None:
            return area, capability
    return None, None


def capability_map_text(capability_map: list[dict[str, Any]]) -> str:
    """Render the discovery view without repeating JSON keys for every item."""
    sections = []
    for area in capability_map:
        capabilities = "\n".join(
            f"- {item['id']} | {item['kind']} | {item['name']} — {item['description']}"
            + (" | confirmation required" if item["requires_confirmation"] else "")
            + (" | existing references: " + json.dumps(item["references"], ensure_ascii=False, separators=(",", ":"))
               if item.get("references") else "")
            for item in area["capabilities"]
        )
        sections.append(
            f"[{area['id']}] {area['name']} — {area['description']}"
            + (f"\n{capabilities}" if capabilities else "\n- no available capabilities")
        )
    common = "Все READ поддерживают фильтры, сортировку, группировку и агрегаты count/sum/avg/min/max по разрешённым полям."
    return common + "\n" + "\n".join(sections)


def execute_tool(identifier: str, user, payload: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    _discover_app_actions()
    tool = _TOOLS.get(identifier)
    if not tool:
        raise AssistantToolError("Неизвестное действие ассистента")
    if tool.requires_superuser and not user.is_superuser:
        raise AssistantToolError("Недостаточно прав для этого действия")
    validate_tool_call(identifier, user, payload)
    if tool.requires_confirmation and not confirmed:
        raise AssistantToolConfirmationRequired("Действие требует явного подтверждения")
    # OpenAI strict schemas encode optional values as nullable.  The business
    # executor must see the same shape as it would when an optional argument
    # was simply omitted, rather than every executor having to special-case
    # JSON null.
    payload = {
        key: value for key, value in payload.items()
        if value is not None or key in required_input_names(identifier)
    }
    return tool.executor(user, payload)


def validate_tool_call(identifier: str, user, payload: dict[str, Any]) -> None:
    """Validate a registered call without performing its side effect."""
    _discover_app_actions()
    tool = _TOOLS.get(identifier)
    if not tool:
        raise AssistantToolError("Неизвестное действие ассистента")
    if tool.requires_superuser and not user.is_superuser:
        raise AssistantToolError("Недостаточно прав для этого действия")
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
        declared_type = declared.get("type") if isinstance(declared, dict) else declared
        if isinstance(declared, dict) and declared_type == "object" and not isinstance(value, dict):
            raise AssistantToolError(f"Поле {name} должно быть объектом")
        if not isinstance(declared_type, str):
            continue
        kind = declared_type.rstrip("?")
        if kind == "boolean" and not isinstance(value, bool):
            raise AssistantToolError(f"Поле {name} должно иметь значение true или false")
        if kind.endswith("[]") and not isinstance(value, list):
            raise AssistantToolError(f"Поле {name} должно быть списком")
        if kind in {"object"} and not isinstance(value, dict):
            raise AssistantToolError(f"Поле {name} должно быть объектом")
        reference = declared.get("reference") if isinstance(declared, dict) else None
        if isinstance(reference, dict):
            try:
                model = _reference_model(reference, payload)
            except (KeyError, LookupError, ValueError) as exc:
                raise AssistantToolError("Некорректное описание ссылки на запись") from exc
            values = _reference_values(model, declared, value)
            if model._default_manager.filter(pk__in=values).count() != len(set(values)):
                raise AssistantReferenceError(
                    "Выбранная запись не найдена. Сначала найдите её через READ по номеру или названию "
                    "и используйте внутренний ID найденной записи."
                )


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _openai_type(declared):
    """Translate the registry contract recursively without inventing a second one."""
    optional = isinstance(declared, str) and declared.endswith("?")
    value = declared[:-1] if optional else declared
    if isinstance(value, list):
        return {"type": "string", "enum": value}, optional, True
    if isinstance(value, dict):
        if "any_of" in value:
            variants, compatible = [], True
            for nested in value["any_of"]:
                child, _optional, child_compatible = _openai_type(nested)
                variants.append(child)
                compatible = compatible and child_compatible
            schema = {"anyOf": variants}
        else:
            kind = value.get("type")
            if kind is None:
                kind = "object"
                value = {"type": kind, "properties": value, "required": True}
            if kind == "object" and "properties" not in value and "additional_properties" not in value:
                schema, _ignored, compatible = _openai_type(kind)
            elif kind == "object":
                properties, required, compatible = {}, [], True
                for key, nested in value.get("properties", {}).items():
                    child, child_optional, child_compatible = _openai_type(nested)
                    properties[key] = _nullable(child) if child_optional else child
                    required.append(key)
                    compatible = compatible and child_compatible
                schema = {"type": "object", "properties": properties, "required": required,
                          "additionalProperties": bool(value.get("additional_properties", False))}
                compatible = compatible and not schema["additionalProperties"]
            elif kind == "array" and "item" in value:
                child, _ignored, compatible = _openai_type(value["item"])
                schema = {"type": "array", "items": child}
            elif isinstance(kind, str) and kind.endswith("[]"):
                child, _ignored, compatible = _openai_type(value.get("item", kind[:-2]))
                schema = {"type": "array", "items": child}
            else:
                schema, _ignored, compatible = _openai_type(kind or "object")
        for key in ("description", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "enum"):
            if key in value:
                schema[key] = value[key]
        return schema, bool(value.get("required") is False), compatible
    if not isinstance(value, str):
        return {"type": "string"}, optional, False
    if value.endswith("[]"):
        child, _ignored, compatible = _openai_type(value[:-2])
        return {"type": "array", "items": child}, optional, compatible
    if "|" in value:
        variants, compatible = [], True
        for part in value.split("|"):
            child, _ignored, child_compatible = _openai_type(part)
            variants.append(child)
            compatible = compatible and child_compatible
        return {"anyOf": variants}, optional, compatible
    primitive = {"integer", "number", "boolean", "string", "null"}
    if value in primitive:
        return {"type": value}, optional, True
    if value in {"object", "array"}:
        return {"type": value, "additionalProperties": False} if value == "object" else {"type": value, "items": {}}, optional, False
    return {"type": "string"}, optional, True


def _function_definition(identifier, description, input_schema):
    name = re.sub(r"[^A-Za-z0-9_-]", "_", identifier)
    properties, required, strict = {}, [], True
    for key, declared in input_schema.items():
        schema, optional, compatible = _openai_type(declared)
        if optional:
            schema = _nullable(schema)
        properties[key] = schema
        required.append(key)
        strict = strict and compatible
    function = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
    }
    if strict:
        function["strict"] = True
    return name, {"type": "function", "function": function}


def _resource_read_schema(resource):
    schema = deepcopy(read_input_schema(resource["entity"]))
    plan = schema["plan"]
    plan["properties"].pop("entity", None)
    return schema


def openai_functions(user, *, area_id=None, resource_id=None, capability_id=None, planned=True):
    """Expose one discovery layer or one selected capability at a time."""
    definitions, names = [], {}
    capability_ids = sorted({
        capability["id"]
        for area in available_areas(user)
        for capability in available_capabilities(user, area["id"])
    })

    name, definition = _function_definition(
        "assistant.plan",
        "Saves the task plan before business tools. Revise only the route within the same task; an independent request requires assistant.cancel(new_task=true) first. Each capability_id names a published capability, not an area.",
        {"plan": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "minLength": 1},
                "revision_reason": {"type": "string", "required": False, "description": "Reason for a route change within the same task; null initially. Argument corrections keep the plan; an independent request requires assistant.cancel(new_task=true)."},
                "steps": {
                    "type": "array", "minItems": 1,
                    "item": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "capability_id": {"type": "string", "enum": capability_ids},
                            "objective": {"type": "string", "minLength": 1},
                            "expected_result": {"type": "string", "minLength": 1},
                            "reference_sources": {"type": "array", "required": False,
                                "description": "Required for ACTION references, including optional ones you pass. read_id names a preceding READ step or saved source_id; changing it requires plan revision.",
                                "item": {"type": "object", "properties": {
                                    "parameter": "string", "read_id": "string"}, "additional_properties": False}},
                        },
                        "additional_properties": False,
                    },
                },
            },
            "additional_properties": False,
        }},
    )
    definitions.append(definition)
    names[name] = "assistant.plan"

    if not planned:
        name, definition = _function_definition(
            "assistant.chat",
            "Ответ на обычный разговор, не требующий бизнес-данных или действий. Не используй для бизнес-уточнений: сначала assistant.plan.",
            {"answer": {"type": "string", "minLength": 1}},
        )
        definitions.append(definition)
        names[name] = "assistant.chat"
        return definitions, names

    for identifier, description, schema in (
        ("assistant.ask", "Continues dialogue within the active task: responds to the user, asks for clarification when needed, and waits for the next message. Does not complete the task.",
         {"question": {"type": "string", "minLength": 1}}),
        ("assistant.cancel", "Closes the active task. Use new_task=true BEFORE answering or planning an independent request, including a self-contained question. Use false for whole-task cancellation. Argument/preview corrections before cancellation continue the same task; the Cancel button closes it in the backend.",
         {"reason": {"type": "string", "minLength": 1}, "answer": {"type": "string", "minLength": 1}, "new_task": "boolean"}),
    ):
        name, definition = _function_definition(identifier, description, schema)
        definitions.append(definition)
        names[name] = identifier

    name, definition = _function_definition(
        "assistant.finish",
        "Передаёт окончательный ответ пользователю только после выполнения актуального плана. Не используй для вопросов, пауз или промежуточных результатов.",
        {"answer": {"type": "string", "minLength": 1}},
    )
    definitions.append(definition)
    names[name] = "assistant.finish"
    names["assistant_select_area"] = "assistant.select_area"
    names["assistant_select_capability"] = "assistant.select_capability"

    capability_id = capability_id or (f"read:{resource_id}" if resource_id is not None else None)
    if capability_id is not None:
        area, capability = capability_for_user(user, capability_id)
        if area is None or capability is None:
            raise AssistantToolError("Недоступная возможность раздела")
        if capability["kind"] == "read":
            resource = next(item for item in available_resources(user, area["id"]) if item["id"] == capability["resource_id"])
            identifier = capability["id"]
            description = f"Читает «{resource['name']}». {resource['description']} Используй только разрешённые поля и значения из схемы."
            schema = _resource_read_schema(resource)
        else:
            tool = next(item for item in available_tools(user) if item["id"] == capability["id"] and item["available"])
            identifier = tool["id"]
            description = tool["description"]
            schema = deepcopy(tool["input_schema"])
        name, definition = _function_definition(
            identifier, description, schema,
        )
        if name in names:
            raise RuntimeError("OpenAI function name collision")
        definitions.append(definition)
        names[name] = identifier
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
