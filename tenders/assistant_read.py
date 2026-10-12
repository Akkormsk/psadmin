"""Validated read-only ORM plans for the global assistant."""
from __future__ import annotations

from decimal import Decimal

from django.apps import apps
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum, Q


class ReadPlanError(Exception):
    pass


MAX_ROWS = 50
EXCLUDED_APPS = {"admin", "auth", "contenttypes", "sessions"}
EXCLUDED_MODELS = {"CashAuditLog", "BankSyncState", "OwnerFeedbackEvent", "OwnerInteraction"}
SENSITIVE_NAMES = {"raw", "notification_raw", "clarifications_raw", "complaints_raw", "protocol", "avatar_data", "token", "account_number", "counterparty_account", "own_identifiers"}
FIELD_TYPES = (models.CharField, models.TextField, models.IntegerField, models.DecimalField, models.DateField, models.DateTimeField, models.BooleanField, models.ForeignKey)
AGGREGATES = {"count": Count, "sum": Sum, "min": Min, "max": Max, "avg": Avg}
OPS = {"eq", "contains", "gte", "gt", "lte", "lt", "in", "isnull"}


def _entity(identifier):
    if not isinstance(identifier, str) or "." not in identifier:
        raise ReadPlanError("Недоступная сущность")
    app_label, model_name = identifier.split(".", 1)
    if app_label in EXCLUDED_APPS:
        raise ReadPlanError("Недоступная сущность")
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise ReadPlanError("Недоступная сущность") from exc
    if model.__name__ in EXCLUDED_MODELS:
        raise ReadPlanError("Служебные данные недоступны")
    fields = {"id": model._meta.pk.attname}
    searchable = []
    for field in model._meta.fields:
        if field.name in SENSITIVE_NAMES or not isinstance(field, FIELD_TYPES):
            continue
        fields[field.name] = field.attname
        if isinstance(field, (models.CharField, models.TextField)):
            searchable.append(field.name)
    return model, f"{app_label}.{model.__name__}", fields, searchable


def catalogue():
    return [{"id": f"{model._meta.app_label}.{model.__name__}", "fields": sorted(_entity(f"{model._meta.app_label}.{model.__name__}")[2])} for model in apps.get_models() if model._meta.app_label not in EXCLUDED_APPS and model.__name__ not in EXCLUDED_MODELS]


def _value(value):
    return str(value) if isinstance(value, Decimal) else value


def execute(user, plan):
    if not user.is_superuser:
        raise ReadPlanError("Доступ к бизнес-данным через ассистента разрешён только владельцу")
    allowed = {"entity", "fields", "search", "filters", "order_by", "limit", "aggregates", "group_by"}
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ReadPlanError("Некорректный план чтения")
    model, entity, fields, searchable = _entity(plan.get("entity"))
    selected = plan.get("fields") or list(fields)
    if not isinstance(selected, list) or not selected or any(field not in fields for field in selected):
        raise ReadPlanError("Запрошены недоступные поля")
    qs = model.objects.all()
    search = plan.get("search")
    if search not in (None, ""):
        if not isinstance(search, str) or not searchable:
            raise ReadPlanError("Некорректный поиск")
        condition = Q()
        for field in searchable:
            condition |= Q(**{f"{field}__icontains": search[:200]})
        qs = qs.filter(condition)
    for item in plan.get("filters", []):
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"} or item.get("field") not in fields or item.get("op") not in OPS:
            raise ReadPlanError("Некорректный фильтр")
        lookup = fields[item["field"]]
        if item["op"] == "contains": lookup += "__icontains"
        elif item["op"] != "eq": lookup += f"__{item['op']}"
        qs = qs.filter(**{lookup: item["value"]})
    annotations = {}
    for item in plan.get("aggregates", []):
        if not isinstance(item, dict) or set(item) != {"op", "field"} or item.get("op") not in AGGREGATES or item.get("field") not in fields:
            raise ReadPlanError("Некорректная агрегация")
        annotations[f"{item['op']}_{item['field']}"] = AGGREGATES[item["op"]](fields[item["field"]])
    groups = plan.get("group_by", [])
    if not isinstance(groups, list) or any(field not in fields for field in groups):
        raise ReadPlanError("Некорректная группировка")
    total = qs.count()
    if groups:
        rows = list(qs.values(*[fields[field] for field in groups]).annotate(**annotations).order_by(*[fields[field] for field in groups])[:MAX_ROWS])
        records = [{**{field: _value(row.get(fields[field])) for field in groups}, **{key: _value(row[key]) for key in annotations}} for row in rows]
        return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": {}, "records": records, "sources": []}
    summary = {key: _value(value) for key, value in qs.aggregate(**annotations).items()} if annotations else {}
    order = plan.get("order_by", "-id")
    descending = isinstance(order, str) and order.startswith("-")
    order_field = order[1:] if descending else order
    if order_field not in fields:
        raise ReadPlanError("Недоступная сортировка")
    limit = plan.get("limit", 20)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        raise ReadPlanError("Некорректный лимит")
    rows = list(qs.order_by(("-" if descending else "") + fields[order_field]).values(*[fields[field] for field in selected])[:limit])
    records = [{field: _value(row.get(fields[field])) for field in selected} for row in rows]
    return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": summary, "records": records, "sources": [{"entity": entity, "id": row["id"]} for row in records if row.get("id") is not None]}
