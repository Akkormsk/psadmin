from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any, Callable

from django.db.models import Q

from .models import ProcessDefinition, ProviderCalculatorBinding
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
    domain: str = "tenders"
    operation: str = "read"
    output_schema: dict[str, Any] | None = None


_TOOLS: dict[str, AssistantTool] = {}
_DISCOVERED = False


def register_tool(tool: AssistantTool) -> AssistantTool:
    if tool.identifier in _TOOLS:
        raise RuntimeError(f"Duplicate assistant tool: {tool.identifier}")
    _TOOLS[tool.identifier] = tool
    return tool


def discover_capabilities():
    """Load optional ``assistant_capabilities`` modules from installed apps once.

    A future Django app only needs to import/register its capabilities from that
    module; the agent core and UI protocol remain unchanged.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    from django.apps import apps

    _DISCOVERED = True
    for config in apps.get_app_configs():
        try:
            import_module(f"{config.name}.assistant_capabilities")
        except ModuleNotFoundError as exc:
            if exc.name != f"{config.name}.assistant_capabilities":
                raise


def available_tools(user) -> list[dict[str, Any]]:
    discover_capabilities()
    return [
        {
            "id": tool.identifier,
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "read_only": tool.read_only,
            "domain": tool.domain,
            "operation": tool.operation,
            "output_schema": tool.output_schema or {},
            "requires_confirmation": tool.requires_confirmation,
            "available": not tool.requires_superuser or user.is_superuser,
            "ui": tool.ui,
        }
        for tool in _TOOLS.values()
    ]


def execute_tool(identifier: str, user, payload: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    discover_capabilities()
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


def _data_payload(payload, allowed):
    if set(payload) - allowed:
        raise AssistantToolError("Некорректные параметры запроса")


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _universal_query(user, payload):
    from .agent_query import QueryPlanError, execute_plan

    try:
        return execute_plan(user, payload.get("plan"))
    except QueryPlanError as exc:
        raise AssistantToolError(str(exc)) from exc


def _list_provider_calculators(_user, payload):
    _data_payload(payload, {"query"})
    query = str(payload.get("query", "")).strip()
    bindings = ProviderCalculatorBinding.objects.select_related("link__counterparty", "link__stage", "knowledge_version").filter(is_active=True, link__is_active=True, link__counterparty__is_active=True)
    if query:
        bindings = bindings.filter(Q(link__counterparty__name__icontains=query) | Q(link__stage__name__icontains=query))
    return {"calculators": [{"id": item.pk, "counterparty_id": item.link.counterparty_id, "counterparty": item.link.counterparty.name, "stage": item.link.stage.name, "schema": get_provider_calculator_schema(item)} for item in bindings.order_by("link__counterparty__name")[:50]]}


def _calculate_general(_user, payload):
    from calculator.models import CalculatorSettings, Estimate, PriceItem
    from calculator.services import calculate_sheet_estimate

    _data_payload(payload, {"calculator_type", "quantity", "work_hours", "items"})
    calculator_type = payload.get("calculator_type", Estimate.TYPE_SHEET)
    if calculator_type not in {Estimate.TYPE_SHEET, Estimate.TYPE_WIDE}:
        raise AssistantToolError("Неизвестный тип калькулятора")
    quantity = _decimal(payload.get("quantity"))
    hours = _decimal(payload.get("work_hours", 0))
    rows = payload.get("items")
    if quantity is None or quantity <= 0 or hours is None or hours < 0 or not isinstance(rows, list) or len(rows) > 100:
        raise AssistantToolError("Некорректные параметры расчёта")
    item_ids = [row.get("item_id") for row in rows if isinstance(row, dict)]
    if len(item_ids) != len(rows) or not all(isinstance(item_id, int) for item_id in item_ids):
        raise AssistantToolError("Нужны идентификаторы позиций калькулятора")
    items = PriceItem.objects.filter(pk__in=item_ids, is_active=True).select_related("base_item").in_bulk()
    if len(items) != len(set(item_ids)):
        raise AssistantToolError("Одна или несколько позиций калькулятора недоступны")
    lines = []
    for row in rows:
        item = items[row["item_id"]]
        line_quantity = _decimal(row.get("quantity"))
        if line_quantity is None or line_quantity <= 0:
            raise AssistantToolError("Некорректное количество позиции")
        lines.append({"category": item.category, "quantity": line_quantity, "unit_price": item.effective_unit_price})
    settings = CalculatorSettings.objects.order_by("pk").first()
    if not settings:
        raise AssistantToolError("Не настроен общий калькулятор")
    result = calculate_sheet_estimate(lines, quantity, hours, settings, calculator_type)
    return {"calculator_type": calculator_type, "inputs": {"quantity": str(quantity), "work_hours": str(hours), "item_ids": item_ids}, "result": {key: str(value) for key, value in result.items()}, "evidence": [{"entity": "calculator_price_item", "id": item_id} for item_id in item_ids]}


def _mutation_preview_only(_user, _payload):
    raise AssistantToolConfirmationRequired("Изменение доступно только как предпросмотр в беседе")


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


register_tool(AssistantTool("assistant.capabilities", "Доступные возможности", "Показывает фактически зарегистрированные действия.", {"type": "object"}, True, False, False, lambda user, payload: {"tools": available_tools(user)}))
register_tool(AssistantTool("data.query", "Универсальный запрос данных", "Выполняет проверенный декларативный запрос к разрешённым сущностям, полям, фильтрам, сортировке, группировке и агрегациям. Возвращает источники. Не принимает SQL.", {"plan": {"entity": "catalogue id", "filters": "[{field,op,value}]?", "search": "string?", "fields": "string[]?", "order_by": "field?", "page": "integer?", "page_size": "integer?", "group_by": "string[]?", "aggregates": "[{op,field}]?"}}, True, False, False, _universal_query, domain="platform", output_schema={"total_count": "integer", "returned_count": "integer", "aggregates": "object", "records": "array", "evidence": "array"}))
register_tool(AssistantTool("provider.calculator_list", "Калькуляторы контрагентов", "Находит реальные активные калькуляторы контрагентов и их схемы параметров.", {"query": "string?"}, True, False, False, _list_provider_calculators, domain="providers", operation="calculate"))
register_tool(AssistantTool("calculator.quote", "Общий калькулятор", "Выполняет детерминированный расчёт PSADMIN по активным позициям прайса. Цены передаются только из справочника по ID.", {"calculator_type": "sheet|wide?", "quantity": "number", "work_hours": "number?", "items": "[{item_id: integer, quantity: number}]"}, True, False, False, _calculate_general, domain="calculator", operation="calculate"))
register_tool(AssistantTool("data.mutate", "Изменить запись", "Готовит предпросмотр универсального создания, изменения или удаления обычной записи. Исполнение возможно только кнопкой подтверждения в текущей беседе.", {"action": "create|update|delete", "model": "app.Model", "object_id": "integer|null", "values": "object"}, False, True, True, _mutation_preview_only, domain="platform", operation="mutate"))
register_tool(AssistantTool("process.list", "Список этапов", "Показывает доступные этапы производства.", {"type": "object"}, True, False, False, _list_stages))
register_tool(AssistantTool("provider.create_draft", "Создать черновик контрагента", "Создаёт черновик и не подтверждает знания автоматически.", {"name": "string", "stage_ids": "integer[]", "source_text": "string"}, False, True, True, _create_provider_draft, {"kind": "provider_upload", "fields": {"name": "string", "stage_id": "integer"}, "options": {"stages": {"tool": "process.list", "field": "stages"}}}))
register_tool(AssistantTool("provider.calculator_schema", "Входы калькулятора", "Показывает подтверждённую схему калькулятора.", {"binding_id": "integer"}, True, False, False, _calculator_schema))
register_tool(AssistantTool("provider.calculate", "Рассчитать у контрагента", "Запускает детерминированный калькулятор с заданными входами.", {"binding_id": "integer", "spec": "object"}, False, False, False, _calculate_provider))
