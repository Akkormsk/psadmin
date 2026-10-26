"""Validated, metadata-driven read-only ORM plans for the global assistant."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from inspect import cleandoc
import json
import re
from uuid import UUID

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum, Q
from django.db.models.functions import TruncDay, TruncMonth, TruncQuarter, TruncWeek, TruncYear
from django.utils import timezone


class ReadPlanError(Exception):
    pass


MAX_ROWS = 50
MAX_RELATION_DEPTH = 2
MAX_JSON_DEPTH = 4
MAX_JSON_ITEMS = 50
EXCLUDED_APPS = {"admin", "auth", "contenttypes", "sessions", "core"}
EXCLUDED_MODELS = {"CashAuditLog", "BankSyncState", "OwnerFeedbackEvent", "OwnerInteraction"}
SENSITIVE_NAMES = {"raw", "notification_raw", "clarifications_raw", "complaints_raw", "protocol", "avatar_data", "token", "account_number", "counterparty_account", "own_identifiers"}
SENSITIVE_FRAGMENTS = ("password", "secret", "token", "credential", "_raw")
IDENTITY_FIELDS = {"id", "username", "first_name", "last_name"}
SCALAR_TYPES = (models.CharField, models.TextField, models.IntegerField, models.DecimalField, models.FloatField, models.DateField, models.DateTimeField, models.BooleanField)
NUMERIC_TYPES = (models.IntegerField, models.DecimalField, models.FloatField)
AGGREGATES = {"count": Count, "sum": Sum, "min": Min, "max": Max, "avg": Avg}
OPS = {"eq", "contains", "gte", "gt", "lte", "lt", "in", "isnull"}
DATE_GROUP_PERIODS = ("day", "week", "month", "quarter", "year")
DATE_GROUP_LABELS = {
    "day": "день", "week": "неделя", "month": "месяц",
    "quarter": "квартал", "year": "год",
}
DATE_TRUNCS = {
    "day": TruncDay, "week": TruncWeek, "month": TruncMonth,
    "quarter": TruncQuarter, "year": TruncYear,
}
_AUTH_VALUE = re.compile(r"(?i)\\b(?:bearer|basic|api[-_ ]?key)\\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_VALUE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_+/=-]{40,}$")


def _field_value_contract(descriptor):
    field = descriptor["field"]
    choices = [_json_safe_value(value) for value, _label in field.flatchoices if value not in (None, "")]
    if choices:
        labels = "; ".join(f"{value} — {label}" for value, label in field.flatchoices if value not in (None, ""))
        return {
            "type": "string" if isinstance(choices[0], str) else "number",
            "enum": choices,
            "description": f"Допустимые значения: {labels}",
        }
    if isinstance(field, models.BooleanField):
        return "boolean"
    if isinstance(field, models.IntegerField):
        return "integer"
    if isinstance(field, (models.DecimalField, models.FloatField)):
        return {"any_of": ["number", "string"]}
    if isinstance(field, models.DateTimeField):
        return {"type": "string", "description": "ISO-8601 with timezone offset"}
    if isinstance(field, models.DateField):
        return {"type": "string", "description": "YYYY-MM-DD"}
    return "string"


def _paths(model, fields, *, depth=0, prefix=""):
    result = {}
    for name, descriptor in fields.items():
        path = f"{prefix}.{name}" if prefix else name
        result[path] = descriptor
        if descriptor["kind"] == "relation" and depth < MAX_RELATION_DEPTH:
            result.update(_paths(descriptor["field"].related_model, _base_fields(descriptor["field"].related_model), depth=depth + 1, prefix=path))
    return result


def _filter_contracts(paths):
    grouped = {}
    for name, descriptor in sorted(paths.items()):
        if descriptor["kind"] != "scalar":
            continue
        field = descriptor["field"]
        operations = {"eq", "in", "isnull"}
        if isinstance(field, (models.CharField, models.TextField)):
            operations.add("contains")
        if isinstance(field, (*NUMERIC_TYPES, models.DateField, models.DateTimeField)):
            operations.update({"gte", "gt", "lte", "lt"})
        value = _field_value_contract(descriptor)
        in_value = {"type": "array", "item": value, "minimum": 1, "maximum": MAX_ROWS}
        field_groups = {}
        for operation in sorted(operations):
            value_contract = "boolean" if operation == "isnull" else (in_value if operation == "in" else value)
            key = json.dumps(value_contract, ensure_ascii=False, sort_keys=True)
            field_groups.setdefault(key, {"value": value_contract, "operations": []})["operations"].append(operation)
        for group in field_groups.values():
            key = json.dumps({"operations": group["operations"], "value": group["value"]}, ensure_ascii=False, sort_keys=True)
            grouped.setdefault(key, {"fields": [], "operations": group["operations"], "value": group["value"]})["fields"].append((name, field))
    return [
        {
                "type": "object", "required": True, "additional_properties": False,
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": [name for name, _field in group["fields"]],
                        "description": "Поля: " + "; ".join(f"{name} — {field.verbose_name}" for name, field in group["fields"]),
                    },
                    "op": {"type": "string", "enum": group["operations"]},
                    "value": group["value"],
                },
        }
        for group in grouped.values()
    ]


def _aggregate_contracts(paths):
    grouped = {}
    for name, descriptor in sorted(paths.items()):
        operations = _catalog_field(name, descriptor)["aggregate_ops"]
        if not operations:
            continue
        key = json.dumps(operations, ensure_ascii=False)
        grouped.setdefault(key, {"fields": [], "operations": operations})["fields"].append(name)
    return [
        {"type": "object", "required": True, "additional_properties": False, "properties": {
            "op": {"type": "string", "enum": group["operations"]},
            "field": {"type": "string", "enum": group["fields"]},
        }}
        for group in grouped.values()
    ]


def plan_contract(entity=None):
    """Public READ schema built from the same limits and operators as validation."""
    properties = {
        "entity": {"type": "string", "required": True, "description": "entity ID returned by schema.describe"},
        "view": {"type": "string", "required": False, "description": "optional business view ID from schema.describe"},
        "fields": {"type": "string[]", "required": False, "description": "field names from schema.describe; id is always returned"},
        "search": {"type": "string", "required": False},
        "filters": {"type": "object[]", "required": False, "item": {"field": "opened field name", "op": sorted(OPS), "value": {"any_of": ["string", "number", "boolean", "null", "string[]", "number[]", "boolean[]"], "description": "JSON value compatible with the selected field"}}},
        "order_by": {"type": "string|string[]", "required": False, "description": "one or more opened scalar fields, each optionally prefixed with -; id is added as a stable final tiebreaker"},
        "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": MAX_ROWS},
        "aggregates": {"type": "object[]", "required": False, "item": {"op": sorted(AGGREGATES), "field": "opened scalar field"}},
        "group_by": {"type": "string[]", "required": False, "description": "opened scalar field names"},
    }
    if entity is not None:
        model, canonical, fields = _entity(entity)
        paths = _paths(model, fields)
        scalar_paths = {name: item for name, item in paths.items() if item["kind"] == "scalar"}
        path_labels = "; ".join(f"{name} — {item['field'].verbose_name}" for name, item in sorted(paths.items()))
        scalar_labels = "; ".join(f"{name} — {item['field'].verbose_name}" for name, item in sorted(scalar_paths.items()))
        views = [item["id"] for item in _business_views(model)]
        properties.update({
            "entity": {"type": "string", "required": True, "enum": [canonical]},
            "view": {"type": "string" if views else "null", "required": False,
                     **({"enum": views} if views else {}),
                     "description": "Use a listed business view, or null for the base queryset. Never invent a view ID."},
            "fields": {"type": "string[]", "required": False, "description": f"Поля: {path_labels}", "item": {"type": "string", "enum": sorted(paths)}},
            "filters": {"type": "object[]", "required": False, "item": {"any_of": _filter_contracts(paths)}},
            "order_by": {"any_of": [
                {"type": "string", "description": f"Поля сортировки: {scalar_labels}", "enum": sorted([name for name in scalar_paths] + [f"-{name}" for name in scalar_paths])},
                {"type": "string[]", "description": f"Поля сортировки: {scalar_labels}", "item": {"type": "string", "enum": sorted([name for name in scalar_paths] + [f"-{name}" for name in scalar_paths])}},
            ], "required": False},
            "aggregates": {"type": "object[]", "required": False, "item": {"any_of": _aggregate_contracts(scalar_paths)}},
            "group_by": {
                "type": "object[]", "required": False,
                "description": f"Поля группировки: {scalar_labels}. Даты можно группировать по day/week/month/quarter/year.",
                "item": {"any_of": [
                    {"type": "string", "enum": sorted(scalar_paths)},
                    {
                        "type": "object", "required": True, "additional_properties": False,
                        "properties": {
                            "field": {"type": "string", "enum": sorted(
                                name for name, item in scalar_paths.items()
                                if isinstance(item["field"], (models.DateField, models.DateTimeField))
                            )},
                            "period": {"type": "string", "enum": list(DATE_GROUP_PERIODS)},
                        },
                    },
                ]},
            },
        })
    return {
        "type": "object",
        "required": True,
        "additional_properties": False,
        "properties": properties,
    }


def input_schema(entity=None):
    return {"plan": plan_contract(entity)}


def _sensitive(name):
    lowered = name.lower()
    return lowered in SENSITIVE_NAMES or any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def _safe_model(model):
    return not (model._meta.app_label in EXCLUDED_APPS or model.__name__ in EXCLUDED_MODELS or model._meta.auto_created)


def _model_description(model):
    return cleandoc(model.__doc__ or "").strip()[:1000]


def _business_views(model):
    provider = getattr(model, "assistant_read_views", None)
    if not callable(provider):
        return []
    views = provider()
    if not isinstance(views, (list, tuple)):
        return []
    return [
        {key: item[key] for key in ("id", "label", "description") if key in item}
        for item in views
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("label"), str)
    ]


def _business_queryset(model, view, user):
    if not view:
        return model.objects.all()
    if view not in {item["id"] for item in _business_views(model)}:
        raise ReadPlanError("Неизвестное бизнес-представление")
    provider = getattr(model, "assistant_read_queryset", None)
    if not callable(provider):
        raise ReadPlanError("Бизнес-представление недоступно для чтения")
    try:
        return provider(view, user)
    except (TypeError, ValueError) as exc:
        raise ReadPlanError("Бизнес-представление недоступно для чтения") from exc


def _field_kind(field):
    if isinstance(field, models.JSONField):
        return "json"
    if isinstance(field, (models.ForeignKey, models.OneToOneField)):
        return "relation"
    if isinstance(field, SCALAR_TYPES):
        return "scalar"
    return None


def _identity_model(model):
    return model is get_user_model()


def _safe_related_model(model):
    return _safe_model(model) or _identity_model(model)


def _base_fields(model):
    fields = {"id": {"lookup": model._meta.pk.attname, "field": model._meta.pk, "kind": "scalar"}}
    for field in model._meta.fields:
        kind = _field_kind(field)
        if field.primary_key or _sensitive(field.name) or kind is None:
            continue
        if _identity_model(model) and field.name not in IDENTITY_FIELDS:
            continue
        if kind == "relation" and not _safe_related_model(field.related_model):
            continue
        fields[field.name] = {"lookup": field.attname if kind == "relation" else field.name, "field": field, "kind": kind}
    for name, field in getattr(model, "assistant_read_computed_fields", {}).items():
        if name not in fields and not _sensitive(name) and isinstance(field, SCALAR_TYPES) and callable(getattr(model, name, None)):
            fields[name] = {"lookup": name, "field": field, "kind": "computed",
                            "related": getattr(model, "assistant_read_computed_related", ())}
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


def resolve_entity(identifier):
    """Return the canonical public identifier without exposing model internals."""
    return _entity(identifier)[1]


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
            allowed = ", ".join(sorted(current_fields)[:30])
            raise ReadPlanError(f"Недоступное поле {part}. Допустимые поля: {allowed}")
        if position == len(parts) - 1:
            if descriptor["kind"] == "json" and not allow_json:
                raise ReadPlanError("JSON-поля нельзя использовать в этом выражении")
            return {**descriptor, "lookup": "__".join(lookups + [descriptor["lookup"]]), "name": name}
        if descriptor["kind"] != "relation":
            raise ReadPlanError("Переход разрешён только по связи ForeignKey")
        related = descriptor["field"].related_model
        if not _safe_related_model(related):
            raise ReadPlanError("Переход к служебным данным запрещён")
        lookups.append(descriptor["field"].name)
        current_fields = _base_fields(related)
    raise ReadPlanError("Запрошены недоступные поля")


def _catalog_field(name, descriptor, *, include_related=True):
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
        if include_related:
            related_fields = _base_fields(field.related_model)
            result["fields"] = [
                _catalog_field(item_name, item, include_related=False)
                for item_name, item in sorted(related_fields.items())
            ]
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
            items.append({
                "id": f"{model._meta.app_label}.{model.__name__}", "model": model._meta.label,
                "verbose_name": str(model._meta.verbose_name), "description": _model_description(model),
                "views": _business_views(model), "fields": item_fields,
            })
    items = sorted(items, key=lambda item: item["id"])
    if compact:
        return {
            "filter_contract": {
                "item": {"field": "catalogue field name", "op": sorted(OPS), "value": "JSON value compatible with that field type"},
                "date_format": "YYYY-MM-DD",
                "datetime_format": "ISO-8601",
                "value_formats": {
                    "DateField": "YYYY-MM-DD",
                    "DateTimeField": "ISO-8601 with timezone offset",
                    "IntegerField": "JSON integer",
                    "DecimalField": "JSON number or decimal string with .",
                    "FloatField": "JSON number",
                    "BooleanField": "true or false",
                    "CharField": "JSON string",
                    "TextField": "JSON string",
                },
                "choice_rule": "use a choice value, never its human label",
            },
            "entities": items,
        }
    return items


def describe_entity(identifier):
    """Return one entity's current public metadata without a global catalogue."""
    model, entity, fields = _entity(identifier)
    return {
        "entity": entity,
        "label": str(model._meta.verbose_name),
        "description": _model_description(model),
        "views": _business_views(model),
        "fields": [_catalog_field(name, descriptor) for name, descriptor in sorted(fields.items())],
        "filter_contract": catalogue(compact=True)["filter_contract"],
    }


def _display_value(value, field):
    for raw, label in field.flatchoices:
        if value == raw or value == _json_safe_value(raw):
            return str(label)
    if isinstance(field, models.DateTimeField) and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if timezone.is_aware(parsed):
                parsed = timezone.localtime(parsed)
            return parsed.isoformat(sep=" ")
        except ValueError:
            pass
    if isinstance(field, models.DateField) and isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return _json_safe_value(value)


def _with_display(result, model, fields, labels=None):
    paths = _paths(model, fields)
    labels = labels or {}
    records = result.get("records") if isinstance(result.get("records"), list) else []
    names = []
    for record in records:
        if isinstance(record, dict):
            for name in record:
                if name == "empty_period":
                    continue
                if name not in names:
                    names.append(name)
    result["display"] = {
        "entity": str(model._meta.verbose_name),
        "fields": [{"name": name, "label": labels.get(name, str(paths[name]["field"].verbose_name) if name in paths else name)}
                   for name in names],
        "records": [
            {
                name: _display_value(value, paths[name]["field"]) if name in paths else _json_safe_value(value)
                for name, value in record.items()
                if name != "empty_period"
            }
            for record in records if isinstance(record, dict)
        ],
        "aggregates": {name: _json_safe_value(value) for name, value in (result.get("aggregates") or {}).items()},
    }
    return result


def present_result(result):
    """Render only values that a validated READ actually returned."""
    if not isinstance(result, dict):
        return "Данные получены."
    display = result.get("display") if isinstance(result.get("display"), dict) else {}
    labels = {item.get("name"): item.get("label") for item in display.get("fields", []) if isinstance(item, dict)}
    lines = [f"Найдено записей: {result.get('total_count', 0)}."]
    for name, value in (display.get("aggregates") or result.get("aggregates") or {}).items():
        lines.append(f"{name}: {_json_safe_value(value)}.")
    for record in (display.get("records") or result.get("records") or [])[:MAX_ROWS]:
        if isinstance(record, dict):
            values = [f"{labels.get(name, name)}: {_json_safe_value(value)}" for name, value in record.items()]
            if values:
                lines.append("• " + "; ".join(values))
    return "\n".join(lines)


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


def _group_spec(model, fields, value, index):
    if isinstance(value, str):
        descriptor = _path(model, fields, value)
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Некорректная группировка")
        return {
            "descriptor": descriptor, "field": value, "period": None,
            "name": value, "label": str(descriptor["field"].verbose_name),
            "lookup": descriptor["lookup"],
        }
    if not isinstance(value, dict) or set(value) != {"field", "period"}:
        raise ReadPlanError("Некорректная группировка")
    field_name, period = value.get("field"), value.get("period")
    descriptor = _path(model, fields, field_name)
    if period not in DATE_GROUP_PERIODS or not isinstance(descriptor["field"], (models.DateField, models.DateTimeField)):
        raise ReadPlanError("Группировка по периоду доступна только для даты")
    return {
        "descriptor": descriptor, "field": field_name, "period": period,
        "name": f"{field_name}:{period}",
        "label": f"{descriptor['field'].verbose_name} ({DATE_GROUP_LABELS[period]})",
        "lookup": f"_assistant_group_{index}",
    }


def _group_expression(group):
    if group["period"] is None:
        return None
    arguments = [group["descriptor"]["lookup"]]
    if isinstance(group["descriptor"]["field"], models.DateTimeField):
        return DATE_TRUNCS[group["period"]](*arguments, tzinfo=timezone.get_current_timezone())
    return DATE_TRUNCS[group["period"]](*arguments)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _period_start(value, period):
    if period == "day":
        return value
    if period == "week":
        return value - timedelta(days=value.weekday())
    if period == "month":
        return value.replace(day=1)
    if period == "quarter":
        return value.replace(month=((value.month - 1) // 3) * 3 + 1, day=1)
    return value.replace(month=1, day=1)


def _next_period(value, period):
    if period == "day":
        return value + timedelta(days=1)
    if period == "week":
        return value + timedelta(days=7)
    if period == "month":
        return date(value.year + (value.month == 12), value.month % 12 + 1, 1)
    if period == "quarter":
        month = value.month + 3
        return date(value.year + (month > 12), (month - 1) % 12 + 1, 1)
    return date(value.year + 1, 1, 1)


def _date_group_bounds(filters, group):
    lower = upper = None
    for item in filters:
        if item["field"] != group["field"]:
            continue
        value = _as_date(item["value"])
        if value is None:
            continue
        if item["op"] == "gte":
            lower = max(lower, value) if lower else value
        elif item["op"] == "gt":
            candidate = value + timedelta(days=1)
            lower = max(lower, candidate) if lower else candidate
        elif item["op"] == "lte":
            upper = min(upper, value) if upper else value
        elif item["op"] == "lt":
            candidate = value - timedelta(days=1)
            upper = min(upper, candidate) if upper else candidate
    return (lower, upper) if lower is not None and upper is not None else (None, None)


def _fill_empty_periods(records, groups, aggregate_specs, filters):
    if len(groups) != 1 or groups[0]["period"] is None:
        return records
    group = groups[0]
    lower, upper = _date_group_bounds(filters, group)
    if lower is None or upper is None:
        return records
    start, end = _period_start(lower, group["period"]), _period_start(upper, group["period"])
    if start > end:
        return records
    existing = {_period_start(_as_date(row.get(group["name"])), group["period"]): row for row in records if _as_date(row.get(group["name"]))}
    result, current = [], start
    while current <= end:
        row = existing.get(current)
        if row is None:
            row = {
                group["name"]: current.isoformat(), "empty_period": True,
                **{key: 0 if operation in {"count", "sum"} else None for key, operation, _descriptor in aggregate_specs},
            }
        result.append(row)
        current = _next_period(current, group["period"])
    return result


def _group_comparisons(records, groups, aggregate_specs):
    if not groups:
        return {}
    comparisons = {}
    for key, _operation, _descriptor in aggregate_specs:
        values = []
        for row in records:
            try:
                value = Decimal(str(row.get(key)))
            except (ArithmeticError, ValueError, TypeError):
                continue
            values.append((value, row))
        if not values:
            continue
        values.sort(key=lambda item: item[0], reverse=True)
        maximum, row = values[0]
        comparisons[key] = {"maximum": {
            "group": {item["name"]: row.get(item["name"]) for item in groups},
            "value": _value(maximum),
            "difference_to_next": _value(maximum - values[1][0]) if len(values) > 1 else None,
        }}
    return comparisons


def validate_plan(user, plan):
    """Validate a read plan from the same metadata used by ``execute``.

    Keeping this separate lets the agent reject an impossible next step before
    it reaches the ORM, without maintaining a second field catalogue.
    """
    if not user.is_superuser:
        raise ReadPlanError("Доступ к бизнес-данным через ассистента разрешён только владельцу")
    contract = plan_contract()["properties"]
    if not isinstance(plan, dict):
        raise ReadPlanError("Некорректный план чтения")
    unknown = set(plan) - set(contract)
    if unknown:
        raise ReadPlanError("Недопустимые параметры READ: " + ", ".join(sorted(unknown)))
    if "entity" not in plan or plan["entity"] in (None, ""):
        raise ReadPlanError("В READ-плане обязателен параметр entity из открытого описания сущности")
    model, entity, fields = _entity(plan.get("entity"))
    view = plan.get("view")
    if view is not None and (not isinstance(view, str) or view not in {item["id"] for item in _business_views(model)}):
        raise ReadPlanError("Неизвестное бизнес-представление")
    selected_names = plan.get("fields") or list(fields)
    if not isinstance(selected_names, list) or not selected_names:
        raise ReadPlanError("Некорректный список полей")
    selected = [_path(model, fields, name, allow_json=True) for name in selected_names]
    if "id" not in selected_names:
        selected_names = ["id", *selected_names]
        selected = [_path(model, fields, "id"), *selected]
    search = plan.get("search")
    searchable = [name for name, item in fields.items() if item["kind"] == "scalar" and isinstance(item["field"], (models.CharField, models.TextField))]
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
    aggregate_specs = []
    aggregates = _validated_items(plan.get("aggregates"), "Некорректная агрегация")
    for item in aggregates:
        if not isinstance(item, dict) or set(item) != {"op", "field"} or item.get("op") not in AGGREGATES:
            raise ReadPlanError("Некорректная агрегация")
        descriptor = _path(model, fields, item.get("field"))
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Некорректная агрегация")
        if item["op"] in {"sum", "avg"} and (not isinstance(descriptor["field"], NUMERIC_TYPES) or isinstance(descriptor["field"], models.BooleanField)):
            raise ReadPlanError("Эта агрегация доступна только для числового поля")
        key = f"{item['op']}_{item['field'].replace('.', '_')}"
        annotations[key] = AGGREGATES[item["op"]](descriptor["lookup"])
        aggregate_specs.append((key, item["op"], descriptor))
    group_names = _validated_items(plan.get("group_by"), "Некорректная группировка")
    groups = [_group_spec(model, fields, item, index) for index, item in enumerate(group_names)]
    if len({item["name"] for item in groups}) != len(groups):
        raise ReadPlanError("Поле группировки указано повторно")
    order = plan.get("order_by")
    if order is None:
        order = "-id"
    if isinstance(order, str):
        order_names = [order]
    elif isinstance(order, list) and order and all(isinstance(item, str) and item for item in order):
        order_names = order[:]
    else:
        raise ReadPlanError("Некорректная сортировка")
    if len({item.lstrip("-") for item in order_names}) != len(order_names):
        raise ReadPlanError("Поле сортировки указано повторно")
    order_descriptors = []
    for name in order_names:
        descending = name.startswith("-")
        descriptor = _path(model, fields, name[1:] if descending else name)
        if descriptor["kind"] != "scalar":
            raise ReadPlanError("Недоступная сортировка")
        order_descriptors.append((descriptor, descending))
    if "id" not in {item.lstrip("-") for item in order_names}:
        order_descriptors.append((_path(model, fields, "id"), order_descriptors[-1][1]))
    limit = 20 if plan.get("limit") is None else plan["limit"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ReadPlanError("Некорректный лимит")
    return {
        "model": model, "entity": entity, "view": view, "fields": fields,
        "selected_names": selected_names, "selected": selected,
        "search": search, "searchable": searchable, "filters": filters,
        "annotations": annotations, "aggregate_specs": aggregate_specs, "group_names": group_names, "groups": groups,
        "order_descriptors": order_descriptors,
        "limit": limit,
    }


def execute(user, plan):
    validated = validate_plan(user, plan)
    model = validated["model"]
    entity = validated["entity"]
    fields = validated["fields"]
    selected_names = validated["selected_names"]
    selected = validated["selected"]
    qs = _business_queryset(model, validated["view"], user)
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
        group_annotations = {
            group["lookup"]: expression
            for group in groups if (expression := _group_expression(group)) is not None
        }
        group_lookups = [item["lookup"] for item in groups]
        rows = list(qs.annotate(**group_annotations).values(*group_lookups).annotate(**annotations).order_by(*group_lookups)[:MAX_ROWS])
        records = [
            {
                **{item["name"]: _output_value(row.get(item["lookup"]), item["descriptor"]) for item in groups},
                **{key: _value(row[key]) for key in annotations},
            }
            for row in rows
        ]
        records = _fill_empty_periods(records, groups, validated["aggregate_specs"], validated["filters"])
        return _with_display({
            "entity": entity, "query": _query_summary(validated, order_by=group_names),
            "total_count": total, "returned_count": len(records), "aggregates": {}, "records": records,
            "comparisons": _group_comparisons(records, groups, validated["aggregate_specs"]),
            "grouped": True, "sources": [], "source_scope": "grouped results; IDs do not represent every matching record",
        }, model, fields, {item["name"]: item["label"] for item in groups})
    summary = {key: _value(value) for key, value in qs.aggregate(**annotations).items()} if annotations else {}
    limit = validated["limit"]
    value_lookups = [item["lookup"] for item in selected if item["kind"] != "computed"]
    order_lookups = [
        ("-" if descending else "") + descriptor["lookup"]
        for descriptor, descending in validated["order_descriptors"]
    ]
    rows = list(qs.order_by(*order_lookups).values(*value_lookups)[:limit])
    computed = [item for item in selected if item["kind"] == "computed"]
    instances = {}
    pk_lookup = fields["id"]["lookup"]
    if computed and rows:
        related = set()
        for item in computed:
            prefix = item["lookup"].split("__")[:-1]
            if prefix:
                related.add("__".join(prefix))
            related.update("__".join([*prefix, name]) for name in item["related"])
        object_qs = qs.select_related(*related) if related else qs
        instances = object_qs.in_bulk([row[pk_lookup] for row in rows])
    records = []
    for row in rows:
        record = {}
        for name, item in zip(selected_names, selected):
            value = row.get(item["lookup"])
            if item["kind"] == "computed":
                method = instances.get(row[pk_lookup])
                for part in name.split("."):
                    method = getattr(method, part, None) if method is not None else None
                value = method() if callable(method) else None
            record[name] = _output_value(value, item)
        records.append(record)
    sources = [{"entity": entity, "id": row["id"]} for row in records if row.get("id") is not None]
    return _with_display({"entity": entity, "query": _query_summary(validated), "total_count": total, "returned_count": len(records), "aggregates": summary, "records": records, "sources": sources, "source_scope": "all matching records" if total == len(sources) else "returned records only; aggregates cover all matching records"}, model, fields)


def _cache_filter(value, operation, expected):
    if operation == "isnull":
        return (value is None) is expected
    if operation == "in":
        return value in expected
    if operation == "eq":
        return value == expected
    if value is None:
        return False
    if operation == "contains":
        return str(expected).lower() in str(value).lower()
    return {"gte": value >= expected, "gt": value > expected, "lte": value <= expected, "lt": value < expected}[operation]


def reuse_result(user, plan, cached):
    """Reuse a complete, same-conversation READ result without touching ORM."""
    validated = validate_plan(user, plan)
    # Computed presentation may depend on fields absent from the cached rows.
    if any(item["kind"] == "computed" for item in validated["selected"]):
        return None
    if not isinstance(cached, dict) or cached.get("entity") != validated["entity"]:
        return None
    if cached.get("total_count") != cached.get("returned_count") or cached.get("grouped"):
        return None
    old_query = cached.get("query") if isinstance(cached.get("query"), dict) else {}
    if old_query.get("view") != validated["view"] or old_query.get("search") != validated["search"]:
        return None
    old_filters = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in old_query.get("filters", [])}
    new_filters = {
        json.dumps({"field": item["field"], "op": item["op"], "value": _json_safe_value(item["value"])}, ensure_ascii=False, sort_keys=True, default=str)
        for item in validated["filters"]
    }
    if not old_filters <= new_filters or old_query.get("order_by") != _query_summary(validated)["order_by"]:
        return None
    required = set(validated["selected_names"])
    required.update(item["field"] for item in validated["filters"])
    required.update(descriptor["name"] for _key, _op, descriptor in validated["aggregate_specs"])
    if not all(required <= set(record) for record in cached.get("records", [])):
        return None
    rows = []
    for record in cached.get("records", []):
        try:
            if all(_cache_filter(
                _path(validated["model"], validated["fields"], item["field"])["field"].to_python(record[item["field"]]),
                item["op"], item["value"],
            ) for item in validated["filters"]):
                rows.append(record)
        except (KeyError, TypeError, ValueError, ValidationError):
            return None
    aggregates = {}
    for key, operation, descriptor in validated["aggregate_specs"]:
        values = [descriptor["field"].to_python(row[descriptor["name"]]) for row in rows if row.get(descriptor["name"]) is not None]
        if operation == "count":
            aggregates[key] = len(values)
        elif operation == "sum":
            aggregates[key] = _value(sum(values)) if values else None
        elif operation == "avg":
            aggregates[key] = _value(sum(values) / len(values)) if values else None
        elif operation == "min":
            aggregates[key] = _value(min(values)) if values else None
        elif operation == "max":
            aggregates[key] = _value(max(values)) if values else None
    records = [{name: row.get(name) for name in validated["selected_names"]} for row in rows[:validated["limit"]]]
    return _with_display({
        "entity": validated["entity"], "query": _query_summary(validated), "total_count": len(rows),
        "returned_count": len(records), "aggregates": aggregates, "records": records,
        "sources": [{"entity": validated["entity"], "id": row["id"]} for row in records if row.get("id") is not None],
        "source_scope": "cached complete result",
    }, validated["model"], validated["fields"])


def _query_summary(validated, *, order_by=None):
    return {
        "entity": validated["entity"], "view": validated["view"], "search": validated["search"],
        "filters": [
            {"field": item["field"], "op": item["op"], "value": _json_safe_value(item["value"])}
            for item in validated["filters"]
        ],
        "order_by": order_by if order_by is not None else [
            ("-" if descending else "") + descriptor["name"]
            for descriptor, descending in validated["order_descriptors"]
        ],
        "group_by": validated["group_names"],
        "limit": validated["limit"],
    }
