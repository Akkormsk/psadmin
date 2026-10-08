from decimal import Decimal
import json

from django.test import TestCase, override_settings

from tender_selection.models import FilterSettings, Tender
from .calculation_v2_pipeline import (
    ExistingKnowledgeBatchRouter,
    ExtractedItem,
    RouteDecision,
    active_calculation_items,
    assess_quality,
    build_commercial_items,
    eligible_backfill_tenders,
    enrich_suspicious_tender,
    ingest_source_items,
    queue_visible_backfill,
    route_tender_batch,
    run_next_tender_understanding_job,
    trigger_visible_tender,
)
from .models import Lesson, OwnerInteraction, ProcessDefinition, TenderComputeJob, TenderSourceItem


def notification(items, documents=None):
    return {"source": {"commonInfo": {"purchaseObjectInfo": "Supply"}, "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": items}}}, "attachmentsInfo": {"attachmentInfo": documents or []}}}


def raw_item(name, quantity="1", code="", characteristics=None):
    return {"name": name, "quantity": {"value": quantity}, "OKEI": {"nationalCode": "796"}, "OKPD2": {"code": code, "characteristics": {"characteristicsUsingTextForm": characteristics or []}}}


class FakeEnricher:
    def extract(self, *, tender, aggregate, documents):
        return [
            ExtractedItem("Part A", Decimal("2"), "796", {}, {"document_url": documents[0]["url"], "page": 1}, Decimal("0.9")),
            ExtractedItem("Part B", Decimal("3"), "796", {}, {"document_url": documents[0]["url"], "page": 2}, Decimal("0.9")),
        ]


class CalculationV2PipelineTests(TestCase):
    def setUp(self):
        self.settings = FilterSettings.load()
        self.settings.min_price = 0
        self.settings.include_words = ""
        self.settings.exclude_words = ""
        self.settings.save()

    def tender(self, *, title="Supply", payload=None):
        return Tender.objects.create(purchase_number=f"v2-pipeline-{Tender.objects.count() + 1}", title=title, notification_raw=payload or {})

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_visible_trigger_is_idempotent_and_filtered_out_tender_is_ignored(self):
        visible = self.tender(payload=notification([raw_item("Widget", "2", "a")]))
        first = trigger_visible_tender(visible)
        self.assertEqual(first.pk, trigger_visible_tender(visible).pk)
        self.settings.include_words = "required"
        self.settings.save()
        hidden = self.tender(title="other", payload=notification([raw_item("Other")]))
        self.assertIsNone(trigger_visible_tender(hidden))
        self.assertEqual(TenderComputeJob.objects.count(), 1)

    def test_quality_diagnostics_with_document_date_are_json_serializable(self):
        tender = self.tender(payload=notification(
            [raw_item("Supply", "1")],
            [{"fileName": "Spec", "url": "https://zakupki.gov.ru/doc", "docDate": "2026-10-01T03:00:00+03:00"}],
        ))
        self.assertIsInstance(json.dumps(assess_quality(tender, ingest_source_items(tender))), str)

    def test_ingestion_is_idempotent_and_changed_original_is_superseded(self):
        tender = self.tender(payload=notification([raw_item("Widget", "2", "code")]))
        first = ingest_source_items(tender)[0]
        self.assertEqual(first.pk, ingest_source_items(tender)[0].pk)
        tender.notification_raw = notification([raw_item("Widget revised", "2", "code")])
        tender.save(update_fields=["notification_raw"])
        changed = ingest_source_items(tender)[0]
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(changed.supersedes_id, first.pk)
        self.assertEqual(TenderSourceItem.objects.filter(tender=tender).count(), 2)

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_normal_tender_skips_enrichment_and_routes_in_one_batch(self):
        process = ProcessDefinition.objects.create(name="Generic route", role=ProcessDefinition.ROLE_SUPPLY)
        Lesson.objects.create(scope="route", admin_text="Known", item_word="widget", outcome={"route": {"processes": [{"process_id": str(process.pk)}]}})
        tender = self.tender(payload=notification([raw_item("Widget", "2", "code")]))
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter())
        self.assertEqual(job.status, TenderComputeJob.Status.READY)
        self.assertEqual(job.diagnostics["routing_batch_count"], 1)
        self.assertFalse(job.diagnostics["enrichment_used"])
        self.assertEqual(job.lines.get().route_key, str(process.pk))

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_suspicious_aggregate_creates_derived_canonical_items_with_provenance(self):
        tender = self.tender(payload=notification([raw_item("Supply", "1")], [{"fileName": "Spec", "url": "https://zakupki.gov.ru/doc"}]))
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=FakeEnricher())
        originals = TenderSourceItem.objects.filter(tender=tender, source_type="notification")
        self.assertFalse(originals.get().is_active)
        active = active_calculation_items(tender)
        self.assertEqual(sorted(item.original_text for item in active), ["Part A", "Part B"])
        self.assertTrue(all(item.provenance["document_url"] for item in active))
        self.assertEqual(job.diagnostics["derived_item_count"], 2)

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_uncertain_route_creates_channel_independent_interaction(self):
        tender = self.tender(payload=notification([raw_item("Unknown", "2", "code")]))
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter())
        self.assertEqual(job.status, TenderComputeJob.Status.NEEDS_REVIEW)
        interaction = OwnerInteraction.objects.get(tender=tender)
        self.assertEqual(interaction.status, "open")
        self.assertTrue(interaction.question)

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_backfill_queues_only_unprocessed_visible_incoming_tenders(self):
        self.settings.include_words = "логотип"
        self.settings.save()
        visible = self.tender(title="Папки с логотипом", payload=notification([raw_item("Folder")]))
        hidden = self.tender(title="Жалюзи", payload=notification([raw_item("Blind")]))
        processed = self.tender(title="Блокноты с логотипом", payload=notification([raw_item("Notebook")]))
        TenderComputeJob.objects.create(tender=processed, version="v2", status=TenderComputeJob.Status.READY)

        self.assertEqual([tender.pk for tender in eligible_backfill_tenders(limit=10)], [visible.pk])
        jobs = queue_visible_backfill(limit=10)
        self.assertEqual([job.tender_id for job in jobs], [visible.pk])
        self.assertEqual(TenderComputeJob.objects.filter(tender=hidden).count(), 0)
        self.assertEqual(TenderComputeJob.objects.filter(tender=processed).count(), 1)

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_thirty_and_hundred_items_are_one_batch_not_per_line(self):
        for count in (30, 100):
            TenderComputeJob.objects.all().delete()
            tender = self.tender(payload=notification([raw_item(f"Item {index}", "2", str(index)) for index in range(count)]))
            trigger_visible_tender(tender)
            job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter())
            self.assertEqual(job.diagnostics["source_item_count"], count)
            self.assertEqual(job.diagnostics["routing_item_count"], count)
            self.assertEqual(job.diagnostics["routing_batch_count"], 1)


class OwnerQuestionEvidenceTests(TestCase):
    def component(self, requirements, *, quantity="1", commercial_requirements=None):
        tender = Tender.objects.create(
            purchase_number=f"question-evidence-{Tender.objects.count() + 1}",
            title="Evidence item",
            notification_raw=notification([raw_item("Evidence item", quantity)]),
        )
        source = ingest_source_items(tender)[0]
        source.requirements = requirements
        source.save(update_fields=["requirements"])
        job = TenderComputeJob.objects.create(tender=tender)
        commercial = build_commercial_items(job)[0]
        if commercial_requirements is not None:
            commercial.requirements = commercial_requirements
            commercial.save(update_fields=["requirements"])
        return job

    def route(self, job, *, missing, question="Уточните характеристику."):
        process = ProcessDefinition.objects.create(
            name=f"Execution capability {ProcessDefinition.objects.count() + 1}",
            role=ProcessDefinition.ROLE_PRODUCTION,
            performs_production=True,
        )

        class Router:
            def route(self, *, items, **kwargs):
                return [RouteDecision(
                    item.pk, (process.pk,), Decimal(".9"), {}, True, question,
                    "missing_information", tuple(missing),
                ) for item in items]

        return route_tender_batch(job, Router())

    def test_known_document_fact_suppresses_missing_fact_question(self):
        job = self.component({"Высота": "32 см"})
        decisions = self.route(job, missing=["Высота"], question="Укажите высоту изделия.")
        self.assertFalse(decisions[0].needs_review)
        self.assertFalse(OwnerInteraction.objects.filter(tender=job.tender, status="open").exists())

    def test_agreed_quantity_does_not_create_question(self):
        job = self.component({}, quantity="500")
        self.route(job, missing=["Количество"], question="Укажите количество.")
        self.assertFalse(OwnerInteraction.objects.filter(tender=job.tender, status="open").exists())

    def test_conflicting_evidence_keeps_owner_question(self):
        job = self.component({"characteristics": [
            {"name": "Материал", "value": "бумага"},
            {"name": "Материал", "value": "пластик"},
        ]})
        decisions = self.route(job, missing=["Материал"], question="Уточните материал.")
        self.assertTrue(decisions[0].needs_review)
        self.assertTrue(OwnerInteraction.objects.filter(tender=job.tender, status="open").exists())

    def test_missing_fact_keeps_owner_question(self):
        job = self.component({})
        self.route(job, missing=["Плотность"], question="Укажите плотность.")
        self.assertTrue(OwnerInteraction.objects.filter(tender=job.tender, status="open").exists())

    def test_resolvable_alternate_label_suppresses_duplicate_question(self):
        job = self.component({"Плотность бумаги, г/м2": "200"})
        self.route(job, missing=["Плотность бумаги"], question="Укажите плотность бумаги.")
        self.assertFalse(OwnerInteraction.objects.filter(tender=job.tender, status="open").exists())

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_extraction_failure_is_partial_without_owner_question(self):
        class FailingEnricher:
            diagnostics = {"outcome": "system_extraction_failure"}
            cost_rub = 0

            def extract(self, **kwargs):
                return []

        tender = Tender.objects.create(
            purchase_number=f"question-failure-{Tender.objects.count() + 1}",
            title="Sparse item",
            notification_raw=notification([raw_item("Sparse item")], [{"fileName": "Spec", "url": "https://zakupki.gov.ru/spec"}]),
        )
        TenderComputeJob.objects.create(tender=tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=FailingEnricher())
        self.assertEqual(job.status, TenderComputeJob.Status.PARTIAL)
        self.assertFalse(OwnerInteraction.objects.filter(tender=tender, status="open").exists())


class FullCompositionRegressionTests(TestCase):
    def setUp(self):
        settings = FilterSettings.load()
        settings.min_price = 0
        settings.include_words = ""
        settings.exclude_words = ""
        settings.save()

    def tender(self, items):
        return Tender.objects.create(
            purchase_number=f"full-composition-{Tender.objects.count() + 1}",
            title="Поставка сувенирной продукции",
            notification_raw=notification(items, [{"fileName": "Техническое задание", "url": "https://zakupki.gov.ru/spec"}]),
        )

    def test_document_split_keeps_the_original_and_unrelated_notification_items(self):
        class BundleEnricher:
            diagnostics = {"outcome": "success"}
            cost_rub = 0

            def extract(self, **kwargs):
                if kwargs["aggregate"].original_text != "Набор сувенирный":
                    return []
                document = kwargs["documents"][0]["url"]
                return [
                    ExtractedItem("Ручка", Decimal("100"), "шт.", {"Материал": "пластик"}, {"document_url": document, "page_or_section": "таблица 2"}, Decimal("0.95")),
                    ExtractedItem("Блокнот", Decimal("100"), "шт.", {"Формат": "A5"}, {"document_url": document, "page_or_section": "таблица 2"}, Decimal("0.95")),
                    ExtractedItem("Кружка", Decimal("100"), "шт.", {"Объём": "330 мл"}, {"document_url": document, "page_or_section": "таблица 2"}, Decimal("0.95")),
                ]

        tender = self.tender([raw_item("Набор сувенирный", "100"), raw_item("Инструкция", "100")])
        originals = ingest_source_items(tender)
        result = enrich_suspicious_tender(tender, assess_quality(tender, originals), BundleEnricher())

        self.assertEqual(result["state"], "success")
        self.assertEqual(sorted(item.original_text for item in active_calculation_items(tender)), ["Блокнот", "Инструкция", "Кружка", "Ручка"])
        source = TenderSourceItem.objects.get(tender=tender, source_type="notification", original_text="Набор сувенирный")
        self.assertFalse(source.is_active)
        self.assertEqual(source.derived_items.filter(is_active=True).count(), 3)
        self.assertEqual(source.derived_items.filter(is_active=True).first().provenance["page_or_section"], "таблица 2")

    def test_document_that_adds_no_fact_does_not_create_an_enriched_item(self):
        class SameAsNotificationEnricher:
            diagnostics = {"outcome": "success"}
            cost_rub = 0

            def extract(self, **kwargs):
                document = kwargs["documents"][0]["url"]
                return [ExtractedItem("Папка", Decimal("20"), "796", {}, {"document_url": document, "page_or_section": "таблица 1"}, Decimal("0.95"))]

        tender = self.tender([raw_item("Папка", "20")])
        originals = ingest_source_items(tender)
        result = enrich_suspicious_tender(tender, assess_quality(tender, originals), SameAsNotificationEnricher())

        self.assertEqual(result["state"], "no_change")
        self.assertFalse(TenderSourceItem.objects.filter(tender=tender, source_type="document_enrichment").exists())
        self.assertEqual([item.original_text for item in active_calculation_items(tender)], ["Папка"])

    def test_enriched_item_combines_notification_and_document_characteristics_with_sources(self):
        class DetailEnricher:
            diagnostics = {"outcome": "success"}
            cost_rub = 0

            def extract(self, **kwargs):
                document = kwargs["documents"][0]["url"]
                return [ExtractedItem("Папка", Decimal("20"), "796", {"Плотность": "250 г/м²"}, {"document_url": document, "page_or_section": "таблица 1"}, Decimal("0.95"))]

        tender = self.tender([raw_item("Папка", "20", characteristics=[{"name": "Цвет", "value": "синий"}])])
        originals = ingest_source_items(tender)
        originals[0].requirements = {"characteristics": [{"name": "Цвет", "value": "синий"}]}
        originals[0].save(update_fields=["requirements"])
        enrich_suspicious_tender(tender, assess_quality(tender, originals), DetailEnricher())

        enriched = TenderSourceItem.objects.get(tender=tender, source_type="document_enrichment")
        values = {(row["name"], row["value"], row.get("source")) for row in enriched.requirements["characteristics"]}
        self.assertIn(("Цвет", "синий", "notification"), values)
        self.assertIn(("Плотность", "250 г/м²", "document"), values)
