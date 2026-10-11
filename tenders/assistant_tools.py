from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any, Callable

from django.db.models import Q

from .models import Counterparty, CounterpartyKnowledgeVersion, OrderEstimate, ProcessDefinition, ProviderCalculatorBinding, TenderEstimate
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


def _find_counterparty(_user, payload):
    query = str(payload.get("query", "")).strip()
    matches = Counterparty.objects.filter(is_active=True)
    if query:
        matches = matches.filter(name__icontains=query)
    return {"counterparties": [{"id": item.pk, "name": item.name} for item in matches.order_by("name")[:20]]}


def _data_payload(payload, allowed):
    if set(payload) - allowed:
        raise AssistantToolError("Некорректные параметры запроса")


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _estimate_data(user, payload, model, number_field, relation_field, relation_name_field):
    _data_payload(payload, {"query", "status"})
    query = str(payload.get("query", "")).strip()
    status = str(payload.get("status", "")).strip()
    records = model.objects.select_related(relation_field)
    if not user.is_superuser:
        records = records.filter(owner=user)
    if query:
        records = records.filter(Q(name__icontains=query) | Q(**{f"{number_field}__icontains": query}) | Q(**{f"{relation_field}__{relation_name_field}__icontains": query}))
    if status:
        records = records.filter(**{f"{relation_field}__status": status})
    items = list(records.order_by("-updated_at", "-pk")[:20])
    total = sum((_decimal((item.summary_snapshot or {}).get("our_price")) or Decimal("0") for item in items), Decimal("0"))
    return {
        "count": len(items),
        "totals": {"our_price": f"{total:.2f}"},
        "records": [
            {
                "id": item.pk,
                "number": getattr(item, number_field),
                "name": item.name,
                "status": item.display_status(),
                f"{relation_field}_id": getattr(item, f"{relation_field}_id"),
            }
            for item in items
        ],
    }


def _data_tenders(user, payload):
    return _estimate_data(user, payload, TenderEstimate, "tender_number", "tender", "title")


def _data_orders(user, payload):
    return _estimate_data(user, payload, OrderEstimate, "order_number", "order", "name")


def _data_counterparties(_user, payload):
    _data_payload(payload, {"query"})
    query = str(payload.get("query", "")).strip()
    records = Counterparty.objects.filter(is_active=True)
    if query:
        records = records.filter(name__icontains=query)
    return {"count": records.count(), "records": [{"id": item.pk, "name": item.name} for item in records.order_by("name")[:20]]}


def _data_cash(user, payload):
    from cash.models import CashTransaction

    _data_payload(payload, {"date", "account", "direction", "query"})
    filters = {key: str(payload[key]).strip() for key in ("account", "direction") if payload.get(key)}
    records = CashTransaction.objects.filter(**filters)
    if payload.get("date"):
        records = records.filter(operation_date=str(payload["date"]))
    if payload.get("query"):
        records = records.filter(reason__icontains=str(payload["query"]).strip())
    items = list(records.order_by("-operation_date", "-pk")[:50])
    income = sum((item.amount for item in items if item.direction == CashTransaction.DIRECTION_INCOME), Decimal("0"))
    expense = sum((item.amount for item in items if item.direction == CashTransaction.DIRECTION_EXPENSE), Decimal("0"))
    return {"count": len(items), "totals": {"income": f"{income:.2f}", "expense": f"{expense:.2f}"}, "records": [{"id": item.pk, "date": item.operation_date.isoformat(), "account": item.account, "direction": item.direction, "amount": f"{item.amount:.2f}", "reason": item.reason} for item in items]}


def _data_payroll(user, payload):
    from finance.models import PayrollLine

    _data_payload(payload, {"period"})
    records = PayrollLine.objects.select_related("period").all()
    if not user.is_superuser:
        records = records.filter(manager=user)
    if payload.get("period"):
        records = records.filter(period__code=str(payload["period"]))
    items = list(records.order_by("-period__code", "pk")[:20])
    return {"count": len(items), "records": [{"id": item.pk, "period": item.period.code, "name": item.name, "work_shifts": item.work_shifts, "advance": f"{item.advance:.2f}", "order_profit": f"{item.order_profit:.2f}", "kpi_bonus": f"{item.kpi_bonus:.2f}"} for item in items]}


def _universal_query(user, payload):
    from .agent_query import QueryPlanError, execute_plan

    try:
        return execute_plan(user, payload.get("plan"))
    except QueryPlanError as exc:
        raise AssistantToolError(str(exc)) from exc


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
register_tool(AssistantTool("provider.find", "Найти контрагента", "Ищет активных контрагентов.", {"query": "string"}, True, False, False, _find_counterparty))
register_tool(AssistantTool("data.tenders", "Данные тендерных расчётов", "Ищет доступные тендерные расчёты и суммирует известную цену из сводки. Возвращает ID записей.", {"query": "string?", "status": "string?"}, True, False, False, _data_tenders))
register_tool(AssistantTool("data.orders", "Данные заказов", "Ищет доступные расчёты заказов и суммирует известную цену из сводки. Возвращает ID записей.", {"query": "string?", "status": "string?"}, True, False, False, _data_orders))
register_tool(AssistantTool("data.counterparties", "Данные контрагентов", "Ищет активных контрагентов по названию и возвращает ID записей.", {"query": "string?"}, True, False, False, _data_counterparties))
register_tool(AssistantTool("data.cash", "Данные кассы", "Показывает операции кассы с ограниченными фильтрами и итогами прихода/расхода.", {"date": "YYYY-MM-DD?", "account": "cash|card?", "direction": "income|expense?", "query": "string?"}, True, False, False, _data_cash))
register_tool(AssistantTool("data.payroll", "Данные зарплаты", "Показывает доступные строки зарплаты за период; сотрудник видит только свои.", {"period": "YYYY-MM?"}, True, False, False, _data_payroll))
register_tool(AssistantTool("data.query", "Универсальный запрос данных", "Выполняет проверенный декларативный запрос к разрешённым сущностям, полям, фильтрам, сортировке и агрегациям. Возвращает источники. Не принимает SQL.", {"plan": {"entity": "catalogue id", "filters": "[{field,op,value}]?", "search": "string?", "fields": "string[]?", "order_by": "field?", "page": "integer?", "page_size": "integer?", "aggregates": "[{op,field}]?"}}, True, False, False, _universal_query, domain="platform", output_schema={"total_count": "integer", "returned_count": "integer", "aggregates": "object", "records": "array", "evidence": "array"}))
register_tool(AssistantTool("process.list", "Список этапов", "Показывает доступные этапы производства.", {"type": "object"}, True, False, False, _list_stages))
register_tool(AssistantTool("provider.create_draft", "Создать черновик контрагента", "Создаёт черновик и не подтверждает знания автоматически.", {"name": "string", "stage_ids": "integer[]", "source_text": "string"}, False, True, True, _create_provider_draft, {"kind": "provider_upload", "fields": {"name": "string", "stage_id": "integer"}, "options": {"stages": {"tool": "process.list", "field": "stages"}}}))
register_tool(AssistantTool("provider.calculator_schema", "Входы калькулятора", "Показывает подтверждённую схему калькулятора.", {"binding_id": "integer"}, True, False, False, _calculator_schema))
register_tool(AssistantTool("provider.calculate", "Рассчитать у контрагента", "Запускает детерминированный калькулятор с заданными входами.", {"binding_id": "integer", "spec": "object"}, False, False, False, _calculate_provider))
