from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable

from django.apps import apps

from .models import Counterparty, CounterpartyKnowledgeVersion, ProcessDefinition, ProviderCalculatorBinding
from .provider_calculators import calculate_provider, get_provider_calculator_schema
from .provider_knowledge import create_knowledge_draft, create_provider


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
    _DISCOVERED = True
    for config in apps.get_app_configs():
        try:
            import_module(f"{config.name}.assistant_capabilities")
        except ModuleNotFoundError as exc:
            if exc.name != f"{config.name}.assistant_capabilities":
                raise


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
    return tool.executor(user, payload)


def required_input_names(identifier: str) -> list[str]:
    """Required top-level inputs declared by a real action contract."""
    _discover_app_actions()
    tool = _TOOLS.get(identifier)
    if tool is None:
        return []
    return [
        name for name, field_type in tool.input_schema.items()
        if isinstance(field_type, str) and not (name == "type" and field_type == "object") and not field_type.endswith("?")
    ]


def _find_counterparty(_user, payload):
    query = str(payload.get("query", "")).strip()
    matches = Counterparty.objects.filter(is_active=True)
    if query:
        matches = matches.filter(name__icontains=query)
    return {"counterparties": [{"id": item.pk, "name": item.name} for item in matches.order_by("name")[:20]]}


def _list_stages(_user, _payload):
    return {"stages": [{"id": item.pk, "name": item.name} for item in ProcessDefinition.objects.filter(is_active=True).order_by("name")]}


def _create_provider_draft(user, payload):
    name = str(payload.get("name", "")).strip()
    stage_ids = payload.get("stage_ids")
    source_text = str(payload.get("source_text", "")).strip()
    if not name or not isinstance(stage_ids, list) or not source_text:
        raise AssistantToolError("Нужны название, этапы и источник прайса")
    stages = list(ProcessDefinition.objects.filter(pk__in=stage_ids, is_active=True))
    if len(stages) != len(set(stage_ids)):
        raise AssistantToolError("Один или несколько этапов недоступны")
    provider, staging = create_provider(user, name, stages, extracted_text=source_text)
    version = create_knowledge_draft(provider, user, {"summary": source_text[:4000], "input_schema": [], "pricing": {}}, staging=staging)
    return {"provider_id": provider.pk, "knowledge_version_id": version.pk, "status": version.status}


def _calculator_schema(_user, payload):
    binding = ProviderCalculatorBinding.objects.select_related("link__counterparty", "link__stage", "knowledge_version").filter(pk=payload.get("binding_id"), is_active=True).first()
    if not binding:
        raise AssistantToolError("Калькулятор не найден")
    return {"binding_id": binding.pk, "schema": get_provider_calculator_schema(binding)}


def _calculate_provider(_user, payload):
    binding = ProviderCalculatorBinding.objects.select_related("link__counterparty", "link__stage", "knowledge_version").filter(pk=payload.get("binding_id"), is_active=True).first()
    if not binding:
        raise AssistantToolError("Калькулятор не найден")
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise AssistantToolError("Не заданы параметры расчёта")
    return {"binding_id": binding.pk, "quote": calculate_provider(binding, spec)}


def _read_data(user, payload):
    from .assistant_read import ReadPlanError, execute

    try:
        return execute(user, payload.get("plan"))
    except ReadPlanError as exc:
        raise AssistantToolError(str(exc)) from exc


def _catalog(user, _payload):
    from .assistant_catalog import catalog

    return catalog(user)


register_tool(AssistantTool("assistant.capabilities", "Доступные возможности", "Показывает фактически зарегистрированные действия.", {"type": "object"}, True, False, False, lambda user, payload: {"tools": available_tools(user)}))
register_tool(AssistantTool("assistant.catalog", "Энциклопедия возможностей PSADMIN", "Показывает автоматически собранные бизнес-модели и зарегистрированные реальные действия.", {"type": "object"}, True, False, True, _catalog))
register_tool(AssistantTool("read", "Прочитать бизнес-данные", "Выполняет проверенный read-only ORM план по разрешённым моделям, полям, фильтрам, группировкам и агрегатам. Не принимает SQL.", {"plan": "object"}, True, False, True, _read_data))
register_tool(AssistantTool("provider.find", "Найти контрагента", "Ищет активных контрагентов.", {"query": "string"}, True, False, False, _find_counterparty))
register_tool(AssistantTool("process.list", "Список этапов", "Показывает доступные этапы производства.", {"type": "object"}, True, False, False, _list_stages))
register_tool(AssistantTool("provider.create_draft", "Создать черновик контрагента", "Создаёт черновик и не подтверждает знания автоматически.", {"name": "string", "stage_ids": "integer[]", "source_text": "string"}, False, True, True, _create_provider_draft, {"kind": "provider_upload", "fields": {"name": "string", "stage_id": "integer"}, "options": {"stages": {"tool": "process.list", "field": "stages"}}}))
register_tool(AssistantTool("provider.calculator_schema", "Входы калькулятора", "Показывает подтверждённую схему калькулятора.", {"binding_id": "integer"}, True, False, False, _calculator_schema))
register_tool(AssistantTool("provider.calculate", "Рассчитать у контрагента", "Запускает детерминированный калькулятор с заданными входами.", {"binding_id": "integer", "spec": "object"}, False, False, False, _calculate_provider))
