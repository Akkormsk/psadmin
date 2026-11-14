import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from .admin import TenderHistoryAdmin
from .models import ContractStat, IncomingTrace, Tender, TenderDismissalFeedback, TenderHistory
from . import word_audit


class TenderHistoryTests(TestCase):
    def test_existing_table_and_alias_are_reused(self):
        self.assertIs(IncomingTrace, TenderHistory)
        self.assertEqual(TenderHistory._meta.db_table, "tender_selection_incomingtrace")
        self.assertNotIn("tender_selection_tenderhistory", connection.introspection.table_names())

    def test_save_update_and_bulk_writes_keep_one_summary_without_documents(self):
        tender = Tender.objects.create(purchase_number="one", title="Кружки", raw={"private_document": "must-not-copy"})
        tender.title = "Кружки с печатью"
        tender.save(update_fields=["title"])
        Tender.objects.filter(pk=tender.pk).update(max_price=Decimal("1000"))
        tender.refresh_from_db()
        history = TenderHistory.objects.get(tender=tender)
        self.assertEqual(history.title, tender.title)
        self.assertEqual(history.max_price, Decimal("1000"))
        self.assertEqual(TenderHistory.objects.count(), 1)
        self.assertNotIn("must-not-copy", json.dumps(history.legacy_data))
        self.assertFalse(any(f.name in {"raw", "notification_raw", "protocol"} for f in TenderHistory._meta.fields))
        bulk = Tender.objects.bulk_create([Tender(purchase_number="two"), Tender(purchase_number="three")])
        bulk[0].title = "Блокнот"
        Tender.objects.bulk_update(bulk, ["title"])
        self.assertEqual(TenderHistory.objects.get(tender=bulk[0]).title, "Блокнот")
        self.assertEqual(TenderHistory.objects.count(), 3)

    def test_source_is_part_of_the_unique_history_key(self):
        TenderHistory.objects.create(source=Tender.EIS, purchase_number="same")
        TenderHistory.objects.create(source=Tender.EAT, purchase_number="same")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TenderHistory.objects.create(source=Tender.EIS, purchase_number="same")

    def test_archive_and_restore_update_existing_history_without_new_filtering(self):
        tender = Tender.objects.create(purchase_number="archive")
        Tender.objects.filter(pk=tender.pk).update(status=Tender.DISMISSED, archived_at=timezone.now(), archived_from_stage="incoming")
        history = TenderHistory.objects.get(tender=tender)
        self.assertEqual(history.status, Tender.DISMISSED)
        self.assertEqual(history.archived_from_stage, "incoming")
        tender.refresh_from_db()
        tender.status = Tender.NEW
        tender.archived_at = None
        tender.archived_from_stage = ""
        tender.save(update_fields=["status", "archived_at", "archived_from_stage"])
        history.refresh_from_db()
        self.assertIsNone(history.archived_at)
        self.assertEqual(history.status, Tender.NEW)
        self.assertEqual(TenderHistory.objects.count(), 1)

    def test_card_deletion_keeps_manual_reason_forecast_and_result(self):
        tender = Tender.objects.create(purchase_number="deleted", title="Кружки", review=Tender.INTERESTING,
            market_forecast_percent=Decimal("12"), market_forecast_sample_count=5,
            market_forecast_at=timezone.now(), contract_price=Decimal("700"), contract_reduction_percent=Decimal("30"))
        TenderDismissalFeedback.objects.create(tender=tender, reason=TenderDismissalFeedback.NOT_PROFILE)
        stat = ContractStat.objects.create(purchase_number=tender.purchase_number, contract_reg_num="own",
            own_funnel=True, subject=tender.title, final_price=Decimal("700"), discount_pct=Decimal("30"))
        history_id = stat.history_id
        tender.delete()
        history = TenderHistory.objects.get(pk=history_id)
        self.assertIsNone(history.tender_id)
        self.assertEqual(history.forecast_percent, Decimal("12"))
        self.assertEqual(history.final_price, Decimal("700"))
        self.assertEqual(history.reason_code, TenderDismissalFeedback.NOT_PROFILE)
        self.assertEqual(history.decision_source, "manual")
        self.assertEqual(len(history.legacy_data["dismissal_feedback"]), 1)
        self.assertTrue(ContractStat.objects.filter(pk=stat.pk, history=history).exists())

    def test_unknown_facts_do_not_erase_known_result(self):
        tender = Tender.objects.create(purchase_number="facts")
        ContractStat.objects.create(purchase_number="facts", contract_reg_num="facts", own_funnel=True,
                                   final_price=Decimal("50"), discount_pct=Decimal("10"))
        tender.title = "Updated"
        tender.save(update_fields=["title"])
        self.assertEqual(TenderHistory.objects.get(tender=tender).final_price, Decimal("50"))

    def test_compatibility_statistics_update_and_delete_with_canonical_history(self):
        stat = ContractStat.objects.create(purchase_number="stat", contract_reg_num="stat", own_funnel=True,
                                          subject="Кружки", nmck=100, final_price=80, discount_pct=20)
        market = ContractStat.objects.create(purchase_number="market", contract_reg_num="market")
        ContractStat.objects.filter(pk=stat.pk).update(forecast_included=False)
        history = TenderHistory.objects.get(pk=stat.history_id)
        self.assertFalse(history.forecast_included)
        self.assertEqual(history.review, "")
        self.assertEqual(word_audit._corpus()["ignored"], [])
        history.delete()
        self.assertFalse(ContractStat.objects.filter(pk=stat.pk).exists())
        self.assertTrue(ContractStat.objects.filter(pk=market.pk).exists())

    def test_protocol_counts_and_confirmed_place_are_retained_without_full_protocol(self):
        tender = Tender.objects.create(purchase_number="protocol", bid_number="12", protocol={
            "applications_total": 15, "participants": [{"id": "12", "rank": 12}]})
        history = TenderHistory.objects.get(tender=tender)
        self.assertEqual(history.our_place, 12)
        self.assertEqual(history.participants_count, 15)
        self.assertEqual(history.legacy_data, {})

    def test_optional_malformed_protocol_does_not_break_card_storage(self):
        tender = Tender.objects.create(purchase_number="malformed", bid_number="12", protocol={
            "participants": ["bad row", {"id": "12", "rank": "12"}], "applications_total": "15"})
        self.assertEqual(tender.history_record.our_place, 12)
        self.assertEqual(tender.history_record.participants_count, 15)

    def test_history_does_not_double_count_working_card_for_word_audit(self):
        tender = Tender.objects.create(purchase_number="corpus", title="Кружки")
        self.assertEqual(word_audit._corpus()["incoming"], ["Кружки"])
        tender.delete()
        self.assertEqual(word_audit._corpus()["incoming"], ["Кружки"])

    def test_admin_allows_deleting_only_detached_history(self):
        from django.contrib import admin
        user = get_user_model().objects.create_superuser("history-admin", "admin@example.test", "password")
        request = RequestFactory().get("/admin/")
        request.user = user
        model_admin = TenderHistoryAdmin(TenderHistory, admin.site)
        tender = Tender.objects.create(purchase_number="protected")
        history = tender.history_record
        self.assertFalse(model_admin.has_delete_permission(request, history))
        tender.delete()
        history.refresh_from_db()
        self.assertTrue(model_admin.has_delete_permission(request, history))

    def test_read_only_coverage_audit_reports_history_completeness(self):
        Tender.objects.create(purchase_number="audited")
        out = StringIO()
        with patch("tender_selection.gosplan.fetch_page") as upstream:
            call_command("audit_discovery_coverage", stdout=out)
        upstream.assert_not_called()
        summary = json.loads(out.getvalue())["tender_history"]
        self.assertEqual(summary["working_cards_missing_history"], 0)
        self.assertEqual(summary["own_stats_missing_history"], 0)


class TenderHistoryMigrationTests(TransactionTestCase):
    migrate_from = ("tender_selection", "0044_discoverycursor")
    migrate_to = ("tender_selection", "0047_backfill_tender_history")

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old = executor.loader.project_state([self.migrate_from]).apps
        self.OldTender = old.get_model("tender_selection", "Tender")
        self.OldTrace = old.get_model("tender_selection", "IncomingTrace")
        self.OldStat = old.get_model("tender_selection", "ContractStat")
        self.OldFeedback = old.get_model("tender_selection", "TenderDismissalFeedback")

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_backfill_merges_duplicates_preserves_all_old_facts_and_working_data(self):
        tender = self.OldTender.objects.create(purchase_number="legacy", source=Tender.EAT, title="Кружки",
            contract_price=Decimal("80"), contract_reduction_percent=Decimal("20"),
            market_forecast_percent=Decimal("15"), market_forecast_sample_count=7)
        traces = [self.OldTrace.objects.create(purchase_number="legacy", title="Старое название", plus_hits=["кружк"], filtered_out=True),
                  self.OldTrace.objects.create(purchase_number="legacy", title="Новое название", plus_hits=["сувенир"], filtered_out=True)]
        orphan = self.OldTrace.objects.create(purchase_number="orphan", title="Неизвестный источник")
        for contract, amount in (("one", "70"), ("two", "80")):
            self.OldStat.objects.create(purchase_number="legacy", contract_reg_num=contract, own_funnel=True,
                                       final_price=Decimal(amount), discount_pct=Decimal("20"))
        market = self.OldStat.objects.create(purchase_number="market", contract_reg_num="market", own_funnel=False)
        self.OldFeedback.objects.create(tender_id=tender.pk, reason="not_profile")
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        History = apps.get_model("tender_selection", "TenderHistory")
        Stat = apps.get_model("tender_selection", "ContractStat")
        history = History.objects.get(tender_id=tender.pk)
        self.assertEqual(history.source, Tender.EAT)
        self.assertEqual(history.title, "Кружки")
        self.assertEqual(history.final_price, Decimal("80"))
        self.assertEqual(history.forecast_percent, Decimal("15"))
        self.assertEqual(history.reason_code, "not_profile")
        self.assertEqual({r["id"] for r in history.legacy_data["incoming_traces"]}, {r.pk for r in traces})
        self.assertEqual(len(history.legacy_data["contract_stats"]), 2)
        self.assertEqual(Stat.objects.filter(history_id=history.pk).count(), 2)
        self.assertIsNone(Stat.objects.get(pk=market.pk).history_id)
        self.assertEqual(History.objects.get(pk=orphan.pk).source, "unknown")
        self.assertEqual(apps.get_model("tender_selection", "Tender").objects.count(), 1)
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        reversed_apps = executor.loader.project_state([self.migrate_from]).apps
        Trace = reversed_apps.get_model("tender_selection", "IncomingTrace")
        self.assertEqual(Trace.objects.get(pk=traces[0].pk).title, "Старое название")
        self.assertEqual(Trace.objects.get(pk=traces[1].pk).title, "Новое название")
        self.assertEqual(reversed_apps.get_model("tender_selection", "ContractStat").objects.count(), 3)
