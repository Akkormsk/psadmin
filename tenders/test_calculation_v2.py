from datetime import timedelta
from threading import Barrier, Thread

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from tender_selection.models import Tender
from .calculation_v2 import (
    EngineRegistry,
    PreparationPlan,
    claim_next_job,
    enqueue_tender_compute,
    plan_preparations,
    plan_work_units,
    requeue_stale_jobs,
)
from .models import (
    KnowledgeRecord,
    OwnerFeedbackEvent,
    OwnerInteraction,
    TenderComputeJob,
    TenderComputeLine,
    TenderSourceItem,
)


class FakeEngine:
    def __init__(self, key, *, preparation_key="lookup"):
        self.key = key
        self.preparation_key = preparation_key

    def build_work_input(self, line):
        return {"normalized": line.input_snapshot["normalized"]}

    def dedupe_key(self, work_input):
        return work_input["normalized"]

    def plan_preparations(self, work_unit):
        return [PreparationPlan(self.preparation_key, freshness="event")]


class CalculationV2TestCase(TestCase):
    def make_tender(self):
        return Tender.objects.create(purchase_number=f"v2-{Tender.objects.count() + 1}", title="V2 test")

    def make_source(self, tender, key="line-1", **kwargs):
        return TenderSourceItem.objects.create(
            tender=tender, source_key=key, source_type="notification", original_text="Original request", **kwargs
        )

    def make_job_and_line(self, *, normalized="same", engine_key="fake.alpha"):
        tender = self.make_tender()
        source = self.make_source(tender)
        job = TenderComputeJob.objects.create(tender=tender)
        line = TenderComputeLine.objects.create(
            job=job, source_item=source, engine_key=engine_key, input_snapshot={"normalized": normalized}
        )
        return job, line

    def registry(self):
        registry = EngineRegistry()
        registry.register(FakeEngine("fake.alpha"))
        registry.register(FakeEngine("fake.beta", preparation_key="rates"))
        return registry

    @override_settings(CALCULATION_V2_ENABLED=False)
    def test_feature_flag_keeps_future_trigger_inert(self):
        self.assertIsNone(enqueue_tender_compute(self.make_tender()))

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_future_trigger_creates_tender_wide_job(self):
        job = enqueue_tender_compute(self.make_tender())
        self.assertEqual(job.trigger, "incoming_visible")
        self.assertEqual(job.status, TenderComputeJob.Status.QUEUED)

    def test_zero_one_thirty_and_hundred_source_items_persist(self):
        tender = self.make_tender()
        self.assertEqual(tender.v2_source_items.count(), 0)
        self.make_source(tender)
        TenderSourceItem.objects.bulk_create([
            TenderSourceItem(tender=tender, source_key=f"bulk-{index}", source_type="notification", original_text="item")
            for index in range(99)
        ])
        self.assertEqual(tender.v2_source_items.count(), 100)

    def test_many_lines_dedupe_to_one_work_unit(self):
        job, line = self.make_job_and_line()
        for index in range(29):
            source = self.make_source(job.tender, f"line-{index + 2}")
            TenderComputeLine.objects.create(
                job=job, source_item=source, engine_key="fake.alpha", input_snapshot={"normalized": "same"}
            )
        units = plan_work_units(job, self.registry())
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].lines.count(), 30)

    def test_multiple_engines_are_selected_from_registry_without_orchestration_branches(self):
        job, line = self.make_job_and_line(engine_key="fake.alpha", normalized="a")
        source = self.make_source(job.tender, "line-2")
        TenderComputeLine.objects.create(
            job=job, source_item=source, engine_key="fake.beta", input_snapshot={"normalized": "b"}
        )
        registry = self.registry()
        units = plan_work_units(job, registry)
        self.assertEqual({unit.engine_key for unit in units}, {"fake.alpha", "fake.beta"})
        self.assertEqual(registry.keys(), ("fake.alpha", "fake.beta"))

    def test_preparation_is_persisted_and_idempotent(self):
        job, line = self.make_job_and_line()
        unit = plan_work_units(job, self.registry())[0]
        first = plan_preparations(unit, self.registry())
        second = plan_preparations(unit, self.registry())
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].pk, second[0].pk)
        self.assertEqual(unit.preparations.get().freshness, "event")

    def test_source_provenance_and_superseding_do_not_delete_original(self):
        tender = self.make_tender()
        original = self.make_source(tender, "original", provenance={"kind": "notification"})
        derived = self.make_source(tender, "derived", parent=original, provenance={"kind": "enriched"})
        replacement = self.make_source(tender, "replacement", supersedes=derived, provenance={"kind": "manual"})
        derived.is_active = False
        derived.save(update_fields=["is_active"])
        self.assertEqual(TenderSourceItem.objects.filter(tender=tender).count(), 3)
        self.assertEqual(replacement.supersedes_id, derived.pk)
        self.assertEqual(derived.parent_id, original.pk)

    def test_feedback_is_immutable_and_never_auto_promotes_to_knowledge(self):
        tender = self.make_tender()
        interaction = OwnerInteraction.objects.create(tender=tender, question="Which material?")
        event = OwnerFeedbackEvent.objects.create(interaction=interaction, raw_text="Use approved material")
        self.assertFalse(KnowledgeRecord.objects.filter(feedback_event=event).exists())
        event.raw_text = "Changed"
        with self.assertRaises(ValidationError):
            event.save()
        knowledge = KnowledgeRecord.objects.create(
            feedback_event=event, scope_type="current_tender", scope_context={"tender_id": tender.pk}, status="draft"
        )
        self.assertEqual(knowledge.status, "draft")

    def test_stale_preparation_phase_is_retryable_after_worker_crash(self):
        job, _ = self.make_job_and_line()
        TenderComputeJob.objects.filter(pk=job.pk).update(
            status=TenderComputeJob.Status.ROUTING,
            started_at=timezone.now() - timedelta(minutes=16),
        )
        self.assertEqual(requeue_stale_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, TenderComputeJob.Status.QUEUED)
        self.assertIsNone(job.started_at)

    def test_claim_and_stale_retry_are_persisted(self):
        job, line = self.make_job_and_line()
        claimed = claim_next_job()
        self.assertEqual(claimed.pk, job.pk)
        self.assertIsNone(claim_next_job())
        TenderComputeJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(requeue_stale_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, TenderComputeJob.Status.QUEUED)
        self.assertIsNone(job.started_at)


class ConcurrentClaimTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="v2-concurrent", title="V2 concurrent")
        self.job = TenderComputeJob.objects.create(tender=self.tender)

    def test_two_workers_cannot_claim_the_same_job(self):
        barrier = Barrier(2)
        results = []

        def worker():
            from django.db import close_old_connections
            close_old_connections()
            barrier.wait()
            job = claim_next_job()
            results.append(job.pk if job else None)
            close_old_connections()

        first = Thread(target=worker)
        second = Thread(target=worker)
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(sorted(result for result in results if result is not None), [self.job.pk])
        self.assertEqual(results.count(None), 1)