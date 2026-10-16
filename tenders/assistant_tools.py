from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _navigation_index():
    """Build the capability graph once from the same model and action metadata."""
    from .assistant_read import catalogue

    _discover_app_actions()
    compact_catalogue = catalogue(compact=True)
    read = {}
    for model in catalogue():
        app_label, model_name = model["id"].split(".", 1)
        config = apps.get_app_config(app_label)
        section = read.setdefault(app_label, {
            "id": app_label,
            "label": str(config.verbose_name),
            "models": {},
        })
        section["models"][model_name] = model

    actions = {}
    for tool in _TOOLS.values():
        if tool.identifier in {"read", "assistant.capabilities", "assistant.catalog", "assistant.navigate"}:
            continue
        source = tool.source or f"{tool.executor.__module__}.{tool.executor.__qualname__}"
        app_label = source.split(".", 1)[0]
        try:
            label = str(apps.get_app_config(app_label).verbose_name)
        except LookupError:
            label = app_label
        actions.setdefault(app_label, {"id": app_label, "label": label, "tools": []})["tools"].append({
            "id": tool.identifier,
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "read_only": tool.read_only,
            "requires_confirmation": tool.requires_confirmation,
            "requires_superuser": tool.requires_superuser,
        })
    for section in actions.values():
        section["tools"].sort(key=lambda item: item["name"])
    return {"read": read, "action": actions, "filter_contract": compact_catalogue["filter_contract"]}


def _visible_tools(items, user):
    return [item for item in items if not item["requires_superuser"] or user.is_superuser]


def capability_tree(user):
    """Return the compact, complete route map used for one LLM route choice.

    Detailed field and argument contracts stay behind their leaf nodes.  The
    tree itself is assembled from the same runtime metadata as ``navigation``;
    it is not a second, hand-maintained catalogue.
    """
    graph = _navigation_index()
    read_children = []
    for section in graph["read"].values():
        read_children.append({
            "id": section["id"], "label": section["label"],
            "children": [
                {"id": model["id"].split(".", 1)[1], "label": model["verbose_name"]}
                for model in section["models"].values()
            ],
        })
    action_children = []
    for section in graph["action"].values():
        tools = _visible_tools(section["tools"], user)
        if not tools:
            continue
        action_children.append({
            "id": section["id"], "label": section["label"],
            "children": [
                {"id": tool["id"], "label": tool["name"], "description": tool["description"]}
                for tool in tools
            ],
        })
    return {"children": [
        {"id": "read", "label": "Чтение и анализ данных", "children": read_children},
        {"id": "action", "label": "Действия приложений", "children": action_children},
        {"id": "compute", "label": "Вычисления", "operations": ["count", "sum", "avg", "min", "max", "subtract", "divide", "percent_change"]},
    ]}


def navigation(user, payload):
    """Return only one graph node's children; never the whole catalogue."""
    path = payload.get("path", [])
    if not isinstance(path, list) or not all(isinstance(item, str) and item for item in path):
        raise AssistantToolError("Путь навигации должен быть списком идентификаторов")
    graph = _navigation_index()
    if not path:
        return {"path": [], "children": [
            {"id": "read", "label": "Чтение и анализ данных", "description": "Разрешённые бизнес-данные, фильтры, связи и агрегаты"},
            {"id": "action", "label": "Действия приложений", "description": "Существующие функции сайта через их штатные исполнители"},
            {"id": "compute", "label": "Вычисления", "description": "Безопасная арифметика только по уже проверенным результатам"},
        ]}
    if path == ["compute"]:
        return {"path": path, "children": [{"id": "compute", "label": "Вычисления", "operations": ["count", "sum", "avg", "min", "max", "subtract", "divide", "percent_change"]}]}
    if path[0] == "read":
        if len(path) == 1:
            return {"path": path, "children": [
                {"id": section["id"], "label": section["label"], "kind": "application"}
                for section in graph["read"].values()
            ]}
        section = graph["read"].get(path[1])
        if section is None:
            raise AssistantToolError("Раздел чтения не найден")
        if len(path) == 2:
            return {"path": path, "children": [
                {"id": model["id"].split(".", 1)[1], "label": model["verbose_name"], "kind": "model"}
                for model in section["models"].values()
            ]}
        model = section["models"].get(path[2])
        if model is None or len(path) != 3:
            raise AssistantToolError("Сущность чтения не найдена")
        return {
            "path": path,
            "entity": model["id"],
            "description": model.get("description", ""),
            "views": model.get("views", []),
            "filter_contract": graph["filter_contract"],
            "children": [{"id": field["name"], **field} for field in model["fields"]],
        }
    if path[0] == "action":
        if len(path) == 1:
            return {"path": path, "children": [
                {"id": section["id"], "label": section["label"], "kind": "application"}
                for section in graph["action"].values()
                if _visible_tools(section["tools"], user)
            ]}
        section = graph["action"].get(path[1])
        if section is None or len(path) not in {2, 3}:
            raise AssistantToolError("Раздел действий не найден")
        tools = _visible_tools(section["tools"], user)
        if len(path) == 2:
            return {"path": path, "children": tools}
        tool = next((item for item in tools if item["id"] == path[2]), None)
        if tool is None:
            raise AssistantToolError("Действие не найдено")
        return {"path": path, "tool": tool}
    raise AssistantToolError("Узел навигации не найден")


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
register_tool(AssistantTool("assistant.navigate", "Навигация по возможностям PSADMIN", "Открывает один узел графа READ/ACTION; не выдаёт весь каталог сразу.", {"path": "string[]?"}, True, False, True, navigation))
register_tool(AssistantTool("read", "Прочитать бизнес-данные", "Выполняет проверенный read-only ORM план по разрешённым моделям, полям, фильтрам, группировкам и агрегатам. Не принимает SQL.", read_input_schema(), True, False, True, _read_data))
