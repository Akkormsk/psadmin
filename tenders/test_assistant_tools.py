import json
from copy import deepcopy
from datetime import datetime
from importlib import import_module
from django.db import models
from django.contrib.auth import get_user_model
from django.test import TestCase
from datetime import date

from .assistant_tools import AssistantTool, AssistantToolConfirmationRequired, available_areas, available_capability_map, available_capabilities, available_tools, capability_issues, capability_map_text, confirmation_arguments, execute_tool, openai_functions, register_tool, required_input_names
from .models import CatalogMatchDecision, ProcessDefinition, TenderKnowledgeSource


def _legacy_filter_contracts(paths):
    """The pre-compression READ variants, kept only as a contract-test oracle."""
    from .assistant_read import MAX_ROWS, NUMERIC_TYPES, _field_value_contract

    result = []
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
        for operation in sorted(operations):
            result.append({
                "type": "object", "required": True, "additional_properties": False,
                "properties": {
                    "field": {"type": "string", "enum": [name], "description": str(field.verbose_name)},
                    "op": {"type": "string", "enum": [operation]},
                    "value": "boolean" if operation == "isnull" else (in_value if operation == "in" else value),
                },
            })
    return result


def _expanded_filter_contracts(contracts):
    """Expand an operator enum to compare contracts, not their representation."""
    return {
        (
            field,
            operation,
            json.dumps(item["properties"]["value"], ensure_ascii=False, sort_keys=True),
        )
        for item in contracts
        for field in item["properties"]["field"]["enum"]
        for operation in item["properties"]["op"]["enum"]
    }


def _m3_filter_contracts(paths):
    grouped = {}
    for item in _legacy_filter_contracts(paths):
        field = item["properties"]["field"]["enum"][0]
        key = (field, json.dumps(item["properties"]["value"], ensure_ascii=False, sort_keys=True))
        if key not in grouped:
            grouped[key] = deepcopy(item)
        else:
            grouped[key]["properties"]["op"]["enum"].append(item["properties"]["op"]["enum"][0])
    return list(grouped.values())


def _m3_aggregate_contracts(paths):
    from .assistant_read import _catalog_field

    return [
        {"type": "object", "required": True, "additional_properties": False, "properties": {
            "op": {"type": "string", "enum": _catalog_field(name, descriptor)["aggregate_ops"]},
            "field": {"type": "string", "enum": [name]},
        }}
        for name, descriptor in paths.items()
        if _catalog_field(name, descriptor)["aggregate_ops"]
    ]


def _m3_read_parameters(entity):
    from .assistant_read import _entity, _paths, input_schema
    from .assistant_tools import _function_definition

    schema = deepcopy(input_schema(entity))
    model, _canonical, fields = _entity(entity)
    paths = _paths(model, fields)
    schema["plan"]["properties"].pop("entity", None)
    schema["plan"]["properties"]["filters"]["item"] = {"any_of": _m3_filter_contracts(paths)}
    scalar_paths = {name: descriptor for name, descriptor in paths.items() if descriptor["kind"] == "scalar"}
    schema["plan"]["properties"]["aggregates"]["item"] = {"any_of": _m3_aggregate_contracts(scalar_paths)}
    _name, definition = _function_definition("read:baseline", "", schema)
    return definition["function"]["parameters"]


def _schema_sample(schema):
    if "anyOf" in schema:
        return _schema_sample(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        return "value"
    if kind == "boolean":
        return False
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.5
    if kind == "null":
        return None
    if kind == "array":
        return [_schema_sample(schema.get("items", {})) for _index in range(schema.get("minimum", 0))]
    if kind == "object":
        return {name: _schema_sample(item) for name, item in schema.get("properties", {}).items()}
    return "value"


def _schema_accepts(value, schema):
    if "anyOf" in schema:
        return any(_schema_accepts(value, item) for item in schema["anyOf"])
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "array":
        return (
            isinstance(value, list)
            and schema.get("minimum", 0) <= len(value) <= schema.get("maximum", float("inf"))
            and all(_schema_accepts(item, schema.get("items", {})) for item in value)
        )
    if kind == "object":
        if not isinstance(value, dict) or any(name not in value for name in schema.get("required", [])):
            return False
        if not schema.get("additionalProperties", True) and set(value) - set(schema.get("properties", {})):
            return False
        return all(_schema_accepts(item, schema["properties"][name]) for name, item in value.items())
    return True


class AssistantToolRegistryTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("assistant-admin", "assistant@example.test", "password")
        self.user = get_user_model().objects.create_user("assistant-user")
        self.stage, _ = ProcessDefinition.objects.get_or_create(name="Швейное производство под ключ", role="production")

    def test_published_capability_sources_resolve_to_shared_callables(self):
        self.assertEqual(capability_issues(self.admin), [])

    def test_area_catalogue_is_assembled_from_registered_capabilities(self):
        from .assistant_tools import available_areas, available_resources

        areas = {item["id"]: item for item in available_areas(self.admin)}
        self.assertEqual(areas["cash"]["name"], "Касса")
        self.assertEqual(
            set(areas),
            {"calculator", "cash", "payroll", "finance", "tenders", "tender_selection"},
        )
        self.assertNotIn("schema", areas)
        self.assertNotIn("action", areas)
        expected_resources = {
            "calculator": {"calculator.estimate_groups", "calculator.estimates", "calculator.price_items", "calculator.production_rules", "calculator.settings"},
            "cash": {"cash.bank_payments", "cash.reconciliations", "cash.transactions"},
            "finance": {"finance.kpi_rates", "finance.kpi_tiers", "finance.manager_settings", "finance.operating_expenses", "finance.payroll_lines", "finance.period_expenses", "finance.periods"},
            "payroll": {"finance.kpi_rates", "finance.kpi_tiers", "finance.manager_settings", "finance.payroll_lines", "payroll.order_records"},
            "tenders": {"tenders.catalog_categories", "tenders.catalog_products", "tenders.catalog_suppliers", "tenders.counterparties", "tenders.lessons", "tenders.order_estimates", "tenders.order_lines", "tenders.orders", "tenders.production_stages", "tenders.production_types", "tenders.settings", "tenders.stage_counterparties", "tenders.tender_estimates", "tenders.tender_lines"},
            "tender_selection": {"tender_selection.contract_statistics", "tender_selection.customers", "tender_selection.filter_settings", "tender_selection.tenders"},
        }
        for area_id, identifiers in expected_resources.items():
            resources = {item["id"]: item for item in available_resources(self.admin, area_id)}
            self.assertEqual(set(resources), identifiers)
        self.assertEqual(
            next(item for item in available_resources(self.admin, "cash") if item["id"] == "cash.transactions")["entity"],
            "cash.CashTransaction",
        )

    def test_each_registered_resource_resolves_through_universal_read_metadata(self):
        from django.apps import apps
        from .assistant_read import resolve_entity
        from .assistant_tools import available_resources

        for resource in available_resources(self.admin):
            self.assertEqual(resolve_entity(resource["entity"]), resource["entity"])
            model = apps.get_model(resource["entity"])
            technical_labels = [
                field.name for field in model._meta.fields
                if str(field.verbose_name) == field.name.replace("_", " ")
            ]
            self.assertEqual(technical_labels, [], resource["entity"])

    def test_read_resources_are_hidden_when_universal_read_is_not_permitted(self):
        from .assistant_tools import available_resources

        self.assertEqual(available_resources(self.user), [])

    def test_each_resource_opens_a_read_contract_without_a_model_identifier(self):
        from .assistant_tools import available_resources

        for area in available_areas(self.admin):
            for resource in available_resources(self.admin, area["id"]):
                definitions, names = openai_functions(
                    self.admin, area_id=area["id"], resource_id=resource["id"],
                )
                read = next(
                    item["function"] for item in definitions
                    if names[item["function"]["name"]] == f"read:{resource['id']}"
                )
                self.assertNotIn("entity", read["parameters"]["properties"]["plan"]["properties"])

    def test_openai_tools_start_with_plan_and_finish_not_django_entities(self):
        definitions, names = openai_functions(self.admin)

        self.assertEqual(
            {names[item["function"]["name"]] for item in definitions},
            {"assistant.plan", "assistant.ask", "assistant.cancel", "assistant.finish"},
        )

        finish = next(item["function"] for item in definitions if names[item["function"]["name"]] == "assistant.finish")
        self.assertTrue(finish["strict"])
        self.assertEqual(finish["parameters"], {
            "type": "object", "properties": {"answer": {"type": "string", "minLength": 1}},
            "required": ["answer"], "additionalProperties": False,
        })

    def test_plan_schema_limits_each_step_to_registered_capabilities(self):
        definitions, names = openai_functions(self.admin)
        plan = next(item["function"] for item in definitions if names[item["function"]["name"]] == "assistant.plan")
        capability = plan["parameters"]["properties"]["plan"]["properties"]["steps"]["items"]["properties"]["capability_id"]

        expected = {
            item["id"]
            for area in available_areas(self.admin)
            for item in available_capabilities(self.admin, area["id"])
        }
        self.assertEqual(set(capability["enum"]), expected)
        self.assertNotIn("calculator", capability["enum"])

    def test_capability_map_exposes_all_permitted_capabilities_without_schemas(self):
        capability_map = available_capability_map(self.admin)
        cash = next(item for item in capability_map if item["id"] == "cash")
        transaction_read = next(item for item in cash["capabilities"] if item["id"] == "read:cash.transactions")
        transaction_create = next(item for item in cash["capabilities"] if item["id"] == "cash.transaction_create")

        self.assertEqual(transaction_read["kind"], "read")
        self.assertIn("count/sum/avg/min/max", transaction_read["description"])
        self.assertTrue(transaction_create["requires_confirmation"])
        self.assertEqual(
            set(transaction_read),
            {"id", "kind", "name", "description", "requires_confirmation"},
        )
        self.assertNotIn("input_schema", json.dumps(capability_map, ensure_ascii=False))
        self.assertNotIn("properties", json.dumps(capability_map, ensure_ascii=False))
        rendered = capability_map_text(capability_map)
        self.assertIn("read:cash.transactions", rendered)
        self.assertLess(len(rendered.encode()), len(json.dumps(capability_map, ensure_ascii=False).encode()))

    def test_capability_map_updates_from_registry_and_hides_unavailable_tools(self):
        from . import assistant_tools

        public = AssistantTool(
            "test.map_public", "Тестовая публичная возможность", "Проверка динамической карты.",
            {"value": "string"}, True, False, False, lambda _user, _payload: {}, areas=("cash",),
        )
        private = AssistantTool(
            "test.map_private", "Тестовая закрытая возможность", "Не должна попасть обычному пользователю.",
            {"value": "string"}, True, False, True, lambda _user, _payload: {}, areas=("cash",),
        )
        try:
            register_tool(public)
            register_tool(private)
            admin_ids = {
                item["id"] for area in available_capability_map(self.admin)
                for item in area["capabilities"]
            }
            user_ids = {
                item["id"] for area in available_capability_map(self.user)
                for item in area["capabilities"]
            }
            self.assertIn(public.identifier, admin_ids)
            self.assertIn(private.identifier, admin_ids)
            self.assertIn(public.identifier, user_ids)
            self.assertNotIn(private.identifier, user_ids)
        finally:
            assistant_tools._TOOLS.pop(public.identifier, None)
            assistant_tools._TOOLS.pop(private.identifier, None)

    def test_confirmation_preview_resolves_references_and_omits_empty_arguments(self):
        tool = next(item for item in available_tools(self.admin) if item["id"] == "provider.create")

        self.assertEqual(confirmation_arguments(tool, {
            "name": "Атекс", "stage_ids": [self.stage.pk], "source_text": None,
        }), [
            {"label": "Название", "value": "Атекс"},
            {"label": str(ProcessDefinition._meta.verbose_name_plural), "items": [{"text": self.stage.name, "hint": f"ID {self.stage.pk}"}]},
        ])

    def test_reference_preview_metadata_does_not_leak_into_function_schema(self):
        definitions, _names = openai_functions(
            self.admin, area_id="tenders", capability_id="provider.create",
        )
        schema = next(
            item["function"]["parameters"] for item in definitions
            if item["function"]["name"] == "provider_create"
        )

        self.assertEqual(schema["properties"]["stage_ids"], {
            "type": "array", "items": {"type": "integer"},
        })

    def test_model_instructions_do_not_change_confirmation_for_different_actions(self):
        from copy import deepcopy
        from tender_selection.models import Tender
        tender = Tender.objects.create(purchase_number="UI-001", title="Проверка карточки")
        payloads = {
            "cash.transaction_create": {"operation_date": "2026-10-24", "account": "cash",
                "direction": "expense", "amount": 175, "reason": "Бумага"},
            "provider.create": {"name": "Мастерская", "stage_ids": [self.stage.pk]},
            "tender.send_to_calculation": {"tender_id": tender.pk},
        }
        tools = {tool["id"]: tool for tool in available_tools(self.admin)}
        for identifier, payload in payloads.items():
            with self.subTest(action=identifier):
                tool = tools[identifier]
                expected = confirmation_arguments(tool, payload)
                changed = deepcopy(tool)
                for name, declaration in changed["input_schema"].items():
                    if not isinstance(declaration, dict):
                        declaration = {"type": declaration}
                        changed["input_schema"][name] = declaration
                    declaration["description"] = "Техническая инструкция для модели: используйте READ"
                self.assertEqual(confirmation_arguments(changed, payload), expected)

    def test_new_action_confirmation_needs_no_central_field_or_action_mapping(self):
        from uuid import uuid4
        from . import assistant_tools
        identifier = "extension." + uuid4().hex
        field = "parameter_" + uuid4().hex
        description = "Техническая инструкция, которая не должна попадать в карточку"
        register_tool(AssistantTool(identifier, "Новое действие", "Описание действия", {
            field: {"type": "string", "required": True, "label": "Понятное название параметра",
                    "description": description},
        }, False, True, True, lambda user, payload: payload))
        try:
            tool = next(t for t in available_tools(self.admin) if t["id"] == identifier)
            payload = {field: "Проверенное значение"}
            self.assertEqual(confirmation_arguments(tool, payload), [{
                "label": "Понятное название параметра", "value": "Проверенное значение",
            }])
            self.assertFalse(hasattr(assistant_tools, "_PREVIEW_LABELS"))
            self.assertEqual(payload, {field: "Проверенное значение"})
        finally:
            assistant_tools._TOOLS.pop(identifier)

    def test_confirmation_uses_existing_model_field_labels_and_choices(self):
        from unittest.mock import patch
        from django.test.utils import isolate_apps
        from uuid import uuid4
        field_name = "state_" + uuid4().hex
        with isolate_apps("tenders"):
            record = type("FutureRecord", (models.Model,), {
                "__module__": "tenders.models",
                field_name: models.CharField("Состояние проверки", max_length=20,
                    choices=[("new-code", "Ожидает проверки")]),
            })
            tool = {"ui": {"model": "tenders.FutureRecord"},
                    "input_schema": {field_name: {"type": "string",
                        "description": "Техническая инструкция", "enum": ["new-code"]}}}
            with patch("tenders.assistant_tools.apps.get_model", return_value=record):
                self.assertEqual(confirmation_arguments(tool, {field_name: "new-code"}), [{
                    "label": "Состояние проверки", "value": "Ожидает проверки",
                }])
        from . import assistant_tools
        self.assertFalse(hasattr(assistant_tools, "_PREVIEW_ENUMS"))

    def test_unplanned_turn_exposes_plan_or_explicit_chat_not_questions(self):
        definitions, names = openai_functions(self.admin, planned=False)

        self.assertEqual(set(names.values()), {"assistant.plan", "assistant.chat"})
        self.assertEqual(len(definitions), 2)

    def test_dialogue_functions_have_strict_schemas_and_state_specific_availability(self):
        for planned, expected in ((False, {"assistant.chat"}), (True, {"assistant.ask", "assistant.cancel", "assistant.finish"})):
            definitions, names = openai_functions(self.admin, planned=planned)
            controls = {names[item["function"]["name"]]: item["function"] for item in definitions
                        if names[item["function"]["name"]] != "assistant.plan"}
            self.assertEqual(set(controls), expected)
            for tool in controls.values():
                self.assertTrue(tool["strict"])
                schema = tool["parameters"]
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                sample = _schema_sample(schema)
                self.assertTrue(_schema_accepts(sample, schema))
                self.assertFalse(_schema_accepts({**sample, "extra": 1}, schema))

    def test_plan_schema_is_strict_and_has_structured_steps(self):
        definitions, names = openai_functions(self.admin, planned=False)
        plan = next(item["function"] for item in definitions if names[item["function"]["name"]] == "assistant.plan")
        schema = plan["parameters"]["properties"]["plan"]

        self.assertTrue(plan["strict"])
        self.assertEqual(set(schema["properties"]), {"goal", "steps", "revision_reason"})
        self.assertEqual({item["type"] for item in schema["properties"]["revision_reason"]["anyOf"]}, {"string", "null"})
        self.assertIn("revision_reason", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        step = schema["properties"]["steps"]["items"]
        self.assertEqual(set(step["properties"]), {"id", "capability_id", "objective", "expected_result"})
        self.assertFalse(step["additionalProperties"])

    def test_selected_capability_is_the_only_detailed_action_schema(self):
        definitions, names = openai_functions(
            self.admin, area_id="cash", capability_id="cash.transaction_create",
        )

        self.assertEqual(names["cash_transaction_create"], "cash.transaction_create")
        self.assertNotIn("cash_transaction_delete", names)
        self.assertNotIn("read:cash.transactions", names.values())

    def test_opened_resource_publishes_a_specialized_universal_read(self):
        definitions, names = openai_functions(
            self.admin, area_id="cash", resource_id="cash.transactions",
        )

        self.assertIn("read:cash.transactions", names.values())
        read = next(item["function"] for item in definitions if names[item["function"]["name"]] == "read:cash.transactions")
        plan = read["parameters"]["properties"]["plan"]
        self.assertNotIn("entity", plan["properties"])
        self.assertIn("direction", str(plan))

    def test_workflow_encyclopedia_reuses_only_registered_capabilities(self):
        from .assistant_tools import catalog, workflow_catalog

        actions = available_tools(self.admin)
        workflows = workflow_catalog(self.admin, actions)
        published = {action["id"] for action in actions}
        grouped = [tool_id for goal in workflows for tool_id in goal["actions"]]
        self.assertEqual(set(grouped), published)
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(catalog(self.admin)["workflows"], workflows)
        self.assertTrue(next(goal for goal in workflows if goal["id"] == "cash")["requires_confirmation"])

    def test_openai_definitions_are_generated_from_the_registry(self):
        definitions, names = openai_functions(self.admin, area_id="cash", resource_id="cash.transactions")
        self.assertTrue(all(item["function"]["name"] in names for item in definitions))
        self.assertNotIn("cash_transaction_update", names)
        self.assertTrue(all(item["function"]["parameters"]["additionalProperties"] is False for item in definitions))
        read = next(item["function"] for item in definitions if names[item["function"]["name"]] == "read:cash.transactions")
        plan = read["parameters"]["properties"]["plan"]
        self.assertNotIn("entity", plan["properties"])
        self.assertIn("filters", plan["properties"])
        self.assertIn("items", plan["properties"]["filters"]["anyOf"][0])
        self.assertEqual(set(plan["properties"]), set(plan["required"]))
        filters = plan["properties"]["filters"]["anyOf"][0]["items"]["anyOf"]
        direction = next(item for item in filters if (
            "direction" in item["properties"]["field"].get("enum", [])
            and item["properties"]["op"]["enum"] == ["contains", "eq"]
        ))
        account = next(item for item in filters if (
            "account" in item["properties"]["field"].get("enum", [])
            and item["properties"]["op"]["enum"] == ["contains", "eq"]
        ))
        self.assertEqual(direction["properties"]["value"]["enum"], ["income", "expense"])
        self.assertEqual(account["properties"]["value"]["enum"], ["cash", "card"])
        self.assertIn("Приход", direction["properties"]["value"]["description"])
        self.assertIn("Карта", account["properties"]["value"]["description"])
        self.assertNotEqual(direction["properties"]["value"], account["properties"]["value"])
        self.assertNotIn("schema_describe", names)
        self.assertNotIn("action_describe", names)

    def test_read_filter_schema_compresses_equivalent_operator_variants(self):
        from .assistant_read import _entity, _filter_contracts, _paths
        from .assistant_tools import available_resources

        resources = available_resources(self.admin)
        for entity in {resource["entity"] for resource in resources}:
            model, _canonical, fields = _entity(entity)
            paths = _paths(model, fields)
            legacy = _legacy_filter_contracts(paths)
            compressed = _filter_contracts(paths)

            self.assertEqual(_expanded_filter_contracts(compressed), _expanded_filter_contracts(legacy))

        for resource in resources:
            definitions, names = openai_functions(
                self.admin, area_id=resource["areas"][0], resource_id=resource["id"],
            )
            read = next(
                item["function"] for item in definitions
                if names[item["function"]["name"]] == f"read:{resource['id']}"
            )
            self.assertTrue(read.get("strict"), resource["id"])

        model, _canonical, fields = _entity("cash.CashTransaction")
        filters = _filter_contracts(_paths(model, fields))
        self.assertLess(len(filters), len(_legacy_filter_contracts(_paths(model, fields))))
        direction = next(item for item in filters if (
            "direction" in item["properties"]["field"]["enum"]
            and item["properties"]["op"]["enum"] == ["contains", "eq"]
        ))
        amount = next(item for item in filters if (
            "amount" in item["properties"]["field"]["enum"]
            and item["properties"]["op"]["enum"] == ["eq", "gt", "gte", "lt", "lte"]
        ))
        self.assertEqual(direction["properties"]["op"]["enum"], ["contains", "eq"])
        self.assertEqual(amount["properties"]["op"]["enum"], ["eq", "gt", "gte", "lt", "lte"])

    def test_specialized_read_schema_keeps_values_and_rejects_invalid_filter_shapes(self):
        from .assistant_read import ReadPlanError, validate_plan

        definitions, names = openai_functions(self.admin, area_id="cash", resource_id="cash.transactions")
        read = next(item["function"] for item in definitions if names[item["function"]["name"]] == "read:cash.transactions")
        self.assertTrue(read["strict"])
        filter_schema = read["parameters"]["properties"]["plan"]["properties"]["filters"]
        self.assertTrue(_schema_accepts(None, filter_schema))
        filters = filter_schema["anyOf"][0]["items"]["anyOf"]

        def accepts(value):
            return any(_schema_accepts(value, item) for item in filters)

        self.assertTrue(accepts({"field": "direction", "op": "eq", "value": "income"}))
        self.assertTrue(accepts({"field": "direction", "op": "contains", "value": "income"}))
        self.assertTrue(accepts({"field": "amount", "op": "in", "value": [1, "2.50"]}))
        self.assertTrue(accepts({"field": "operation_date", "op": "isnull", "value": True}))
        self.assertFalse(accepts({"field": "direction", "op": "gte", "value": "income"}))
        self.assertFalse(accepts({"field": "direction", "op": "eq", "value": "other"}))
        self.assertFalse(accepts({"field": "amount", "op": "in", "value": 1}))
        self.assertFalse(accepts({"field": "operation_date", "op": "isnull", "value": "true"}))

        for filter_item in (
            {"field": "direction", "op": "eq", "value": "income"},
            {"field": "amount", "op": "in", "value": [1, "2.50"]},
            {"field": "operation_date", "op": "isnull", "value": True},
        ):
            validate_plan(self.admin, {"entity": "cash.CashTransaction", "filters": [filter_item]})
        with self.assertRaises(ReadPlanError):
            validate_plan(self.admin, {"entity": "cash.CashTransaction", "filters": [{"field": "direction", "op": "gte", "value": "income"}]})

        definitions, names = openai_functions(self.admin, area_id="tenders", resource_id="tenders.tender_estimates")
        read = next(item["function"] for item in definitions if names[item["function"]["name"]] == "read:tenders.tender_estimates")
        nested_filters = read["parameters"]["properties"]["plan"]["properties"]["filters"]["anyOf"][0]["items"]["anyOf"]
        self.assertTrue(any(
            _schema_accepts({"field": "tender.outcome_status", "op": "in", "value": ["lost", "won"]}, item)
            for item in nested_filters
        ))
        self.assertFalse(any(
            _schema_accepts({"field": "tender.outcome_status", "op": "eq", "value": "Проигран"}, item)
            for item in nested_filters
        ))

    def test_read_schema_groups_fields_without_changing_request_acceptance(self):
        from .assistant_tools import available_resources

        def samples(variants, *, with_value):
            for variant in variants:
                properties = variant["properties"]
                for field in properties["field"]["enum"]:
                    for operation in properties["op"]["enum"]:
                        item = {
                            "field": field,
                            "op": operation,
                        }
                        if with_value:
                            item["value"] = _schema_sample(properties["value"])
                        yield item

        def accepts(parameters, property_name, item):
            plan_schema = parameters["properties"]["plan"]
            plan = {name: None for name in plan_schema["properties"]}
            plan[property_name] = [item]
            return _schema_accepts({"plan": plan}, parameters)

        total_old_filters = total_new_filters = 0
        total_old_aggregates = total_new_aggregates = 0
        for resource in available_resources(self.admin):
            baseline = _m3_read_parameters(resource["entity"])
            definitions, names = openai_functions(
                self.admin, area_id=resource["areas"][0], resource_id=resource["id"],
            )
            current = next(
                item["function"]["parameters"] for item in definitions
                if names[item["function"]["name"]] == f"read:{resource['id']}"
            )
            self.assertTrue(next(
                item["function"] for item in definitions
                if names[item["function"]["name"]] == f"read:{resource['id']}"
            ).get("strict"), resource["id"])

            baseline_plan = baseline["properties"]["plan"]
            current_plan = current["properties"]["plan"]
            old_filters = baseline_plan["properties"]["filters"]["anyOf"][0]["items"]["anyOf"]
            new_filters = current_plan["properties"]["filters"]["anyOf"][0]["items"]["anyOf"]
            old_aggregates = baseline_plan["properties"]["aggregates"]["anyOf"][0]["items"]["anyOf"]
            new_aggregates = current_plan["properties"]["aggregates"]["anyOf"][0]["items"]["anyOf"]
            total_old_filters += len(old_filters)
            total_new_filters += len(new_filters)
            total_old_aggregates += len(old_aggregates)
            total_new_aggregates += len(new_aggregates)

            for item in [*samples(old_filters, with_value=True), *samples(new_filters, with_value=True)]:
                self.assertTrue(accepts(baseline, "filters", item), (resource["id"], item, "baseline"))
                self.assertTrue(accepts(current, "filters", item), (resource["id"], item, "current"))
            for item in [*samples(old_aggregates, with_value=False), *samples(new_aggregates, with_value=False)]:
                self.assertTrue(accepts(baseline, "aggregates", item), (resource["id"], item, "baseline"))
                self.assertTrue(accepts(current, "aggregates", item), (resource["id"], item, "current"))

            first_filter = next(samples(old_filters, with_value=True))
            for item in (
                {**first_filter, "field": "not_a_field"},
                {**first_filter, "op": "not_an_operation"},
                {**first_filter, "value": None},
                {"field": first_filter["field"], "op": first_filter["op"]},
            ):
                self.assertFalse(accepts(baseline, "filters", item), (resource["id"], item, "baseline"))
                self.assertFalse(accepts(current, "filters", item), (resource["id"], item, "current"))

        self.assertLess(total_new_filters, total_old_filters)
        self.assertLess(total_new_aggregates, total_old_aggregates)

    def test_no_navigation_tool_is_published_before_context_is_known(self):
        definitions, names = openai_functions(self.admin)

        self.assertNotIn("read", names)
        self.assertNotIn("cash_transaction_create", names)
        self.assertEqual(len(definitions), 4)
        self.assertNotIn("assistant_select_area", [item["function"]["name"] for item in definitions])
        self.assertNotIn("assistant_select_capability", [item["function"]["name"] for item in definitions])

    def test_opened_area_does_not_publish_navigation_or_action_schemas(self):
        definitions, names = openai_functions(self.admin, area_id="cash")

        self.assertNotIn("cash_transaction_create", names)
        self.assertEqual(names["assistant_select_capability"], "assistant.select_capability")
        self.assertEqual(len(definitions), 4)
        self.assertNotIn("assistant_select_capability", [item["function"]["name"] for item in definitions])

    def test_compact_capabilities_are_views_of_the_existing_registry(self):
        capabilities = available_capabilities(self.admin, "cash")

        self.assertTrue(any(item["id"] == "read:cash.transactions" and item["kind"] == "read" for item in capabilities))
        action = next(item for item in capabilities if item["id"] == "cash.transaction_create")
        self.assertEqual(set(action), {"id", "kind", "name", "description", "requires_confirmation"})
        self.assertTrue(action["requires_confirmation"])

    def test_compact_capability_selection_is_smaller_than_all_area_action_schemas(self):
        from .assistant_tools import _function_definition, available_resources

        current, _names = openai_functions(self.admin, area_id="cash")
        _name, finish = _function_definition(
            "assistant.finish", "Передаёт окончательный ответ или уточняющий вопрос пользователю после завершения работы. Не используй для промежуточных результатов инструментов.",
            {"answer": {"type": "string", "minLength": 1}},
        )
        areas = available_areas(self.admin)
        _name, select_area = _function_definition(
            "assistant.select_area", "Выбирает раздел PSADMIN для запроса к данным или операции. Используй, если пользователь прямо назвал раздел или его нужно определить по смыслу запроса.",
            {"area": {"type": "string", "enum": [item["id"] for item in areas],
                      "description": "; ".join(f'{item["id"]}: {item["name"]}' for item in areas)}},
        )
        resources = available_resources(self.admin, "cash")
        _name, select_resource = _function_definition(
            "assistant.select_resource", "Выбирает данные, с которыми нужно работать внутри текущего раздела. Используй перед чтением данных.",
            {"resource": {"type": "string", "enum": [item["id"] for item in resources],
                          "description": "; ".join(f'{item["id"]}: {item["name"]} — {item["description"]}' for item in resources)}},
        )
        legacy = [finish, select_area, select_resource]
        for tool in available_tools(self.admin):
            if tool["available"] and tool["id"] != "read" and "cash" in tool["areas"]:
                _name, definition = _function_definition(tool["id"], tool["description"], deepcopy(tool["input_schema"]))
                legacy.append(definition)

        current_bytes = len(json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode())
        legacy_bytes = len(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLess(current_bytes, legacy_bytes)

    def test_read_tool_publishes_the_same_contract_its_validator_uses(self):
        from .assistant_read import ReadPlanError, plan_contract, validate_plan

        read = next(tool for tool in available_tools(self.admin) if tool["id"] == "read")
        self.assertEqual(read["input_schema"], {"plan": plan_contract()})
        self.assertTrue(read["input_schema"]["plan"]["properties"]["entity"]["required"])
        self.assertNotIn("model", read["input_schema"]["plan"]["properties"])
        with self.assertRaisesRegex(ReadPlanError, "Недопустимые параметры READ: model"):
            validate_plan(self.admin, {"model": "cash.CashTransaction", "fields": ["amount"]})

    def test_schema_description_uses_the_same_metadata_as_read(self):
        from .assistant_read import describe_entity

        description = describe_entity("tenders.TenderEstimate")

        self.assertEqual(description["entity"], "tenders.TenderEstimate")
        owner = next(field for field in description["fields"] if field["name"] == "owner")
        self.assertEqual(owner["relation"], get_user_model()._meta.label)
        self.assertIn("username", {field["name"] for field in owner["fields"]})

    def test_read_optional_nulls_use_the_documented_defaults(self):
        from .assistant_read import validate_plan

        validated = validate_plan(self.admin, {
            "entity": "cash.CashTransaction", "fields": None, "order_by": None,
            "limit": None, "filters": None, "aggregates": None, "group_by": None,
        })

        self.assertEqual(validated["limit"], 20)
        self.assertEqual(validated["order_descriptors"][0][0]["name"], "id")

    def test_optional_function_argument_null_is_normalized_before_an_action_executor(self):
        from cash.models import CashReconciliation

        execute_tool("cash.reconciliation_save", self.admin, {
            "effective_date": "2026-10-18", "cash_balance": 1, "card_balance": 2, "note": None,
        }, confirmed=True)

        self.assertEqual(CashReconciliation.objects.get(effective_date=date(2026, 10, 18)).note, "")

    def test_read_unknown_field_explains_the_allowed_fields(self):
        with self.assertRaisesRegex(Exception, "Допустимые поля:.*amount"):
            execute_tool("read", self.admin, {"plan": {
                "entity": "cash.CashTransaction", "fields": ["not_a_field"],
            }})

    def test_read_rejects_unknown_top_level_parameter(self):
        with self.assertRaisesRegex(Exception, "Недопустимые параметры действия: unexpected"):
            execute_tool("read", self.admin, {"plan": {"entity": "cash.CashTransaction"}, "unexpected": True})

    def test_action_contract_rejects_false_strings_and_wrong_array_shapes(self):
        with self.assertRaisesRegex(Exception, "true или false"):
            execute_tool("finance.period_lock", self.admin, {
                "period": "2026-10", "closed": "false",
            }, confirmed=True)
        with self.assertRaisesRegex(Exception, "списком"):
            execute_tool("provider.production_create", self.admin, {
                "name": "Тест", "stage_ids": "1,2",
            }, confirmed=True)
        with self.assertRaisesRegex(Exception, "объектом"):
            execute_tool("read", self.admin, {"plan": []})

    def test_local_audit_command_is_safe_and_reports_registered_actions(self):
        from io import StringIO
        from django.core.management import call_command
        output = StringIO()
        call_command("audit_assistant_capabilities", "--strict", stdout=output)
        self.assertIn("Registered actions:", output.getvalue())
        self.assertIn("Registry references: OK", output.getvalue())

    def test_inventory_describes_real_registered_tools_and_permissions(self):
        admin_tools = {item["id"]: item for item in available_tools(self.admin)}
        user_tools = {item["id"]: item for item in available_tools(self.user)}

        self.assertIn("cash.transaction_create", admin_tools)
        self.assertTrue(admin_tools["cash.transaction_create"]["requires_confirmation"])
        self.assertFalse(user_tools["cash.transaction_create"]["available"])
        self.assertTrue({"provider.find", "process.list", "provider.create_draft", "provider.calculator_schema", "provider.calculate"}.isdisjoint(admin_tools))

    def test_required_fields_checked_after_confirmation(self):
        from cash.models import CashTransaction
        with self.assertRaisesRegex(Exception, "обязательные поля: amount"):
            execute_tool("cash.transaction_create", self.admin, {
                "operation_date": "2026-10-12", "account": "cash",
                "direction": "expense", "reason": "Доставка",
            }, confirmed=True)
        self.assertFalse(CashTransaction.objects.exists())

    def test_counterparty_creation_reuses_the_provider_form_workflow(self):
        from unittest.mock import patch
        from .models import Counterparty, CounterpartyKnowledgeVersion

        payload = {"name": "Новый поставщик", "stage_ids": [self.stage.pk], "source_text": "Прайс на пошив"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.create", self.admin, payload)
        with patch("tenders.services.extract_calculation_source", return_value={"content": "Прайс на пошив"}), \
                patch("tenders.services.parse_counterparty_draft", return_value={"summary": "Прайс", "ambiguities": []}):
            result = execute_tool("provider.create", self.admin, payload, confirmed=True)

        provider = Counterparty.objects.get(pk=result["counterparty_id"])
        self.assertEqual(provider.name, "Новый поставщик")
        self.assertTrue(provider.stage_links.filter(stage=self.stage).exists())
        self.assertEqual(CounterpartyKnowledgeVersion.objects.get(pk=result["knowledge_version_id"]).status, CounterpartyKnowledgeVersion.STATUS_DRAFT)

    def test_production_lesson_deactivation_uses_shared_service(self):
        from unittest.mock import patch
        from .models import Lesson

        Lesson.objects.create(pk=37, summary="Проверенное правило")

        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("production.lesson_deactivate", self.admin, {"lesson_id": 37})
        with patch("tenders.assistant_capabilities.deactivate_lesson", return_value=True) as service:
            result = execute_tool("production.lesson_deactivate", self.admin, {"lesson_id": 37}, confirmed=True)
        service.assert_called_once_with(37)
        self.assertTrue(result["dropped"])

    def test_requirement_skip_restore_uses_the_shared_route_executor(self):
        from .models import RequirementSkipRule

        RequirementSkipRule.objects.create(label_normalized="формат", label="Формат")
        result = execute_tool("production.requirement_skip_restore", self.admin, {
            "label": "Формат",
        }, confirmed=True)

        self.assertTrue(result["dropped"])
        self.assertFalse(RequirementSkipRule.objects.get(label_normalized="формат").is_active)

    def test_route_build_uses_the_same_executor_as_the_route_screen(self):
        from unittest.mock import patch
        from .models import ProductionTrainingSession

        payload = {"line": {"name": "Папка", "quantity": 100, "requirements": {}}}
        with patch("tenders.views._submit_assistant_job") as submit:
            result = execute_tool("production.route_build", self.admin, payload, confirmed=True)

        session = ProductionTrainingSession.objects.get(pk=result["session_id"])
        self.assertEqual(result["status"], "processing")
        self.assertEqual(session.position_name, "Папка")
        self.assertEqual(submit.call_args.args[0], session.pk)

    def test_route_revision_uses_the_shared_route_executor(self):
        from unittest.mock import patch
        from .models import ProductionTrainingSession

        session = ProductionTrainingSession.objects.create(
            created_by=self.admin, position_name="Папка", requirements={},
            current_hypothesis={"route": {"name": "Черновик"}},
        )
        payload = {
            "session_id": session.pk,
            "line": {"name": "Папка", "quantity": 100, "requirements": {}},
            "feedback": "Уточнить материал",
        }
        with patch("tenders.views._submit_assistant_job") as submit:
            result = execute_tool("production.route_revise", self.admin, payload, confirmed=True)

        self.assertEqual(result["status"], "processing")
        self.assertEqual(submit.call_args.args[0], session.pk)

    def test_manual_route_example_uses_the_shared_screen_executor(self):
        from .models import ProductionType, ProductionTrainingExample

        kind = ProductionType.objects.create(code="assistant-example", name="Тестовый тип")
        result = execute_tool("production.training_example_save", self.admin, {
            "production_type": kind.code,
            "line": {"name": "Тестовая папка", "requirements": {}},
            "routes": [{"name": "Печать", "processes": [{"name": "Печать", "role": "production"}]}],
        }, confirmed=True)

        self.assertEqual(ProductionTrainingExample.objects.get(pk=result["example_id"]).position_name, "Тестовая папка")

    def test_start_evaluation_is_a_fixed_high_level_tender_action(self):
        from tender_selection.models import Tender

        tender = Tender.objects.create(purchase_number="assistant-review")
        from unittest.mock import patch
        with patch("tender_selection.services.start_risk_assessment_in_background"):
            result = execute_tool("tender.start_evaluation", self.admin, {
                "tender_id": tender.pk,
            }, confirmed=True)

        tender.refresh_from_db()
        self.assertEqual(result["review"], Tender.INTERESTING)
        self.assertEqual(tender.review, Tender.INTERESTING)

    def test_document_preview_is_available_through_the_shared_executor(self):
        from unittest.mock import patch
        from tender_selection.models import Tender

        tender = Tender.objects.create(
            purchase_number="assistant-document",
            notification_raw={"source": {"attachmentsInfo": {"attachmentInfo": [{
                "url": "https://zakupki.gov.ru/contract", "fileName": "Контракт.docx",
                "docKindInfo": {"name": "Проект контракта"},
            }]}}},
        )
        with patch("tender_selection.services._fetch_doc_bytes", return_value=b"raw"), \
                patch("tender_selection.services.extract_preview", return_value={"kind": "docx", "html": "<p>контракт</p>"}):
            result = execute_tool("tender.document_preview", self.admin, {
                "tender_id": tender.pk, "document_index": 0,
            })

        self.assertEqual(result["name"], "Контракт.docx")
        self.assertEqual(result["html"], "<p>контракт</p>")

    def test_tender_document_list_precedes_the_existing_preview_executor(self):
        from tender_selection.models import Tender

        tender = Tender.objects.create(
            purchase_number="assistant-document-list",
            notification_raw={"source": {"attachmentsInfo": {"attachmentInfo": [{
                "url": "https://zakupki.gov.ru/contract", "fileName": "Контракт.docx",
                "docKindInfo": {"name": "Проект контракта"}, "fileSize": 2048,
            }]}}},
        )
        result = execute_tool("tender.documents_list", self.admin, {"tender_id": tender.pk})

        self.assertEqual(result["tender_id"], tender.pk)
        self.assertEqual(result["documents"], [{
            "index": 0, "name": "Контракт.docx", "kind": "Проект контракта",
            "size_kb": 2, "url": "https://zakupki.gov.ru/contract",
        }])

    def test_stage_proposal_confirmation_uses_the_shared_route_executor(self):
        from .models import ProductionTrainingSession

        session = ProductionTrainingSession.objects.create(
            created_by=self.admin, position_name="Папка", requirements={},
            current_hypothesis={"stage_proposals": [{
                "id": "assembly", "name": "Сборка", "description": "Собрать изделие", "role": "production",
            }]},
        )
        result = execute_tool("production.stage_proposal_confirm", self.admin, {
            "session_id": session.pk, "proposal_id": "assembly",
        }, confirmed=True)

        self.assertTrue(result["route_refresh_required"])
        self.assertTrue(ProcessDefinition.objects.filter(name="Сборка").exists())

    def test_reopening_confirmed_route_uses_the_shared_executor(self):
        from .models import ProductionTrainingSession

        accepted = ProductionTrainingSession.objects.create(
            created_by=self.admin, position_name="Папка", requirements={"format": "A4"},
            current_hypothesis={"route": {"processes": []}}, is_confirmed=True,
        )
        result = execute_tool("production.route_reopen", self.admin, {
            "session_id": accepted.pk,
        }, confirmed=True)

        self.assertNotEqual(result["session_id"], accepted.pk)
        self.assertFalse(ProductionTrainingSession.objects.get(pk=result["session_id"]).is_confirmed)

    def test_catalog_selection_uses_the_shared_route_executor(self):
        from unittest.mock import patch
        from .models import ProductionTrainingSession

        session = ProductionTrainingSession.objects.create(
            created_by=self.admin, position_name="Папка", requirements={}, current_hypothesis={},
        )
        selected = {"catalog_selection": {"id": "sku-1", "name": "Папка", "supplier_code": "test"}}
        with patch("tenders.services.apply_catalog_candidate", return_value=selected):
            result = execute_tool("production.catalog_product_select", self.admin, {
                "session_id": session.pk, "line": {"name": "Папка", "quantity": 100}, "product_id": "sku-1",
            }, confirmed=True)

        self.assertEqual(result["session_id"], session.pk)
        self.assertEqual(CatalogMatchDecision.objects.filter(session=session, decision="selected").count(), 1)

    def test_production_counterparty_draft_uses_the_shared_executor(self):
        from unittest.mock import patch

        with patch("tenders.services.extract_calculation_source", return_value={
            "source_type": "text", "url": "", "content": "Печать", "structured_data": {},
        }), patch("tenders.services.parse_counterparty_draft", return_value={
            "name": "Тестовый поставщик", "notes": "", "suggested_stage_names": [],
        }):
            result = execute_tool("provider.production_draft", self.admin, {"text": "Печать"}, confirmed=True)

        self.assertEqual(result["name"], "Тестовый поставщик")
        self.assertTrue(result["source_id"])

    def test_calculation_source_action_uses_the_shared_executor(self):
        from unittest.mock import patch
        from .models import ProductionTrainingSession

        session = ProductionTrainingSession.objects.create(
            created_by=self.admin, position_name="Папка", requirements={}, current_hypothesis={"costs": []},
        )
        with patch("tenders.services.extract_calculation_source", return_value={
            "source_type": "text", "url": "", "content": "Цена", "structured_data": {},
        }), patch("tenders.services.build_training_hypothesis", return_value={"costs": []}):
            result = execute_tool("production.calculation_sources_add", self.admin, {
                "session_id": session.pk, "line": {"name": "Папка", "quantity": 10}, "source_text": "Цена",
            }, confirmed=True)

        self.assertEqual(result["session_id"], session.pk)
        self.assertEqual(TenderKnowledgeSource.objects.count(), 1)

    def test_financial_period_lock_is_shared_and_requires_confirmation(self):
        from finance.models import FinancialPeriod
        args = {"period": "2026-10", "closed": True}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("finance.period_lock", self.admin, args)
        self.assertFalse(FinancialPeriod.objects.filter(code="2026-10", is_closed=True).exists())
        self.assertTrue(execute_tool("finance.period_lock", self.admin, args, confirmed=True)["is_closed"])
        self.assertTrue(FinancialPeriod.objects.get(code="2026-10").is_closed)
        with self.assertRaisesRegex(Exception, "true или false"):
            execute_tool("finance.period_lock", self.admin, {
                "period": "2026-10", "closed": "false"
            }, confirmed=True)
        self.assertFalse(execute_tool("finance.period_lock", self.admin, {
            "period": "2026-10", "closed": False
        }, confirmed=True)["is_closed"])

    def test_saved_order_and_tender_estimates_copy_and_delete_using_shared_service(self):
        from .models import OrderEstimate, OrderLine, TenderEstimate, TenderLine

        order = OrderEstimate.objects.create(owner=self.admin, name="Заказ", order_number="O-1")
        OrderLine.objects.create(estimate=order, name="Тираж", quantity=10, nmck_unit=20)
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("estimate.duplicate", self.admin, {"estimate_id": order.pk, "pipeline": False})
        copy_id = execute_tool("estimate.duplicate", self.admin, {"estimate_id": order.pk, "pipeline": False}, confirmed=True)["estimate_id"]
        copied = OrderEstimate.objects.get(pk=copy_id)
        self.assertEqual(copied.order_number, "O-1")
        self.assertEqual(copied.lines.count(), 1)
        tender = TenderEstimate.objects.create(owner=self.admin, name="Тендер", tender_number="T-1")
        TenderLine.objects.create(estimate=tender, name="Папки", quantity=5, nmck_unit=100)
        tender_copy = execute_tool("estimate.duplicate", self.admin, {"estimate_id": tender.pk, "pipeline": True}, confirmed=True)
        self.assertEqual(TenderEstimate.objects.get(pk=tender_copy["estimate_id"]).lines.count(), 1)
        self.assertTrue(execute_tool("estimate.delete", self.admin, {"estimate_id": copy_id, "pipeline": False}, confirmed=True)["deleted"])
        self.assertFalse(OrderEstimate.objects.filter(pk=copy_id).exists())
        self.assertTrue(OrderEstimate.objects.filter(pk=order.pk).exists())

    def test_shared_order_estimate_status_checks_owner_and_writes_status(self):
        from .models import Order, OrderEstimate
        estimate = OrderEstimate.objects.create(owner=self.admin, name="Заказ со статусом", order_number="O-ST")
        params = {"estimate_id": estimate.pk, "status": Order.PENDING}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("estimate.order_status", self.admin, params)
        with self.assertRaisesRegex(Exception, "Недостаточно прав"):
            execute_tool("estimate.order_status", self.user, params, confirmed=True)
        result = execute_tool("estimate.order_status", self.admin, params, confirmed=True)
        self.assertEqual(result["status"], Order.PENDING)
        estimate.refresh_from_db()
        self.assertEqual(estimate.order.status, Order.PENDING)

    def test_complete_tender_filters_macro_updates_only_specified_fields(self):
        from tender_selection.models import FilterSettings

        before = FilterSettings.load()
        old_window = before.window_days
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("tender.discovery_settings", self.admin, {"min_price": "500000"})
        self.assertTrue(execute_tool("tender.discovery_settings", self.admin, {
            "min_price": "500000", "laws": ["fz44", "fz223"],
        }, confirmed=True)["updated"])
        updated = FilterSettings.load()
        updated.refresh_from_db()
        self.assertEqual(str(updated.min_price), "500000.00")
        self.assertEqual(updated.window_days, old_window)
        self.assertEqual(updated.laws, ["fz44", "fz223"])
        with self.assertRaisesRegex(Exception, "неотрицательным"):
            execute_tool("tender.discovery_settings", self.admin, {"min_price": -1}, confirmed=True)

    def test_route_knowledge_macro_needs_confirmation_and_valid_target(self):
        from .models import Lesson
        lesson = Lesson.objects.create(summary="Проверенный маршрут", scope="route")
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("production.route_knowledge_deactivate", self.admin, {"lesson_id": lesson.pk})
        with self.assertRaisesRegex(Exception, "не найден"):
            execute_tool("production.route_knowledge_deactivate", self.admin, {
                "lesson_id": 777
            }, confirmed=True)
        lesson.refresh_from_db()
        self.assertTrue(lesson.is_active)

    def test_production_base_counterparty_macro_reuses_proposal_service(self):
        from .models import Counterparty, Proposal

        payload = {"name": "Мастерская Север", "stage_ids": [self.stage.pk], "notes": "Тест"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.production_create", self.admin, payload)
        result = execute_tool("provider.production_create", self.admin, payload, confirmed=True)
        self.assertTrue(Counterparty.objects.filter(pk=result["id"]).exists())
        self.assertIn(self.stage.name, result["stage_names"])
        self.assertTrue(Proposal.objects.filter(type=Proposal.TYPE_CREATE_COUNTERPARTY).exists())

    def test_feedback_draft_is_reviewable_before_global_mutation(self):
        from unittest.mock import patch
        from .models import Proposal
        args = {"text": "Обнови сведения о подрядчике"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("production.feedback_draft", self.admin, args)
        with patch("tenders.services.parse_production_feedback", return_value=[]):
            result = execute_tool("production.feedback_draft", self.admin, args, confirmed=True)
        self.assertEqual(result["items"], [])
        self.assertFalse(Proposal.objects.exists())

    def test_tender_question_macro_records_answer_once(self):
        from tender_selection.models import Tender
        from .models import OwnerInteraction, OwnerFeedbackEvent

        tender = Tender.objects.create(
            purchase_number="ASK-001", collecting_finished_at="2026-10-31T10:00:00Z"
        )
        question = OwnerInteraction.objects.create(
            tender=tender, question="Уточните срок поставки", status="open",
        )
        args = {"tender_id": tender.pk, "interaction_id": question.pk, "answer": "До конца месяца"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("tender.owner_question_answer", self.admin, args)
        self.assertEqual(execute_tool("tender.owner_question_answer", self.admin, args, confirmed=True)["status"], "resolved")
        self.assertEqual(OwnerFeedbackEvent.objects.filter(interaction=question).count(), 1)
        with self.assertRaisesRegex(Exception, "уже закрыт"):
            execute_tool("tender.owner_question_answer", self.admin, args, confirmed=True)

    def test_estimate_save_recomputes_and_updates_same_record(self):
        from .models import TenderEstimate
        args = {
            "pipeline": True, "tender_number": "CALC-007",
            "name": "Папки", "lines": [{
                "name": "Папка", "quantity": "10", "nmck_unit": "300",
                "material_unit": "100", "application_unit": "20",
                "logistics_unit": "10",
            }],
        }
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("estimate.save", self.admin, args)
        created = execute_tool("estimate.save", self.admin, args, confirmed=True)
        self.assertFalse(created["incomplete"])
        saved = TenderEstimate.objects.get(pk=created["estimate_id"])
        self.assertEqual(saved.lines.count(), 1)
        args.update(estimate_id=saved.pk, name="Папки изменены")
        updated = execute_tool("estimate.save", self.admin, args, confirmed=True)
        self.assertEqual(updated["estimate_id"], saved.pk)
        self.assertEqual(TenderEstimate.objects.get(pk=saved.pk).name, "Папки изменены")
        self.assertEqual(TenderEstimate.objects.count(), 1)

    def test_feedback_decision_rejects_unknown_batch(self):
        with self.assertRaisesRegex(Exception, "Неверный идентификатор"):
            execute_tool("production.feedback_decide", self.admin, {
                "batch_id": "no-such-batch", "accepted_ids": [],
            }, confirmed=True)

    def test_read_uses_real_cash_records_and_aggregates_before_limit(self):
        from cash.models import CashTransaction

        for amount in ("10.00", "20.00", "30.00"):
            CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount=amount, reason="Бумага", created_by=self.admin)
        result = execute_tool("read", self.admin, {"plan": {"entity": "cash.CashTransaction", "fields": ["id", "amount"], "limit": 1, "aggregates": [{"op": "sum", "field": "amount"}]}})
        self.assertEqual(result["total_count"], 3)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(str(result["aggregates"]["sum_amount"]), "60")

    def test_read_uses_multiple_order_fields_and_a_primary_key_tiebreaker(self):
        from cash.models import CashTransaction
        from django.utils import timezone

        first = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="income", amount="10", reason="Первая", created_by=self.admin)
        second = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="income", amount="20", reason="Вторая", created_by=self.admin)
        same_time = timezone.make_aware(datetime(2026, 9, 2, 10, 0))
        CashTransaction.objects.filter(pk__in=[first.pk, second.pk]).update(created_at=same_time)

        result = execute_tool("read", self.admin, {"plan": {
            "entity": "cash.CashTransaction", "fields": ["reason", "operation_date", "created_at"],
            "order_by": ["-operation_date", "-created_at"], "limit": 2,
        }})

        self.assertEqual([row["id"] for row in result["records"]], [second.pk, first.pk])
        self.assertEqual(result["query"]["order_by"], ["-operation_date", "-created_at", "-id"])

    def test_read_keeps_string_order_by_compatible_and_reports_tiebreaker(self):
        from cash.models import CashTransaction

        first = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="income", amount="10", reason="Первая", created_by=self.admin)
        second = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="income", amount="20", reason="Вторая", created_by=self.admin)

        result = execute_tool("read", self.admin, {"plan": {
            "entity": "cash.CashTransaction", "fields": ["reason"], "order_by": "-operation_date", "limit": 1,
        }})

        self.assertEqual(result["records"][0]["id"], second.pk)
        self.assertEqual(result["query"]["order_by"], ["-operation_date", "-id"])

    def test_read_aggregate_does_not_depend_on_row_sorting(self):
        from cash.models import CashTransaction

        for amount in ("10", "20", "30"):
            CashTransaction.objects.create(
                operation_date=date(2026, 9, 1), account="cash", direction="income",
                amount=amount, reason="Агрегация", created_by=self.admin,
            )
        base = {
            "entity": "cash.CashTransaction", "fields": ["amount"],
            "aggregates": [{"op": "sum", "field": "amount"}],
        }
        ascending = execute_tool("read", self.admin, {"plan": {**base, "order_by": "amount"}})
        descending = execute_tool("read", self.admin, {"plan": {**base, "order_by": "-id"}})

        self.assertEqual(ascending["aggregates"], descending["aggregates"])
        self.assertEqual(str(ascending["aggregates"]["sum_amount"]), "60")

    def test_read_groups_dates_by_month_and_reports_empty_periods_and_comparison(self):
        from cash.models import CashTransaction

        for operation_date, amount in ((date(2026, 8, 5), "100"), (date(2026, 9, 10), "350")):
            CashTransaction.objects.create(
                operation_date=operation_date, account="cash", direction="expense", amount=amount,
                reason="Группировка", created_by=self.admin,
            )

        result = execute_tool("read", self.admin, {"plan": {
            "entity": "cash.CashTransaction", "fields": ["amount"],
            "filters": [
                {"field": "operation_date", "op": "gte", "value": "2026-07-01"},
                {"field": "operation_date", "op": "lt", "value": "2026-10-01"},
            ],
            "group_by": [{"field": "operation_date", "period": "month"}],
            "aggregates": [{"op": "sum", "field": "amount"}],
        }})

        self.assertEqual(
            [(row["operation_date:month"], str(row["sum_amount"])) for row in result["records"]],
            [("2026-07-01", "0"), ("2026-08-01", "100"), ("2026-09-01", "350")],
        )
        self.assertTrue(result["records"][0]["empty_period"])
        self.assertEqual(result["comparisons"]["sum_amount"]["maximum"]["value"], "350")
        self.assertEqual(result["comparisons"]["sum_amount"]["maximum"]["difference_to_next"], "250")

    def test_read_rejects_period_grouping_for_non_date_fields(self):
        from .assistant_read import ReadPlanError, validate_plan

        with self.assertRaises(ReadPlanError):
            validate_plan(self.admin, {"entity": "cash.CashTransaction", "group_by": [
                {"field": "amount", "period": "month"},
            ]})

    def test_read_groups_datetime_fields_in_the_configured_timezone(self):
        from cash.models import CashTransaction
        from django.utils import timezone

        transaction = CashTransaction.objects.create(
            operation_date=date(2026, 9, 1), account="cash", direction="expense", amount="10",
            reason="Дата", created_by=self.admin,
        )
        CashTransaction.objects.filter(pk=transaction.pk).update(
            created_at=timezone.make_aware(datetime(2026, 9, 30, 22, 30)),
        )

        result = execute_tool("read", self.admin, {"plan": {
            "entity": "cash.CashTransaction", "fields": ["amount"],
            "group_by": [{"field": "created_at", "period": "month"}],
            "aggregates": [{"op": "sum", "field": "amount"}],
        }})

        self.assertTrue(result["records"][0]["created_at:month"].startswith("2026-09-01"))
        self.assertEqual(str(result["records"][0]["sum_amount"]), "10")

    def test_complete_read_result_can_be_reused_for_a_narrower_aggregate(self):
        from cash.models import CashTransaction
        from .assistant_read import execute, reuse_result

        for direction, amount in (("income", "10"), ("expense", "20"), ("expense", "30")):
            CashTransaction.objects.create(
                operation_date=date(2026, 9, 1), account="cash", direction=direction,
                amount=amount, reason="Кэш", created_by=self.admin,
            )
        cached = execute(self.admin, {"entity": "cash.CashTransaction", "fields": ["direction", "amount"], "limit": 3})
        reused = reuse_result(self.admin, {
            "entity": "cash.CashTransaction", "fields": ["direction", "amount"], "limit": 3,
            "filters": [{"field": "direction", "op": "eq", "value": "expense"}],
            "aggregates": [{"op": "sum", "field": "amount"}],
        }, cached)

        self.assertIsNotNone(reused)
        self.assertEqual(reused["source_scope"], "cached complete result")
        self.assertEqual(str(reused["aggregates"]["sum_amount"]), "50.00")

    def test_limited_read_result_is_not_reused_as_a_complete_aggregate(self):
        from cash.models import CashTransaction
        from .assistant_read import execute, reuse_result

        for amount in ("10", "20", "30"):
            CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount=amount, reason="Лимит", created_by=self.admin)
        cached = execute(self.admin, {"entity": "cash.CashTransaction", "fields": ["amount"], "limit": 2})

        self.assertIsNone(reuse_result(self.admin, {
            "entity": "cash.CashTransaction", "fields": ["amount"], "limit": 2,
            "aggregates": [{"op": "sum", "field": "amount"}],
        }, cached))

    def test_read_rejects_hidden_order_field(self):
        from .assistant_tools import AssistantToolError

        with self.assertRaisesRegex(AssistantToolError, "Недоступное поле raw"):
            execute_tool("read", self.admin, {"plan": {
                "entity": "cash.BankPayment", "order_by": ["-raw", "-id"],
            }})

    def test_read_business_view_reuses_the_existing_incoming_tender_rules(self):
        from tender_selection.models import FilterSettings, Tender

        settings = FilterSettings.load()
        settings.min_price = 100
        settings.include_words = "подход"
        settings.save(update_fields=["min_price", "include_words"])
        visible = Tender.objects.create(purchase_number="read-visible", title="Подходящий", max_price="300000")
        Tender.objects.create(purchase_number="read-hidden", title="Скрытый", max_price="1")

        result = execute_tool("read", self.admin, {"plan": {
            "entity": "tender_selection.Tender", "view": "incoming",
            "fields": ["purchase_number"], "order_by": "-id", "limit": 10,
        }})

        self.assertEqual([record["purchase_number"] for record in result["records"]], [visible.purchase_number])
        self.assertEqual(result["query"]["view"], "incoming")

    def test_read_rejects_sensitive_bank_account_and_non_owner(self):
        from cash.models import BankPayment

        BankPayment.objects.create(external_id="test-payment", amount="10", operation_date=date(2026, 9, 1), account_number="secret")
        with self.assertRaisesRegex(Exception, "Недоступное поле account_number"):
            execute_tool("read", self.admin, {"plan": {"entity": "cash.BankPayment", "fields": ["account_number"]}})
        with self.assertRaisesRegex(Exception, "Недостаточно прав"):
            execute_tool("read", self.user, {"plan": {"entity": "cash.BankPayment", "fields": ["id"]}})

    def test_read_uses_metadata_for_tenders_cash_banks_json_and_relations(self):
        """All business examples use the same generic read plan, not app tools."""
        from cash.models import BankPayment, CashTransaction
        from tender_selection.models import Tender
        from .models import TenderEstimate

        first = Tender.objects.create(
            purchase_number="READ-1", collecting_finished_at="2026-09-30T10:00:00Z",
            outcome_status=Tender.OUTCOME_LOST, contract_reduction_percent="10.00",
            risk_assessment={"risk_level": "high", "secret_token": "must not leak"},
        )
        second = Tender.objects.create(
            purchase_number="READ-2", collecting_finished_at="2026-09-29T10:00:00Z",
            outcome_status=Tender.OUTCOME_LOST, contract_reduction_percent="30.00",
        )
        TenderEstimate.objects.create(owner=self.admin, tender=first, tender_number="READ-1", name="Расчёт")
        CashTransaction.objects.create(operation_date=date(2026, 9, 2), account="cash", direction="income", amount="100", reason="Оплата", created_by=self.admin)
        CashTransaction.objects.create(operation_date=date(2026, 9, 3), account="cash", direction="expense", amount="40", reason="Бумага", created_by=self.admin)
        BankPayment.objects.create(external_id="read-bank", amount="75", operation_date=date(2026, 9, 4), counterparty_name="Заказчик")

        tenders = execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number", "collecting_finished_at"], "order_by": "-collecting_finished_at", "limit": 10}})
        self.assertEqual(tenders["records"][0]["id"], first.pk)
        cash = execute_tool("read", self.admin, {"plan": {"entity": "cash.CashTransaction", "filters": [{"field": "operation_date", "op": "gte", "value": "2026-09-01"}, {"field": "operation_date", "op": "lt", "value": "2026-10-01"}], "group_by": ["direction"], "aggregates": [{"op": "sum", "field": "amount"}]}})
        self.assertEqual({row["direction"]: str(row["sum_amount"]) for row in cash["records"]}, {"income": "100", "expense": "40"})
        bank = execute_tool("read", self.admin, {"plan": {"entity": "cash.BankPayment", "fields": ["operation_date", "amount", "counterparty_name"], "filters": [{"field": "operation_date", "op": "gte", "value": "2026-09-01"}, {"field": "operation_date", "op": "lt", "value": "2026-10-01"}]}})
        self.assertEqual(bank["records"][0]["id"], BankPayment.objects.get(external_id="read-bank").pk)
        lost = execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "fields": ["purchase_number"], "filters": [{"field": "outcome_status", "op": "eq", "value": "lost"}], "aggregates": [{"op": "avg", "field": "contract_reduction_percent"}]}})
        self.assertEqual(str(lost["aggregates"]["avg_contract_reduction_percent"]), "20")
        risk = execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "fields": ["risk_assessment"], "filters": [{"field": "id", "op": "eq", "value": first.pk}]}})
        self.assertEqual(risk["records"][0]["risk_assessment"]["risk_level"], "high")
        self.assertNotIn("secret_token", risk["records"][0]["risk_assessment"])
        estimates = execute_tool("read", self.admin, {"plan": {"entity": "tenders.TenderEstimate", "fields": ["name", "tender.purchase_number", "tender.outcome_status"], "filters": [{"field": "tender.id", "op": "eq", "value": first.pk}]}})
        self.assertEqual(estimates["records"][0]["tender.purchase_number"], "READ-1")
        self.assertEqual(second.outcome_status, Tender.OUTCOME_LOST)

    def test_read_result_is_json_serializable_and_redacts_credential_shaped_json_values(self):
        from tender_selection.models import Tender

        tender = Tender.objects.create(
            purchase_number="SAFE-JSON", collecting_finished_at="2026-09-30T10:00:00Z",
            risk_assessment={"risk_level": "low", "evidence": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturevalue"},
        )
        result = execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "fields": ["collecting_finished_at", "risk_assessment"], "filters": [{"field": "id", "op": "eq", "value": tender.pk}]}})
        self.assertEqual(result["records"][0]["collecting_finished_at"], "2026-09-30T10:00:00+00:00")
        self.assertEqual(result["records"][0]["risk_assessment"]["evidence"], "[скрыто: возможные учётные данные]")
        json.dumps(result, ensure_ascii=False)

    def test_read_catalog_and_validation_share_secret_and_relation_policy(self):
        catalog = execute_tool("assistant.catalog", self.admin, {})["models"]
        bank = next(item for item in catalog if item["id"] == "cash.BankPayment")
        self.assertNotIn("account_number", {field["name"] for field in bank["fields"]})
        estimate = next(item for item in catalog if item["id"] == "tenders.TenderEstimate")
        self.assertEqual(next(field for field in estimate["fields"] if field["name"] == "tender")["traversal"], "forward_to_one")
        from .assistant_read import validate_plan
        validated = validate_plan(self.admin, {"entity": "tenders.TenderEstimate", "fields": ["owner.username"]})
        self.assertIn("owner.username", validated["selected_names"])
        with self.assertRaisesRegex(Exception, "Недоступное поле password"):
            execute_tool("read", self.admin, {"plan": {"entity": "tenders.TenderEstimate", "fields": ["owner.password"]}})
        with self.assertRaisesRegex(Exception, "JSON-поля"):
            execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "filters": [{"field": "risk_assessment", "op": "eq", "value": {}}]}})

    def test_read_contract_exposes_choices_and_normalizes_orm_values(self):
        from .assistant_read import catalogue, validate_plan

        models = {item["id"]: item for item in catalogue(compact=True)["entities"]}
        contract = catalogue(compact=True)["filter_contract"]
        self.assertEqual(contract["value_formats"]["DateField"], "YYYY-MM-DD")
        self.assertEqual(contract["value_formats"]["DecimalField"], "JSON number or decimal string with .")
        self.assertTrue(models)
        self.assertTrue(all(field.get("label") for model in models.values() for field in model["fields"]))
        tender = models["tender_selection.Tender"]
        outcome = next(field for field in tender["fields"] if field["name"] == "outcome_status")
        published = next(field for field in tender["fields"] if field["name"] == "published_at")
        self.assertEqual(outcome["label"], "Стадия сделки")
        self.assertEqual(published["label"], "Опубликовано")
        self.assertIn({"value": "lost", "label": "Проигран"}, outcome["choices"])

        validated = validate_plan(self.admin, {"entity": "cash.CashTransaction", "filters": [
            {"field": "operation_date", "op": "gte", "value": "2026-09-01"},
            {"field": "amount", "op": "gt", "value": "12.50"},
        ]})
        self.assertEqual(validated["filters"][0]["value"], date(2026, 9, 1))
        self.assertEqual(str(validated["filters"][1]["value"]), "12.50")

    def test_read_contract_rejects_values_not_valid_for_the_discovered_field(self):
        with self.assertRaisesRegex(Exception, "outcome_status"):
            execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "filters": [
                {"field": "outcome_status", "op": "eq", "value": "Проигран"},
            ]}})
        with self.assertRaisesRegex(Exception, "operation_date"):
            execute_tool("read", self.admin, {"plan": {"entity": "cash.CashTransaction", "filters": [
                {"field": "operation_date", "op": "gte", "value": "сентябрь"},
            ]}})

    def test_cash_action_uses_existing_form_and_audit(self):
        from cash.models import CashAuditLog, CashTransaction

        payload = {"operation_date": "2026-10-12", "account": "cash", "direction": "expense", "amount": "15000", "reason": "Доставка"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("cash.transaction_create", self.admin, payload)
        result = execute_tool("cash.transaction_create", self.admin, payload, confirmed=True)
        self.assertEqual(CashTransaction.objects.get(pk=result["transaction_id"]).reason, "Доставка")
        self.assertEqual(CashAuditLog.objects.filter(transaction_id=result["transaction_id"], action=CashAuditLog.ACTION_CREATED).count(), 1)

    def test_finance_action_uses_existing_order_form(self):
        from payroll.models import OrderRecord

        manager = get_user_model().objects.create_user("order-manager")
        payload = {"record_type": "order", "order_number": "123", "gross_profit": "85000", "accounting_period": "2026-10", "manager": str(manager.pk)}
        result = execute_tool("finance.order_record_create", self.admin, payload, confirmed=True)
        self.assertEqual(OrderRecord.objects.get(pk=result["order_record_id"]).manager, manager)

    def test_catalog_is_generated_from_django_models_and_real_actions(self):
        result = execute_tool("assistant.catalog", self.admin, {})
        self.assertTrue(any(item["model"] == "cash.CashTransaction" for item in result["models"]))
        actions = {item["id"]: item for item in result["actions"]}
        self.assertIn("cash.transaction_update", actions)
        self.assertIn("tender.discovery_settings", actions)
        self.assertTrue(actions["cash.transaction_update"]["source"])

    def test_mass_and_background_operations_are_not_published_to_the_assistant(self):
        actions = {item["id"] for item in available_tools(self.admin)}
        self.assertTrue({
            "cash.bank_sync", "tender.word_audit_run", "tender.word_audit_apply",
            "tender.profile_triage_run", "tender.pull",
        }.isdisjoint(actions))

    def test_published_action_sources_resolve_to_real_callables(self):
        for tool in available_tools(self.admin):
            if tool["id"].startswith("assistant."):
                continue
            module_name, attribute = tool["source"].rsplit(".", 1)
            self.assertTrue(callable(getattr(import_module(module_name), attribute)), tool["id"])

    def test_cash_update_delete_and_bank_visibility_reuse_real_actions(self):
        from cash.models import BankPayment, CashTransaction

        transaction = CashTransaction.objects.create(operation_date=date(2026, 9, 1), account="cash", direction="expense", amount="10", reason="Старое", created_by=self.admin)
        updated = execute_tool("cash.transaction_update", self.admin, {"transaction_id": transaction.pk, "operation_date": "2026-09-02", "account": "card", "direction": "income", "amount": "11", "reason": "Новое"}, confirmed=True)
        self.assertEqual(updated["transaction"]["reason"], "Новое")
        self.assertTrue(execute_tool("cash.transaction_delete", self.admin, {"transaction_id": transaction.pk}, confirmed=True)["deleted"])
        payment = BankPayment.objects.create(external_id="visibility", amount="10", operation_date=date(2026, 9, 1))
        self.assertTrue(execute_tool("cash.bank_payment_visibility_toggle", self.admin, {"bank_payment_id": payment.pk}, confirmed=True)["hidden_from_managers"])

    def test_general_calculator_uses_existing_calculation_service(self):
        from calculator.models import PriceItem

        item = PriceItem.objects.create(category="paper", name="Бумага", unit_name="лист", unit_price="2")
        result = execute_tool("calculator.sheet_calculate", self.admin, {"product_quantity": 100, "work_hours": "1", "lines": [{"item_id": item.pk, "category": "paper", "quantity": "100"}]})
        self.assertIn("standard", result["summary"])

    def test_saved_calculator_uses_shared_executor_and_requires_a_name(self):
        from calculator.models import Estimate, PriceItem

        item = PriceItem.objects.create(category="paper", name="Сохранённая бумага", unit_name="лист", unit_price="2")
        payload = {"name": "Тираж для клиента", "product_quantity": 100, "work_hours": "1", "lines": [{"item_id": item.pk, "category": "paper", "quantity": "100", "custom": False}]}
        self.assertIn("name", required_input_names("calculator.estimate_save"))
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("calculator.estimate_save", self.admin, payload)
        result = execute_tool("calculator.estimate_save", self.admin, payload, confirmed=True)
        self.assertEqual(Estimate.objects.get(pk=result["estimate_id"]).name, "Тираж для клиента")

    def test_saved_calculator_actions_use_shared_service(self):
        from calculator.models import Estimate

        estimate = Estimate.objects.create(owner=self.admin, name="Исходный")
        copied = execute_tool("calculator.estimate_duplicate", self.admin, {"estimate_id": estimate.pk}, confirmed=True)
        self.assertEqual(Estimate.objects.get(pk=copied["estimate_id"]).name, "Исходный (копия)")
        self.assertTrue(execute_tool("calculator.estimate_delete", self.admin, {"estimate_id": copied["estimate_id"]}, confirmed=True)["deleted"])
