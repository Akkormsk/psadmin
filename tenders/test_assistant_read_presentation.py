from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase, override_settings

from cash.models import CashTransaction
from tender_selection.models import Tender
from .assistant_read import ReadPlanError, _display_value, describe_entity, plan_contract, reuse_result, validate_plan
from .assistant_tools import AssistantToolError, execute_tool
from .models import TenderEstimate


@override_settings(TIME_ZONE="Europe/Moscow")
class AssistantReadPresentationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("read-presentation", "read@example.test", "password")

    def read(self, fields, **kwargs):
        return execute_tool("read", self.user, {"plan": {"entity": "tenders.TenderEstimate", "fields": fields, "order_by": ["id"], **kwargs}})

    def estimate(self, incomplete=True, outcome=Tender.OUTCOME_DRAFT):
        tender = Tender.objects.create(purchase_number=f"DISPLAY-{Tender.objects.count()}", title="Тест", outcome_status=outcome)
        return TenderEstimate.objects.create(owner=self.user, tender=tender, tender_number="DISPLAY", name="Тест", summary_snapshot={"is_incomplete": incomplete})

    def test_datetime_display_converts_offset_and_preserves_precision_and_date(self):
        transaction = CashTransaction.objects.create(operation_date=date(2026, 10, 24), account="cash", direction="expense", amount="1.25", reason="Точная дата", created_by=self.user)
        timestamp = datetime(2026, 10, 24, 22, 10, 28, 688137, tzinfo=dt_timezone.utc)
        CashTransaction.objects.filter(pk=transaction.pk).update(created_at=timestamp)
        result = execute_tool("read", self.user, {"plan": {"entity": "cash.CashTransaction", "fields": ["created_at", "operation_date", "amount"], "filters": [{"field": "id", "op": "eq", "value": transaction.pk}]}})
        self.assertEqual(result["records"][0]["created_at"], timestamp.isoformat())
        self.assertEqual(result["display"]["records"][0]["created_at"], "2026-10-25 01:10:28.688137+03:00")
        self.assertEqual(result["display"]["records"][0]["operation_date"], "24.10.2026")
        self.assertEqual(result["records"][0]["amount"], "1.25")

    def test_datetime_already_in_moscow_is_not_shifted_twice(self):
        field = CashTransaction._meta.get_field("created_at")
        self.assertEqual(_display_value("2026-10-25T12:10:28+03:00", field), "2026-10-25 12:10:28+03:00")
        self.assertEqual(_display_value("not-a-date", field), "not-a-date")

    def test_read_status_matches_site_method_for_draft_ready_and_later_stage(self):
        estimates = [self.estimate(), self.estimate(False), self.estimate(outcome=Tender.OUTCOME_PENDING)]
        result = self.read(["name", "display_status", "tender.status"])
        values = [r["display_status"] for r in result["records"]]
        self.assertEqual(values, [item.display_status() for item in estimates])
        self.assertEqual(values, ["Черновик", "Готово", "На торгах"])
        labels = {f["name"]: f["label"] for f in result["display"]["fields"]}
        self.assertEqual(labels["display_status"], "Статус расчёта")

    def test_read_calls_existing_method_instead_of_reimplementing_status(self):
        self.estimate()
        with patch.object(TenderEstimate, "display_status", return_value="Проверенное представление") as method:
            result = self.read(["display_status"])
        method.assert_called_once()
        self.assertEqual(result["records"][0]["display_status"], "Проверенное представление")

    def test_computed_status_is_published_for_read_only_selection(self):
        field = next(f for f in describe_entity("tenders.TenderEstimate")["fields"] if f["name"] == "display_status")
        self.assertEqual(field["filter_ops"], [])
        self.assertEqual(field["aggregate_ops"], [])
        self.assertIn("display_status", plan_contract("tenders.TenderEstimate")["properties"]["fields"]["item"]["enum"])
        for options in ({"order_by": ["display_status"]}, {"filters": [{"field": "display_status", "op": "eq", "value": "Готово"}]}, {"aggregates": [{"op": "count", "field": "display_status"}]}, {"group_by": ["display_status"]}):
            with self.subTest(options=options), self.assertRaises(ReadPlanError):
                validate_plan(self.user, {"entity": "tenders.TenderEstimate", "fields": ["id"], **options})

    def test_free_text_search_does_not_try_to_filter_computed_field(self):
        self.estimate()
        self.assertEqual(self.read(["display_status"], search="Тест")["total_count"], 1)

    def test_computed_status_is_not_reused_from_cached_snapshot(self):
        estimate = self.estimate()
        plan = {"entity": "tenders.TenderEstimate", "fields": ["display_status"], "order_by": ["id"]}
        cached = execute_tool("read", self.user, {"plan": plan})
        estimate.summary_snapshot = {"is_incomplete": False}
        estimate.save(update_fields=["summary_snapshot"])
        self.assertIsNone(reuse_result(self.user, plan, cached))
        self.assertEqual(execute_tool("read", self.user, {"plan": plan})["records"][0]["display_status"], "Готово")

    def test_unpublished_methods_remain_unavailable(self):
        with self.assertRaises(AssistantToolError):
            self.read(["delete"])

    def test_computed_selection_works_through_existing_forward_relation(self):
        from .models import TenderLine
        estimate = self.estimate(False)
        line = TenderLine.objects.create(estimate=estimate, name="Позиция", quantity=1, nmck_unit=1)
        result = execute_tool("read", self.user, {"plan": {"entity": "tenders.TenderLine", "fields": ["estimate.display_status"], "filters": [{"field": "id", "op": "eq", "value": line.pk}]}})
        self.assertEqual(result["records"][0]["estimate.display_status"], estimate.display_status())

    def test_new_computed_field_works_without_model_name_conditions(self):
        from uuid import uuid4
        field = "value_" + uuid4().hex
        transaction = CashTransaction.objects.create(operation_date=date(2026, 10, 25), account="cash", direction="expense", amount=1, reason="Произвольное значение", created_by=self.user)
        with patch.object(CashTransaction, "assistant_read_computed_fields", {field: models.CharField("Представление")}, create=True), patch.object(CashTransaction, field, lambda instance: instance.reason, create=True):
            result = execute_tool("read", self.user, {"plan": {"entity": "cash.CashTransaction", "fields": [field], "filters": [{"field": "id", "op": "eq", "value": transaction.pk}]}})
        self.assertEqual(result["records"][0][field], transaction.reason)
        self.assertEqual(result["display"]["fields"][-1]["label"], "Представление")

    def test_computed_status_has_no_per_record_related_queries(self):
        for _ in range(3):
            self.estimate()
        with self.assertNumQueries(3):
            result = self.read(["display_status"])
        self.assertEqual(len(result["records"]), 3)
