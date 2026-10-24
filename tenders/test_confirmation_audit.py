"""Render every published confirmation using synthetic data, without executing actions."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from uuid import UUID

from django.apps import apps
from django.db import models
from django.test import TestCase

from .assistant_tools import _preview_field, _reference_model, available_tools, confirmation_arguments


class ConfirmationDeclarationAuditTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_superuser("audit", "audit@example.test", "password")
        self.objects = {get_user_model(): self.user}

    def record(self, model):
        if model in self.objects:
            return self.objects[model]
        values = {}
        for field in model._meta.concrete_fields:
            if field.primary_key or field.auto_created or getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False):
                continue
            if field.is_relation:
                if not field.null:
                    values[field.name] = self.record(field.related_model)
            elif field.has_default():
                values[field.name] = field.get_default()
            elif field.choices:
                values[field.name] = next(iter(field.flatchoices))[0]
            elif field.null:
                continue
            elif isinstance(field, models.DateTimeField):
                values[field.name] = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
            elif isinstance(field, models.DateField):
                values[field.name] = date(2026, 10, 24)
            elif isinstance(field, models.DecimalField):
                values[field.name] = Decimal("1")
            elif isinstance(field, models.BooleanField):
                values[field.name] = False
            elif isinstance(field, (models.IntegerField, models.FloatField)):
                values[field.name] = 1
            elif isinstance(field, models.JSONField):
                values[field.name] = {}
            elif isinstance(field, models.UUIDField):
                values[field.name] = UUID(int=1)
            else:
                values[field.name] = "Тест"[:getattr(field, "max_length", None)]
            if not field.is_relation and isinstance(field, (models.CharField, models.TextField)) and not field.choices:
                values[field.name] = "Тест"[:getattr(field, "max_length", None)]
        instance = model(**values)
        # No save hooks, services, signals, documents, or background jobs.
        model._default_manager.bulk_create([instance])
        self.objects[model] = instance
        return instance

    def value(self, declaration, payload=None):
        spec = declaration if isinstance(declaration, dict) else {"type": declaration}
        reference = spec.get("reference")
        kind = spec.get("type", "object")
        if reference:
            identifier = self.record(_reference_model(reference, payload or {})).pk
            return [identifier] if kind.endswith("[]") else identifier
        if spec.get("enum"):
            return spec["enum"][0]
        kind = kind.rstrip("?")
        if "|" in kind:
            return kind.split("|")[0]
        if kind.endswith("[]"):
            return [self.value(kind[:-2])]
        if kind in {"integer", "number", "user id for owner"}:
            return 1
        if kind == "boolean":
            return True
        if kind == "object":
            return {"name": "Тестовая позиция", "quantity": 1}
        if kind == "array":
            return [{"name": "Тестовая позиция", "quantity": 1}]
        if kind == "YYYY-MM-DD":
            return "2026-10-24"
        if kind == "YYYY-MM":
            return "2026-10"
        return "Тестовое значение"

    def test_render_every_confirmation_and_preserve_payload(self):
        report = []
        for tool in available_tools(self.user):
            if not tool["requires_confirmation"]:
                continue
            with self.subTest(action=tool["id"]):
                payload = {name: self.value(spec) for name, spec in tool["input_schema"].items()
                           if not isinstance(spec, dict) or not spec.get("reference")}
                for name, spec in tool["input_schema"].items():
                    if isinstance(spec, dict) and spec.get("reference"):
                        payload[name] = self.value(spec, payload)
                payload = {name: payload[name] for name in tool["input_schema"]}
                for name in payload:
                    field = _preview_field(tool, name)
                    if field is not None and getattr(field, "choices", None):
                        payload[name] = next(iter(field.flatchoices))[0]
                    elif isinstance(field, models.DateField):
                        payload[name] = "2026-10-24"
                # These aliases have no matching typed field in the current contract.
                if tool["id"] == "estimate.tender_status":
                    payload["status"] = "pending"
                if tool["id"] == "production.route_revise":
                    payload["scope"] = "route"
                if tool["id"] == "tender.discovery_settings":
                    payload["laws"] = ["fz44", "fz223"]
                if tool["id"].startswith("finance.order_record_") and "manager" in payload:
                    payload["manager"] = str(self.user.pk)
                if tool["id"] == "provider.sewing_price_confirm":
                    payload["currency"] = "RUB"
                if tool["id"] == "production.feedback_decide":
                    payload["batch_id"] = str(self.record(apps.get_model("tenders.Proposal")).batch_id)
                original = deepcopy(payload)
                card = confirmation_arguments(tool, payload)
                self.assertEqual(payload, original)
                self.assertEqual(len(card), len(payload))
                issues = []
                for (name, spec), row in zip(tool["input_schema"].items(), card):
                    if not re.search("[А-Яа-яЁё]", row["label"]):
                        issues.append({"field": name, "kind": "technical_label"})
                    if re.search(r"_ids?$", name) and not isinstance(payload[name], str) and "items" not in row:
                        issues.append({"field": name, "kind": "bare_object_id"})
                    field = _preview_field(tool, name)
                    if field is not None and field.is_relation and "items" not in row:
                        issues.append({"field": name, "kind": "bare_related_value"})
                    kind = spec.get("type", "") if isinstance(spec, dict) else spec
                    choices = spec.get("enum", []) if isinstance(spec, dict) else []
                    if "|" in kind:
                        choices = kind.rstrip("?").split("|")
                    if choices and row.get("value") in choices and not re.search("[А-Яа-яЁё]", str(row["value"])):
                        issues.append({"field": name, "kind": "raw_enum"})
                    if name == "status" and payload[name] == "pending" and row.get("value") == "pending":
                        issues.append({"field": name, "kind": "raw_enum"})
                    if name == "scope" and payload[name] == "route" and row.get("value") == "route":
                        issues.append({"field": name, "kind": "raw_enum"})
                    if name == "laws" and any(i.get("text") == "fz44" for i in row.get("items", [])):
                        issues.append({"field": name, "kind": "raw_enum"})
                variants = []
                for name, spec in tool["input_schema"].items():
                    field = _preview_field(tool, name)
                    choices = list(spec.get("choices", ())) if isinstance(spec, dict) else []
                    if field is not None and getattr(field, "choices", None):
                        choices.extend(field.flatchoices)
                    for value, label in choices:
                        variant = deepcopy(payload)
                        variant[name] = [value] if isinstance(payload[name], list) else value
                        saved = deepcopy(variant)
                        rendered = confirmation_arguments(tool, variant)
                        self.assertEqual(variant, saved)
                        row = rendered[list(tool["input_schema"]).index(name)]
                        self.assertEqual(row.get("value", (row.get("items") or [{}])[0].get("text")), str(label))
                        variants.append({"field": name, "value": value, "label": label})
                    if isinstance(spec, dict) and isinstance(spec.get("reference", {}).get("model"), list):
                        for option in spec["reference"]["model"]:
                            variant = {**payload, **option["when"]}
                            variant[name] = self.value(spec, variant)
                            saved = deepcopy(variant)
                            rendered = confirmation_arguments(tool, variant)
                            self.assertEqual(variant, saved)
                            row = rendered[list(tool["input_schema"]).index(name)]
                            self.assertNotEqual(row["items"][0]["text"], "Объект не найден")
                            variants.append({"field": name, "when": option["when"], "model": option["model"]})
                report.append({"action": tool["id"], "arguments": payload, "card": card,
                               "issues": issues, "variants": variants})
                self.assertFalse(issues, issues)
        self.assertEqual(len(report), len([t for t in available_tools(self.user) if t["requires_confirmation"]]))
        if os.getenv("M112_AUDIT_OUTPUT"):
            Path(os.environ["M112_AUDIT_OUTPUT"]).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
