from decimal import Decimal

from django.test import TestCase

from tender_selection.models import Tender
from .calculation_v2_pipeline import ExtractedItem, RouteDecision, assess_quality, build_commercial_items, enrich_suspicious_tender, ingest_source_items, route_tender_batch
from .models import CalculationComponent, ComponentOperationStep, ProcessDefinition, TenderComputeJob, TenderCommercialItem
from .test_calculation_v2_pipeline import notification, raw_item


class CommercialItemModelTests(TestCase):
    def tender(self, name, quantity="1"):
        return Tender.objects.create(
            purchase_number=f"commercial-{Tender.objects.count() + 1}", title=name,
            notification_raw=notification([raw_item(name, quantity)], [{"fileName": "Spec", "url": "https://example.test/spec"}]),
        )

    def materialize(self, tender, extracted):
        class Enricher:
            diagnostics = {"outcome": "success"}
            cost_rub = 0
            def extract(self, **kwargs): return extracted
        ingest_source_items(tender)
        enrich_suspicious_tender(tender, {"enrichment_recommended": True, "documents": [{"url": "https://example.test/spec"}]}, Enricher())
        job = TenderComputeJob.objects.create(tender=tender)
        return build_commercial_items(job)

    def test_enrich_keeps_one_commercial_item_without_unnecessary_split(self):
        tender = self.tender("Paper bag", "500")
        rows = self.materialize(tender, [ExtractedItem("Paper bag", Decimal("500"), "шт", {"density": "200"}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "enrich")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].structure, TenderCommercialItem.Structure.SIMPLE)
        self.assertEqual(rows[0].components.count(), 1)

    def test_split_creates_independent_commercial_items(self):
        tender = self.tender("Manufacturing service")
        rows = self.materialize(tender, [
            ExtractedItem("Independent A", Decimal("1"), "шт", {}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "split"),
            ExtractedItem("Independent B", Decimal("1"), "шт", {}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "split"),
        ])
        self.assertEqual({item.display_name for item in rows}, {"Independent A", "Independent B"})
        self.assertTrue(all(item.components.count() == 1 for item in rows))

    def test_composite_preserves_commercial_identity_and_quantity_relation(self):
        tender = self.tender("Commercial bundle", "100")
        rows = self.materialize(tender, [
            ExtractedItem("Component A", None, "шт", {}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "component", Decimal("1")),
            ExtractedItem("Component B", None, "шт", {}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "component", Decimal("2")),
        ])
        self.assertEqual(len(rows), 1)
        commercial = rows[0]
        self.assertEqual(commercial.display_name, "Commercial bundle")
        self.assertEqual(commercial.structure, TenderCommercialItem.Structure.COMPOSITE)
        components = {component.name: component for component in commercial.components.all()}
        self.assertEqual(set(components), {"Component A", "Component B"})
        self.assertEqual(components["Component A"].effective_quantity, Decimal("100"))
        self.assertEqual(components["Component B"].effective_quantity, Decimal("200"))

    def test_generic_purchasable_set_is_not_componentized_without_evidence(self):
        tender = self.tender("Purchasable set", "12")
        rows = self.materialize(tender, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].structure, TenderCommercialItem.Structure.SIMPLE)
        self.assertEqual(rows[0].components.count(), 1)
    def test_component_route_plan_keeps_ordered_steps_without_fixed_process_count(self):
        tender = self.tender("Commercial bundle", "2")
        commercial = self.materialize(tender, [ExtractedItem("Component", None, "шт", {}, {"document_url": "https://example.test/spec"}, Decimal(".9"), "component")])[0]
        first = ProcessDefinition.objects.create(name="Supply", role=ProcessDefinition.ROLE_SUPPLY)
        second = ProcessDefinition.objects.create(name="Mark", role=ProcessDefinition.ROLE_PRODUCTION)
        job = commercial.job
        class Router:
            def route(self, *, items, **kwargs):
                return [RouteDecision(item.pk, (first.pk, second.pk), Decimal(".9"), {}) for item in items]
        route_tender_batch(job, Router())
        component = commercial.components.get()
        self.assertEqual(list(component.route_plans.get().steps.values_list("process_id", flat=True)), [first.pk, second.pk])
