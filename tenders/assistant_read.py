"""Validated, metadata-driven read-only ORM plans for the global assistant."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import re
from uuid import UUID

from django.apps import apps
from django.core.exceptions import ValidationError
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
    result = {
        "name": name,
        "label": str(field.verbose_name),
        "type": field.get_internal_type(),
        "kind": descriptor["kind"],
        "nullable": bool(getattr(field, "null", False)),
        "filter_ops": sorted(OPS) if descriptor["kind"] == "scalar" else [],
        "aggregate_ops": aggregate_ops,
    }
    if str(field.help_text).strip():
        result["help_text"] = str(field.help_text).strip()
    if descriptor["kind"] == "relation":
        result.update({"relation": field.related_model._meta.label, "traversal": "forward_to_one"})
    if descriptor["kind"] == "json":
        result["json_policy"] = {"read": True, "max_depth": MAX_JSON_DEPTH, "max_items": MAX_JSON_ITEMS}
    choices = [
        {"value": _json_safe_value(value), "label": str(label)}
        for value, label in field.flatchoices
        if value not in (None, "")
    ]
    if choices:
        result["choices"] = choices
    return result


def catalogue(*, compact=False):
    """The single metadata source used for both prompting and validation."""
    items = []
    for model in apps.get_models():
        if not _safe_model(model):
            continue
        fields = _base_fields(model)
        item_fields = [_catalog_field(name, descriptor) for name, descriptor in sorted(fields.items())]
        if compact:
            item_fields = [
                {
                    key: field[key]
                    for key in ("name", "label", "help_text", "type", "kind", "relation", "filter_ops", "aggregate_ops", "choices")
                    if key in field
                }
                for field in item_fields
            ]
            items.append({"id": f"{model._meta.app_label}.{model.__name__}", "fields": item_fields})
        else:
            items.append({"id": f"{model._meta.app_label}.{model.__name__}", "model": model._meta.label, "verbose_name": str(model._meta.verbose_name), "fields": item_fields})
    items = sorted(items, key=lambda item: item["id"])
    if compact:
        return {
            "filter_contract": {
                "item": {"field": "catalogue field name", "op": sorted(OPS), "value": "JSON value compatible with that field type"},
                "date_format": "YYYY-MM-DD",
                "datetime_format": "ISO-8601",
                "choice_rule": "use a choice value, never its human label",
            },
            "entities": items,
        }
    return items


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


def _filter_value(descriptor, op, value):
    field = descriptor["field"]
    name = descriptor["name"]
    if op == "isnull":
        if not isinstance(value, bool):
            raise ReadPlanError(f"Значение фильтра {name} для isnull должно быть true или false")
        return value
    if op == "in":
        if not isinstance(value, list) or not value or len(value) > MAX_ROWS:
            raise ReadPlanError(f"Значение фильтра {name} для in должно быть непустым списком")
        return [_filter_value(descriptor, "eq", item) for item in value]
    if op == "contains" and not isinstance(field, (models.CharField, models.TextField)):
        raise ReadPlanError(f"Оператор contains недоступен для поля {name}")
    if op in {"gte", "gt", "lte", "lt"} and not isinstance(field, (*NUMERIC_TYPES, models.DateField, models.DateTimeField)):
        raise ReadPlanError(f"Оператор {op} недоступен для поля {name}")
    if isinstance(field, models.BooleanField) and not isinstance(value, bool):
        raise ReadPlanError(f"Значение фильтра {name} должно быть true или false")
    if op == "contains" and not isinstance(value, str):
        raise ReadPlanError(f"Значение фильтра {name} для contains должно быть строкой")
    try:
        normalized = field.to_python(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ReadPlanError(f"Некорректное значение поля {name}") from exc
    choices = {choice for choice, _ in field.flatchoices if choice not in (None, "")}
    if choices and normalized not in choices:
        raise ReadPlanError(f"Недопустимое значение поля {name}")
    return normalized


def validate_plan(user, plan):
    """Validate a read plan from the same metadata used by ``execute``.

    Keeping this separate lets the agent reject an impossible next step before
    it reaches the ORM, without maintaining a second field catalogue.
    """
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
    search = plan.get("search")
    searchable = [name for name, item in fields.items() if isinstance(item["field"], (models.CharField, models.TextField))]
    if search not in (None, ""):
        if not isinstance(search, str) or not searchable:
            raise ReadPlanError("Некорректный поиск")
    filters = _validated_items(plan.get("filters"), "Некорректный фильтр")
    for item in filters:
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"} or item.get("op") not in OPS:
            raise ReadPlanError("Некорректный фильтр")
        descriptor = _path(model, fields, item.get("field"))
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Некорректный фильтр")
        item["value"] = _filter_value(descriptor, item["op"], item["value"])
    annotations = {}
    aggregates = _validated_items(plan.get("aggregates"), "Некорректная агрегация")
    for item in aggregates:
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
    order = plan.get("order_by", "-id")
    descending = isinstance(order, str) and order.startswith("-")
    order_descriptor = _path(model, fields, order[1:] if descending else order)
    if order_descriptor["kind"] != "scalar":
        raise ReadPlanError("Недоступная сортировка")
    limit = plan.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ReadPlanError("Некорректный лимит")
    return {
        "model": model, "entity": entity, "fields": fields,
        "selected_names": selected_names, "selected": selected,
        "search": search, "searchable": searchable, "filters": filters,
        "annotations": annotations, "group_names": group_names, "groups": groups,
        "order_descriptor": order_descriptor, "descending": descending,
        "limit": limit,
    }


def execute(user, plan):
    validated = validate_plan(user, plan)
    model = validated["model"]
    entity = validated["entity"]
    fields = validated["fields"]
    selected_names = validated["selected_names"]
    selected = validated["selected"]
    qs = model.objects.all()
    if validated["search"] not in (None, ""):
        condition = Q()
        for name in validated["searchable"]:
            condition |= Q(**{f"{fields[name]['lookup']}__icontains": validated["search"][:200]})
        qs = qs.filter(condition)
    for item in validated["filters"]:
        descriptor = _path(model, fields, item["field"])
        lookup = descriptor["lookup"]
        if item["op"] == "contains":
            lookup += "__icontains"
        elif item["op"] != "eq":
            lookup += f"__{item['op']}"
        qs = qs.filter(**{lookup: item["value"]})
    annotations = validated["annotations"]
    group_names = validated["group_names"]
    groups = validated["groups"]
    total = qs.count()
    if groups:
        group_lookups = [item["lookup"] for item in groups]
        rows = list(qs.values(*group_lookups).annotate(**annotations).order_by(*group_lookups)[:MAX_ROWS])
        records = [{**{name: _output_value(row.get(item["lookup"]), item) for name, item in zip(group_names, groups)}, **{key: _value(row[key]) for key in annotations}} for row in rows]
        return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": {}, "records": records, "sources": [], "source_scope": "grouped results; IDs do not represent every matching record"}
    summary = {key: _value(value) for key, value in qs.aggregate(**annotations).items()} if annotations else {}
    descending = validated["descending"]
    order_descriptor = validated["order_descriptor"]
    limit = validated["limit"]
    value_lookups = [item["lookup"] for item in selected]
    rows = list(qs.order_by(("-" if descending else "") + order_descriptor["lookup"]).values(*value_lookups)[:limit])
    records = [{name: _output_value(row.get(item["lookup"]), item) for name, item in zip(selected_names, selected)} for row in rows]
    sources = [{"entity": entity, "id": row["id"]} for row in records if row.get("id") is not None]
    return {"entity": entity, "total_count": total, "returned_count": len(records), "aggregates": summary, "records": records, "sources": sources, "source_scope": "all matching records" if total == len(sources) else "returned records only; aggregates cover all matching records"}
