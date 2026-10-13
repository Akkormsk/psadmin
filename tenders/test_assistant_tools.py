import json
from importlib import import_module
from django.contrib.auth import get_user_model
from django.test import TestCase
from datetime import date

from .assistant_tools import AssistantToolConfirmationRequired, available_tools, capability_issues, execute_tool, required_input_names
from .models import ProcessDefinition


class AssistantToolRegistryTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("assistant-admin", "assistant@example.test", "password")
        self.user = get_user_model().objects.create_user("assistant-user")
        self.stage, _ = ProcessDefinition.objects.get_or_create(name="Швейное производство под ключ", role="production")

    def test_published_capability_sources_resolve_to_shared_callables(self):
        self.assertEqual(capability_issues(self.admin), [])

    def test_workflow_encyclopedia_reuses_only_registered_capabilities(self):
        from .assistant_catalog import action_catalog, catalog, workflow_catalog

        actions = action_catalog(self.admin)
        workflows = workflow_catalog(self.admin, actions)
        published = {action["id"] for action in actions}
        grouped = [tool_id for goal in workflows for tool_id in goal["actions"]]
        self.assertEqual(set(grouped), published)
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(catalog(self.admin)["workflows"], workflows)
        self.assertTrue(next(goal for goal in workflows if goal["id"] == "cash")["requires_confirmation"])

    def test_inventory_describes_real_registered_tools_and_permissions(self):
        admin_tools = {item["id"]: item for item in available_tools(self.admin)}
        user_tools = {item["id"]: item for item in available_tools(self.user)}

        self.assertIn("provider.calculate", admin_tools)
        self.assertTrue(admin_tools["provider.create_draft"]["requires_confirmation"])
        self.assertFalse(user_tools["provider.create_draft"]["available"])

    def test_required_fields_checked_after_confirmation(self):
        from cash.models import CashTransaction
        with self.assertRaisesRegex(Exception, "обязательные поля: amount"):
            execute_tool("cash.transaction_create", self.admin, {
                "operation_date": "2026-10-12", "account": "cash",
                "direction": "expense", "reason": "Доставка",
            }, confirmed=True)
        self.assertFalse(CashTransaction.objects.exists())

    def test_mutating_provider_draft_requires_confirmation(self):
        payload = {"name": "Черновой пошив", "stage_ids": [self.stage.pk], "source_text": "Прайс получен"}

        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.create_draft", self.admin, payload)
        result = execute_tool("provider.create_draft", self.admin, payload, confirmed=True)

        self.assertEqual(result["status"], "draft")
        self.assertEqual(execute_tool("provider.find", self.admin, {"query": "Черновой"})["counterparties"][0]["name"], "Черновой пошив")

    def test_provider_knowledge_confirm_is_shared_and_requires_owner_approval(self):
        from .models import CounterpartyKnowledgeVersion
        provider = execute_tool("provider.create_draft", self.admin, {
            "name": "Проверка знаний", "stage_ids": [self.stage.pk],
            "source_text": "Тестовая инструкция", 
        }, confirmed=True)
        args = {"version_id": provider["knowledge_version_id"]}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.knowledge_confirm", self.admin, args)
        result = execute_tool("provider.knowledge_confirm", self.admin, args, confirmed=True)
        self.assertEqual(result["status"], CounterpartyKnowledgeVersion.STATUS_CONFIRMED)
        with self.assertRaisesRegex(Exception, "не найден или уже обработан"):
            execute_tool("provider.knowledge_confirm", self.admin, args, confirmed=True)

    def test_production_lesson_deactivation_uses_shared_service(self):
        from unittest.mock import patch

        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("production.lesson_deactivate", self.admin, {"lesson_id": 37})
        with patch("tenders.assistant_capabilities.deactivate_lesson", return_value=True) as service:
            result = execute_tool("production.lesson_deactivate", self.admin, {"lesson_id": 37}, confirmed=True)
        service.assert_called_once_with(37)
        self.assertTrue(result["dropped"])

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
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("production.route_knowledge_deactivate", self.admin, {"lesson_id": 777})
        with self.assertRaisesRegex(Exception, "Сохранённый опыт не найден"):
            execute_tool("production.route_knowledge_deactivate", self.admin, {
                "lesson_id": 777
            }, confirmed=True)

    def test_production_base_counterparty_macro_reuses_proposal_service(self):
        from .models import Counterparty, Proposal

        payload = {"name": "Мастерская Север", "stage_ids": [self.stage.pk], "notes": "Тест"}
        with self.assertRaises(AssistantToolConfirmationRequired):
            execute_tool("provider.production_create", self.admin, payload)
        result = execute_tool("provider.production_create", self.admin, payload, confirmed=True)
        self.assertTrue(Counterparty.objects.filter(pk=result["id"]).exists())
        self.assertIn(self.stage.name, result["stage_names"])
        self.assertTrue(Proposal.objects.filter(type=Proposal.TYPE_CREATE_COUNTERPARTY).exists())

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

    def test_read_rejects_sensitive_bank_account_and_non_owner(self):
        from cash.models import BankPayment

        BankPayment.objects.create(external_id="test-payment", amount="10", operation_date=date(2026, 9, 1), account_number="secret")
        with self.assertRaisesRegex(Exception, "недоступные поля"):
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
        with self.assertRaisesRegex(Exception, "недоступные поля"):
            execute_tool("read", self.admin, {"plan": {"entity": "tenders.TenderEstimate", "fields": ["owner.username"]}})
        with self.assertRaisesRegex(Exception, "JSON-поля"):
            execute_tool("read", self.admin, {"plan": {"entity": "tender_selection.Tender", "filters": [{"field": "risk_assessment", "op": "eq", "value": {}}]}})

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
        self.assertIn("tender.filter_words_update", actions)
        self.assertTrue(actions["cash.transaction_update"]["source"])

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
