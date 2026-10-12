"""Declarative, permission-aware data access for the assistant.

The model can produce a JSON query plan, but never chooses a Django model,
field path or ORM expression outside this catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from django.db.models import Avg, Count, Max, Min, Sum


class QueryPlanError(Exception):
    pass


@dataclass(frozen=True)
class Entity:
    identifier: str
    model_path: str
    fields: dict[str, str]
    searchable: tuple[str, ...]
    default_order: str
    visible_to: Callable[[Any, Any], Any]

    def model(self):
        from django.apps import apps

        app_label, model_name = self.model_path.split(".")
        return apps.get_model(app_label, model_name)


def _all(_user, queryset):
    return queryset


def _owned(user, queryset):
    return queryset if user.is_superuser else queryset.filter(owner=user)


def _tender_visible(user, queryset):
    if user.is_superuser:
        return queryset
    return queryset.filter(status="pushed", estimates__owner=user).distinct()


def _order_visible(user, queryset):
    if user.is_superuser:
        return queryset
    return queryset.filter(estimates__owner=user).distinct()


def _manager_or_admin(user, queryset):
    return queryset if user.is_superuser else queryset.filter(manager=user)


def _payroll_visible(user, queryset):
    return queryset if user.is_superuser else queryset.filter(manager=user)


ENTITIES: dict[str, Entity] = {
    "tender": Entity("tender", "tender_selection.Tender", {"id": "pk", "number": "purchase_number", "title": "title", "nmck": "max_price", "status": "status", "outcome": "outcome_status", "published_at": "published_at", "winner_price": "contract_price", "our_bid": "bid_price"}, ("purchase_number", "title", "object_info"), "-published_at", _tender_visible),
    "tender_estimate": Entity("tender_estimate", "tenders.TenderEstimate", {"id": "pk", "number": "tender_number", "name": "name", "created_at": "created_at", "updated_at": "updated_at", "reduction": "reduction_percent", "delivery": "russia_delivery"}, ("tender_number", "name"), "-updated_at", _owned),
    "order": Entity("order", "tenders.Order", {"id": "pk", "name": "name", "status": "status", "created_at": "created_at", "updated_at": "updated_at"}, ("name",), "-updated_at", _order_visible),
    "order_estimate": Entity("order_estimate", "tenders.OrderEstimate", {"id": "pk", "number": "order_number", "name": "name", "created_at": "created_at", "updated_at": "updated_at", "reduction": "reduction_percent", "delivery": "russia_delivery"}, ("order_number", "name"), "-updated_at", _owned),
    "counterparty": Entity("counterparty", "tenders.Counterparty", {"id": "pk", "name": "name", "active": "is_active", "created_at": "created_at"}, ("name", "notes"), "name", _all),
    "production_stage": Entity("production_stage", "tenders.ProcessDefinition", {"id": "pk", "name": "name", "role": "role", "active": "is_active"}, ("name",), "name", _all),
    "cash_transaction": Entity("cash_transaction", "cash.CashTransaction", {"id": "pk", "date": "operation_date", "account": "account", "direction": "direction", "amount": "amount", "reason": "reason"}, ("reason",), "-date", _all),
    "financial_period": Entity("financial_period", "finance.FinancialPeriod", {"id": "pk", "code": "code", "closed": "is_closed"}, ("code",), "-code", _all),
    "payroll": Entity("payroll", "finance.PayrollLine", {"id": "pk", "name": "name", "period": "period__code", "work_shifts": "work_shifts", "advance": "advance", "profit": "order_profit", "kpi_bonus": "kpi_bonus"}, ("name",), "-period", _payroll_visible),
    "profit_record": Entity("profit_record", "payroll.OrderRecord", {"id": "pk", "number": "order_number", "period": "accounting_period", "amount": "gross_profit", "type": "record_type", "created_at": "created_at"}, ("order_number",), "-created_at", _manager_or_admin),
    "calculator_estimate": Entity("calculator_estimate", "calculator.Estimate", {"id": "pk", "name": "name", "type": "calculator_type", "quantity": "product_quantity", "hours": "work_hours", "created_at": "created_at", "updated_at": "updated_at"}, ("name", "comment"), "-updated_at", _owned),
}

OPERATORS = {"eq", "ne", "contains", "gte", "gt", "lte", "lt", "in", "isnull"}
AGGREGATES = {"count": Count, "sum": Sum, "min": Min, "max": Max, "avg": Avg}
MAX_LIMIT = 100


def catalogue_for(user):
    return [{"id": item.identifier, "fields": sorted(item.fields), "searchable": list(item.searchable)} for item in ENTITIES.values()]


def _value(value):
    return str(value) if isinstance(value, Decimal) else value


def execute_plan(user, plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) - {"entity", "filters", "search", "fields", "order_by", "page", "page_size", "aggregates", "group_by"}:
        raise QueryPlanError("Некорректный план запроса")
    entity = ENTITIES.get(plan.get("entity"))
    if not entity:
        raise QueryPlanError("Недоступная сущность")
    requested = plan.get("fields") or ["id", *[key for key in entity.fields if key != "id"]]
    if not isinstance(requested, list) or not requested or any(field not in entity.fields for field in requested):
        raise QueryPlanError("Запрошены недоступные поля")
    qs = entity.visible_to(user, entity.model().objects.all())
    search = plan.get("search")
    if search not in (None, ""):
        if not isinstance(search, str) or not entity.searchable:
            raise QueryPlanError("Некорректный поиск")
        from django.db.models import Q
        condition = Q()
        for field in entity.searchable:
            condition |= Q(**{f"{field}__icontains": search[:200]})
        qs = qs.filter(condition)
    for item in plan.get("filters", []):
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"} or item["field"] not in entity.fields or item["op"] not in OPERATORS:
            raise QueryPlanError("Некорректный фильтр")
        lookup = entity.fields[item["field"]]
        op = item["op"]
        if op == "contains":
            lookup += "__icontains"
        elif op != "eq":
            lookup += f"__{op}"
        qs = qs.filter(**{lookup: item["value"]})
    aggregates = plan.get("aggregates", [])
    annotations = {}
    for item in aggregates:
        if not isinstance(item, dict) or set(item) != {"op", "field"} or item["op"] not in AGGREGATES or item["field"] not in entity.fields:
            raise QueryPlanError("Некорректная агрегация")
        annotations[f"{item['op']}_{item['field']}"] = AGGREGATES[item["op"]](entity.fields[item["field"]])
    group_by = plan.get("group_by", [])
    if not isinstance(group_by, list) or any(field not in entity.fields for field in group_by):
        raise QueryPlanError("Некорректная группировка")
    if group_by:
        grouped = list(qs.values(*[entity.fields[field] for field in group_by]).annotate(**annotations).order_by(*[entity.fields[field] for field in group_by])[:MAX_LIMIT])
        records = []
        for row in grouped:
            record = {field: _value(row.get(entity.fields[field])) for field in group_by}
            record.update({key: _value(value) for key, value in row.items() if key in annotations})
            records.append(record)
        return {"entity": entity.identifier, "total_count": qs.count(), "returned_count": len(records), "aggregates": {}, "records": records, "pagination": {"page": 1, "page_size": MAX_LIMIT}, "evidence": []}
    aggregate_values = {key: _value(value) for key, value in qs.aggregate(**annotations).items()} if annotations else {}
    total_count = qs.count()
    order_by = plan.get("order_by", entity.default_order)
    descending = isinstance(order_by, str) and order_by.startswith("-")
    order_key = order_by[1:] if descending else order_by
    if order_key not in entity.fields:
        raise QueryPlanError("Недоступная сортировка")
    page = plan.get("page", 1)
    page_size = plan.get("page_size", 20)
    if not isinstance(page, int) or not isinstance(page_size, int) or page < 1 or not 1 <= page_size <= MAX_LIMIT:
        raise QueryPlanError("Некорректная пагинация")
    values = list(qs.order_by(("-" if descending else "") + entity.fields[order_key]).values(*[entity.fields[field] for field in requested])[(page - 1) * page_size:page * page_size])
    records = [{field: _value(row.get(entity.fields[field])) for field in requested} for row in values]
    return {"entity": entity.identifier, "total_count": total_count, "returned_count": len(records), "aggregates": aggregate_values, "records": records, "pagination": {"page": page, "page_size": page_size}, "evidence": [{"entity": entity.identifier, "id": row.get("id")} for row in records if row.get("id") is not None]}
