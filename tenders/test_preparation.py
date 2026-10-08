from datetime import timedelta
from threading import Barrier, Thread

from django.test import TestCase, TransactionTestCase, override_settings
from unittest.mock import patch
from django.utils import timezone

from tender_selection.models import Tender

from .models import (
    CalculationComponent, ComponentOperationStep, ComponentRoutePlan, ProcessDefinition,
    CatalogProduct, CatalogSupplier, TenderCommercialItem, TenderComputeJob, TenderComputePreparation,
)
from .preparation import (
    claim_next_preparation, eligible_tenders, is_preparation_eligible, persist_preparation_tasks, planned_tasks,
    refresh_work_unit_status, requeue_stale_preparations, sweep_preparation,
)


class PreparationTestCase(TestCase):
    def tender(self, *, working=True):
        return Tender.objects.create(
            purchase_number=f"prep-{Tender.objects.count() + 1}", title="preparation",
            review=Tender.INTERESTING if working else Tender.UNREVIEWED,
        )

    def job_with_steps(self, count=1):
        tender = self.tender()
        job = TenderComputeJob.objects.create(tender=tender, status=TenderComputeJob.Status.READY)
        commercial = TenderCommercialItem.objects.create(tender=tender, job=job, source_key="item", display_name="Marker")
        component = CalculationComponent.objects.create(commercial_item=commercial, name="Marker")
        process = ProcessDefinition.objects.create(
            name=f"Supply {Tender.objects.count()}", role=ProcessDefinition.ROLE_SUPPLY, supplies_input=True
        )
        steps = []
        for position in range(count):
            plan = ComponentRoutePlan.objects.create(commercial_item=commercial, component=component)
            steps.append(ComponentOperationStep.objects.create(route_plan=plan, process=process, position=position))
        return tender, job, steps

    def test_rejected_excluded_and_working_discovered(self):
        rejected = Tender.objects.create(purchase_number="prep-rejected", title="x", status=Tender.DISMISSED)
        working = self.tender()
        self.assertIn(working, eligible_tenders())
        self.assertNotIn(rejected, eligible_tenders())

    def test_kanban_tender_without_v2_job_is_eligible_for_reconciliation(self):
        tender = Tender.objects.create(purchase_number="prep-kanban", title="x", status=Tender.PUSHED)
        self.assertTrue(is_preparation_eligible(tender))

    def test_dry_run_creates_nothing(self):
        tender, job, steps = self.job_with_steps()
        self.assertTrue(sweep_preparation(limit=10, dry_run=True)["dry_run"])
        self.assertFalse(TenderComputePreparation.objects.exists())

    def test_apply_queues_missing_understanding_job_once(self):
        tender = self.tender()
        result = sweep_preparation(limit=10, dry_run=False)
        self.assertEqual(result["understanding_queued"], 1)
        self.assertEqual(TenderComputeJob.objects.filter(tender=tender, trigger="preparation_reconciliation").count(), 1)
        self.assertEqual(sweep_preparation(limit=10, dry_run=False)["understanding_queued"], 0)

    def test_thirty_matching_steps_plan_one_work_unit(self):
        tender, job, steps = self.job_with_steps(count=30)
        tasks = planned_tasks(job)
        preparations, reused = persist_preparation_tasks(job, tasks)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(preparations), 1)
        self.assertEqual(preparations[0].work_unit.operation_steps.count(), 30)
        self.assertEqual(reused, 0)

    def test_thirty_items_are_grouped_by_preparation_semantics(self):
        tender = self.tender()
        job = TenderComputeJob.objects.create(tender=tender, status=TenderComputeJob.Status.READY)
        process = ProcessDefinition.objects.create(
            name="Shared supply", role=ProcessDefinition.ROLE_SUPPLY, supplies_input=True
        )
        for index in range(30):
            commercial = TenderCommercialItem.objects.create(
                tender=tender, job=job, source_key=f"item-{index}", display_name="Marker"
            )
            component = CalculationComponent.objects.create(commercial_item=commercial, name="Marker")
            route = ComponentRoutePlan.objects.create(commercial_item=commercial, component=component)
            ComponentOperationStep.objects.create(route_plan=route, process=process, position=0)
        tasks = planned_tasks(job)
        preparations, _ = persist_preparation_tasks(job, tasks)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(preparations[0].work_unit.operation_steps.count(), 30)

    def test_second_plan_reuses_ready_preparation(self):
        tender, job, steps = self.job_with_steps()
        preparations, _ = persist_preparation_tasks(job, planned_tasks(job))
        TenderComputePreparation.objects.filter(pk=preparations[0].pk).update(status="ready")
        preparations, reused = persist_preparation_tasks(job, planned_tasks(job))
        self.assertEqual(len(preparations), 1)
        self.assertEqual(reused, 1)

    def test_dry_run_distinguishes_current_preparation(self):
        tender, job, steps = self.job_with_steps()
        preparations, _ = persist_preparation_tasks(job, planned_tasks(job))
        TenderComputePreparation.objects.filter(pk=preparations[0].pk).update(status="ready")
        result = sweep_preparation(limit=10, dry_run=True)
        self.assertEqual(result["already_warm"], 1)
        self.assertEqual(result["queued"], 0)

    def test_stale_running_preparation_is_retryable(self):
        tender, job, steps = self.job_with_steps()
        preparations, _ = persist_preparation_tasks(job, planned_tasks(job))
        TenderComputePreparation.objects.filter(pk=preparations[0].pk).update(
            status="running", started_at=timezone.now() - timedelta(minutes=16)
        )
        self.assertEqual(requeue_stale_preparations(), 1)

    def test_ready_preparation_marks_work_unit_ready(self):
        tender, job, steps = self.job_with_steps()
        preparations, _ = persist_preparation_tasks(job, planned_tasks(job))
        preparation = preparations[0]
        preparation.status = "ready"
        preparation.save(update_fields=["status"])
        refresh_work_unit_status(preparation)
        self.assertEqual(preparation.work_unit.status, "ready")

    @override_settings(STEP4_DECISION_CACHE_ENABLED=True)
    def test_warm_step4_decision_is_reused_without_gemini(self):
        from .catalog_preparation import CatalogPreparationEngine
        from .step4_decision_cache import Step4Decision, Step4DecisionWrite, bulk_store_step4_decisions
        tender, job, steps = self.job_with_steps()
        supplier = CatalogSupplier.objects.create(code="prep-supplier", name="Supplier", base_url="https://example.test")
        product = CatalogProduct.objects.create(supplier=supplier, external_id="p1", name="Marker")
        bulk_store_step4_decisions(target="Marker", supplier=supplier, decisions=[
            Step4DecisionWrite("p1", "Marker", Step4Decision.PASS, "test")
        ])
        preparation = TenderComputePreparation.objects.create(
            work_unit=job.work_units.create(engine_key="catalog-search-v1", dedupe_key="marker", input_fingerprint="marker"),
            engine_key="catalog-search-v1", preparation_key="prepare", payload={"target": "Marker"},
        )
        with patch("tenders.catalog_preparation._text_search_pool", return_value=[product]), patch("tenders.catalog_preparation._run_name_filter") as provider:
            result = CatalogPreparationEngine().prepare(preparation)
        self.assertEqual(result["status"], "ready")
        provider.assert_not_called()
        preparation.refresh_from_db()
        self.assertIn("search_ms", preparation.payload["diagnostics"])
        self.assertIn("cache_lookup_ms", preparation.payload["diagnostics"])

    @override_settings(STEP4_DECISION_CACHE_ENABLED=True)
    def test_provider_failure_creates_no_cache_truth(self):
        from .catalog_preparation import CatalogPreparationEngine
        from .step4_decision_cache import bulk_lookup_step4_decisions, Step4ProductCandidate
        tender, job, steps = self.job_with_steps()
        supplier = CatalogSupplier.objects.create(code="prep-provider", name="Supplier", base_url="https://example.test")
        product = CatalogProduct.objects.create(supplier=supplier, external_id="p2", name="Marker")
        preparation = TenderComputePreparation.objects.create(
            work_unit=job.work_units.create(engine_key="catalog-search-v1", dedupe_key="marker", input_fingerprint="marker"),
            engine_key="catalog-search-v1", preparation_key="prepare", payload={"target": "Marker"},
        )
        with patch("tenders.catalog_preparation._text_search_pool", return_value=[product]), patch("tenders.catalog_preparation._run_name_filter", return_value=None):
            result = CatalogPreparationEngine().prepare(preparation)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(bulk_lookup_step4_decisions(target="Marker", supplier=supplier, candidates=[Step4ProductCandidate("p2", "Marker")]).hits, {})


class PreparationClaimTests(TransactionTestCase):
    reset_sequences = True

    def test_two_workers_do_not_claim_one_preparation(self):
        tender = Tender.objects.create(purchase_number="prep-claim", title="x", review=Tender.INTERESTING)
        job = TenderComputeJob.objects.create(tender=tender, status=TenderComputeJob.Status.READY)
        unit = job.work_units.create(engine_key="fake", dedupe_key="one", input_fingerprint="one")
        TenderComputePreparation.objects.create(work_unit=unit, engine_key="fake", preparation_key="prepare")
        barrier, results = Barrier(2), []

        def worker():
            from django.db import close_old_connections
            close_old_connections()
            barrier.wait()
            claimed = claim_next_preparation()
            results.append(claimed.pk if claimed else None)
            close_old_connections()

        first, second = Thread(target=worker), Thread(target=worker)
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(len([result for result in results if result is not None]), 1)
