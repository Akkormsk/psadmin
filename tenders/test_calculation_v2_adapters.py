from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from tender_selection.models import Tender
from .calculation_v2_pipeline import GatewayBatchRouter, GatewayDocumentEnricher
from .models import CalculationComponent, ProcessDefinition, TenderCommercialItem, TenderSourceItem


class GatewayAdapterTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="adapter-1", title="Adapter")
        self.item = TenderSourceItem.objects.create(tender=self.tender, source_key="n:1", original_text="Item")
        commercial = TenderCommercialItem.objects.create(tender=self.tender, source_key="n:1", display_name="Item")
        commercial.source_items.add(self.item)
        self.component = CalculationComponent.objects.create(commercial_item=commercial, source_item=self.item, name="Item")
        self.process = ProcessDefinition.objects.create(name="Adapter process", role="supply")

    @patch("tenders.services._ai_gateway_json")
    def test_router_accepts_valid_structured_batch(self, gateway):
        gateway.return_value = ({"items": [{"component_id": self.component.pk, "process_ids": [self.process.pk], "confidence": 0.9, "reason": "known", "needs_review": False}]}, {"prompt_tokens": 10, "completion_tokens": 2})
        decisions = GatewayBatchRouter().route(tender=self.tender, items=[self.component], processes=[self.process], knowledge=[])
        self.assertEqual(decisions[0].process_ids, (self.process.pk,))
        self.assertFalse(decisions[0].needs_review)
        self.assertEqual(gateway.call_count, 1)
        self.assertIn('"component_id"', gateway.call_args.args[0])

    @patch("tenders.services._ai_gateway_json")
    def test_router_rejects_unknown_and_omitted_items(self, gateway):
        gateway.return_value = ({"items": [{"component_id": self.component.pk, "process_ids": [999999], "confidence": 0.9}]}, {})
        decision = GatewayBatchRouter().route(tender=self.tender, items=[self.component], processes=[self.process], knowledge=[])[0]
        self.assertTrue(decision.needs_review)
        gateway.return_value = ({"items": []}, {})
        omitted = GatewayBatchRouter().route(tender=self.tender, items=[self.component], processes=[self.process], knowledge=[])[0]
        self.assertTrue(omitted.needs_review)

    @patch("tenders.calculation_v2_pipeline.DocumentPreview.objects.filter")
    @patch("tenders.services._ai_gateway_json")
    def test_enricher_requires_selected_source_url(self, gateway, previews):
        previews.return_value.values_list.return_value.first.return_value = "spec text"
        gateway.side_effect = [({"relevant_urls": ["https://doc"]}, {}), ({"items": [{"name": "A", "quantity": 2, "source_url": "https://wrong", "confidence": 0.9}]}, {})]
        result = GatewayDocumentEnricher().extract(tender=self.tender, aggregate=self.item, documents=[{"url": "https://doc", "name": "spec"}])
        self.assertEqual(result, [])

    @patch("tenders.calculation_v2_pipeline.DocumentPreview.objects.filter")
    @patch("tenders.services._ai_gateway_json")
    def test_enricher_reads_all_items_and_documents_once(self, gateway, previews):
        second = TenderSourceItem.objects.create(tender=self.tender, source_key="n:2", original_text="Second source")
        previews.return_value.values_list.return_value.first.return_value = "free document text"
        gateway.side_effect = [
            ({"relevant_urls": ["https://doc-a", "https://doc-b"]}, {}),
            ({"items": [
                {"parent_source_item_id": self.item.pk, "name": "Detail A", "quantity": 2, "unit": "u", "requirements": {}, "confidence": .9, "source_url": "https://doc-a", "page_or_section": "p. 1", "evidence": "Detail A", "relationship": "split"},
                {"parent_source_item_id": second.pk, "name": "Second source", "quantity": 3, "unit": "u", "requirements": {"Any field": "Any value"}, "confidence": .8, "source_url": "https://doc-b", "page_or_section": "p. 2", "evidence": "Any value", "relationship": "enrich"},
            ]}, {}),
        ]

        result = GatewayDocumentEnricher().extract_tender(
            tender=self.tender,
            source_items=[self.item, second],
            documents=[{"url": "https://doc-a", "name": "first"}, {"url": "https://doc-b", "name": "second"}],
        )

        self.assertEqual([entry.parent_source_item_id for entry in result], [self.item.pk, second.pk])
        self.assertEqual(gateway.call_count, 2)
        extraction_prompt = gateway.call_args_list[1].args[0]
        self.assertIn(f'"id": {self.item.pk}', extraction_prompt)
        self.assertIn(f'"id": {second.pk}', extraction_prompt)
        self.assertIn("free document text", extraction_prompt)
