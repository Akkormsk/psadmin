"""Validated, metadata-driven read-only ORM plans for the global assistant."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import re
from uuid import UUID

from django.apps import apps
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum, Q


class ReadPlanError(Exception):
    pass


MAX_ROWS = 50
MAX_RELATION_DEPTH = 2
MAX_JSON_DEPTH = 4
MAX_JSON_ITEMS = 50
EXCLUDED_APPS = {"admin", "auth", "contenttypes", "sessions"}
EXCLUDED_MODELS = {"CashAuditLog", "BankSyncState", "OwnerFeedbackEvent", "OwnerInteraction"}
SENSITIVE_NAMES = {"raw", "notification_raw", "clarifications_raw", "complaints_raw", "protocol", "avatar_data", "token", "account_number", "counterparty_account", "own_identifiers"}
SENSITIVE_FRAGMENTS = ("password", "secret", "token", "credential", "_raw")
SCALAR_TYPES = (models.CharField, models.TextField, models.IntegerField, models.DecimalField, models.FloatField, models.DateField, models.DateTimeField, models.BooleanField)
NUMERIC_TYPES = (models.IntegerField, models.DecimalField, models.FloatField)
AGGREGATES = {"count": Count, "sum": Sum, "min": Min, "max": Max, "avg": Avg}
OPS = {"eq", "contains", "gte", "gt", "lte", "lt", "in", "isnull"}
_AUTH_VALUE = re.compile(r"(?i)\\b(?:bearer|basic|api[-_ ]?key)\\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_VALUE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_+/=-]{40,}$")


def _sensitive(name):
    lowered = name.lower()
    return lowered in SENSITIVE_NAMES or any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def _safe_model(model):
    return not (model._meta.app_label in EXCLUDED_APPS or model.__name__ in EXCLUDED_MODELS or model._meta.auto_created)


def _field_kind(field):
    if isinstance(field, models.JSONField):
        return "json"
    if isinstance(field, (models.ForeignKey, models.OneToOneField)):
        return "relation"
    if isinstance(field, SCALAR_TYPES):
        return "scalar"
    return None


def _base_fields(model):
    fields = {"id": {"lookup": model._meta.pk.attname, "field": model._meta.pk, "kind": "scalar"}}
    for field in model._meta.fields:
        kind = _field_kind(field)
        if field.primary_key or _sensitive(field.name) or kind is None:
            continue
        if kind == "relation" and not _safe_model(field.related_model):
            continue
        fields[field.name] = {"lookup": field.attname if kind == "relation" else field.name, "field": field, "kind": kind}
    return fields


def _entity(identifier):
    if not isinstance(identifier, str) or "." not in identifier:
        raise ReadPlanError("Недоступная сущность")
    app_label, model_name = identifier.split(".", 1)
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise ReadPlanError("Недоступная сущность") from exc
    if not _safe_model(model):
        raise ReadPlanError("Служебные данные недоступны")
    return model, f"{model._meta.app_label}.{model.__name__}", _base_fields(model)


def _path(model, fields, name, *, allow_json=False):
    """Resolve a public path using only safe forward to-one Django relations."""
    if not isinstance(name, str) or not name:
        raise ReadPlanError("Запрошены недоступные поля")
    parts = name.split(".")
    if len(parts) > MAX_RELATION_DEPTH + 1:
        raise ReadPlanError("Слишком глубокий переход по связям")
    current_fields, lookups = fields, []
    for position, part in enumerate(parts):
        descriptor = current_fields.get(part)
        if descriptor is None:
            raise ReadPlanError("Запрошены недоступные поля")
        if position == len(parts) - 1:
            if descriptor["kind"] == "json" and not allow_json:
                raise ReadPlanError("JSON-поля нельзя использовать в этом выражении")
            return {**descriptor, "lookup": "__".join(lookups + [descriptor["lookup"]]), "name": name}
        if descriptor["kind"] != "relation":
            raise ReadPlanError("Переход разрешён только по связи ForeignKey")
        related = descriptor["field"].related_model
        if not _safe_model(related):
            raise ReadPlanError("Переход к служебным данным запрещён")
        lookups.append(descriptor["field"].name)
        current_fields = _base_fields(related)
    raise ReadPlanError("Запрошены недоступные поля")


def _catalog_field(name, descriptor):
    field = descriptor["field"]
    aggregate_ops = []
    if descriptor["kind"] == "scalar":
        aggregate_ops = ["count", "min", "max"]
        if isinstance(field, NUMERIC_TYPES) and not isinstance(field, models.BooleanField):
            aggregate_ops.extend(["sum", "avg"])
    result = {"name": name, "type": field.get_internal_type(), "kind": descriptor["kind"], "nullable": bool(getattr(field, "null", False)), "filter_ops": sorted(OPS) if descriptor["kind"] == "scalar" else [], "aggregate_ops": aggregate_ops}
    if descriptor["kind"] == "relation":
        result.update({"relation": field.related_model._meta.label, "traversal": "forward_to_one"})
    if descriptor["kind"] == "json":
        result["json_policy"] = {"read": True, "max_depth": MAX_JSON_DEPTH, "max_items": MAX_JSON_ITEMS}
    return result


def catalogue():
    """The single metadata source used for both prompting and validation."""
    items = []
    for model in apps.get_models():
        if not _safe_model(model):
            continue
        fields = _base_fields(model)
        items.append({"id": f"{model._meta.app_label}.{model.__name__}", "model": model._meta.label, "verbose_name": str(model._meta.verbose_name), "fields": [_catalog_field(name, descriptor) for name, descriptor in sorted(fields.items())]})
    return sorted(items, key=lambda item: item["id"])



def _field_aliases(model):
    """Resolve display labels only when unique; never infer arbitrary ORM columns."""
    result = {}
    for name, descriptor in _base_fields(model).items():
        field = descriptor["field"]
        for alias in (name, str(getattr(field, "verbose_name", ""))):
            key = alias.strip().casefold()
            if key:
                if key in result and result[key] != name:
                    result[key] = None
                elif key not in result:
                    result[key] = name
    return result


def normalize_plan(plan):
    """Canonicalise harmless LLM schema variations against live model metadata.

    Unknown fields are never silently substituted or dropped.
    """
    if not isinstance(plan, dict):
        raise ReadPlanError("План чтения должен быть объектом")
    result = dict(plan)
    if "entity" not in result and "model" in result:
        result["entity"] = result.pop("model")
    identifier = result.get("entity")
    if isinstance(identifier, str) and "." not in identifier:
        matches = [item["id"] for item in catalogue()
                   if item["id"].rsplit(".", 1)[-1].casefold() == identifier.casefold()]
        if len(matches) == 1:
            result["entity"] = matches[0]
    model, _, fields = _entity(result.get("entity"))
    aliases = _field_aliases(model)
    def resolve(name):
        if not isinstance(name, str):
            raise ReadPlanError("Поле должно быть строкой")
        if name in fields:
            return name
        canonical = aliases.get(name.strip().casefold())
        if canonical:
            return canonical
        raise ReadPlanError("Недоступное поле: " + name[:80])
    if "fields" in result and isinstance(result["fields"], list):
        result["fields"] = [resolve(name) for name in result["fields"]]
    if "order_by" in result and isinstance(result["order_by"], str):
        order = result["order_by"]
        result["order_by"] = ("-" if order.startswith("-") else "") + resolve(order.lstrip("-"))
    if "filters" in result and isinstance(result["filters"], list):
        result["filters"] = [
            {**item, "field": resolve(item["field"])}
            if isinstance(item, dict) and "field" in item else item
            for item in result["filters"]
        ]
    return result


def _value(value):
    return _json_safe_value(value)


def _json_safe_value(value):
    """Convert ORM values before a result is sent to the model or HTTP JSON."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _looks_like_secret(value):
    """Catch credential-shaped values even when their key is innocuous."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return bool(_AUTH_VALUE.search(candidate) or _JWT_VALUE.search(candidate) or _OPAQUE_VALUE.fullmatch(candidate))


def _json_value(value, depth=0):
    if depth > MAX_JSON_DEPTH:
        return "[ограничена глубина JSON]"
    if isinstance(value, dict):
        return {str(key): _json_value(item, depth + 1) for key, item in list(value.items())[:MAX_JSON_ITEMS] if not _sensitive(str(key))}
    if isinstance(value, list):
        return [_json_value(item, depth + 1) for item in value[:MAX_JSON_ITEMS]]
    if _looks_like_secret(value):
        return "[скрыто: возможные учётные данные]"
    return _json_safe_value(value)


def _output_value(value, descriptor):
    return _json_value(value) if descriptor["kind"] == "json" else _json_safe_value(value)


def _validated_items(value, label):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReadPlanError(label)
    return value


def execute(user, plan):
    if not user.is_superuser:
        raise ReadPlanError("Доступ к бизнес-данным через ассистента разрешён только владельцу")
    allowed = {"entity", "fields", "search", "filters", "order_by", "limit", "aggregates", "group_by"}
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ReadPlanError("Некорректный план чтения")
    model, entity, fields = _entity(plan.get("entity"))
    selected_names = plan.get("fields") or list(fields)
    if not isinstance(selected_names, list) or not selected_names:
        raise ReadPlanError("Запрошены недоступные поля")
    selected = [_path(model, fields, name, allow_json=True) for name in selected_names]
    if "id" not in selected_names:
        selected_names = ["id", *selected_names]
        selected = [_path(model, fields, "id"), *selected]
    qs = model.objects.all()
    search = plan.get("search")
    searchable = [name for name, item in fields.items() if isinstance(item["field"], (models.CharField, models.TextField))]
    if search not in (None, ""):
        if not isinstance(search, str) or not searchable:
            raise ReadPlanError("Некорректный поиск")
        condition = Q()
        for name in searchable:
            condition |= Q(**{f"{fields[name]['lookup']}__icontains": search[:200]})
        qs = qs.filter(condition)
    for item in _validated_items(plan.get("filters"), "Некорректный фильтр"):
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"} or item.get("op") not in OPS:
            raise ReadPlanError("Некорректный фильтр")
        descriptor = _path(model, fields, item.get("field"))
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Некорректный фильтр")
        lookup = descriptor["lookup"]
        if item["op"] == "contains":
            lookup += "__icontains"
        elif item["op"] != "eq":
            lookup += f"__{item['op']}"
        qs = qs.filter(**{lookup: item["value"]})
    annotations = {}
    for item in _validated_items(plan.get("aggregates"), "Некорректная агрегация"):
        if not isinstance(item, dict) or set(item) != {"op", "field"} or item.get("op") not in AGGREGATES:
            raise ReadPlanError("Некорректная агрегация")
        descriptor = _path(model, fields, item.get("field"))
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Некорректная агрегация")
        if item["op"] in {"sum", "avg"} and (not isinstance(descriptor["field"], NUMERIC_TYPES) or isinstance(descriptor["field"], models.BooleanField)):
            raise ReadPlanError("Эта агрегация доступна только для числового поля")
        annotations[f"{item['op']}_{item['field'].replace('.', '_')}"] = AGGREGATES[item["op"]](descriptor["lookup"])
    group_names = _validated_items(plan.get("group_by"), "Некорректная группировка")
    groups = [_path(model, fields, name) for name in group_names]
    if any(item["kind"] != "scalar" for item in groups):
        raise ReadPlanError("Некорректная группировка")
    total = qs.count()
    if groups:
        group_lookups = [item["lookup"] for item in groups]
        rows = list(qs.values(*group_lookups).annotate(**annotations).order_by(*group_lookups)[:MAX_ROWS])
        records = [{**{name: _output_value(row.get(item["lookup"]), item) for name, item in zip(group_names, groups)}, **{key: _value(row[key]) for key in annotations}} for row in rows]
        return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": {}, "records": records, "sources": [], "source_scope": "grouped results; IDs do not represent every matching record"}
    summary = {key: _value(value) for key, value in qs.aggregate(**annotations).items()} if annotations else {}
    order = plan.get("order_by", "-id")
    descending = isinstance(order, str) and order.startswith("-")
    order_descriptor = _path(model, fields, order[1:] if descending else order)
    if order_descriptor["kind"] != "scalar":
        raise ReadPlanError("Недоступная сортировка")
    limit = plan.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ReadPlanError("Некорректный лимит")
    value_lookups = [item["lookup"] for item in selected]
    rows = list(qs.order_by(("-" if descending else "") + order_descriptor["lookup"]).values(*value_lookups)[:limit])
    records = [{name: _output_value(row.get(item["lookup"]), item) for name, item in zip(selected_names, selected)} for row in rows]
    sources = [{"entity": entity, "id": row["id"]} for row in records if row.get("id") is not None]
    return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": summary, "records": records, "sources": sources, "source_scope": "all matching records" if total == len(sources) else "returned records only; aggregates cover all matching records"}
