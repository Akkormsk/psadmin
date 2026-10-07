from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from tender_selection.models import Tender
from .calculation_v2_pipeline import GatewayBatchRouter, GatewayDocumentEnricher
from .models import ProcessDefinition, TenderSourceItem


class GatewayAdapterTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="adapter-1", title="Adapter")
        self.item = TenderSourceItem.objects.create(tender=self.tender, source_key="n:1", original_text="Item")
        self.process = ProcessDefinition.objects.create(name="Adapter process", role="supply")

    @patch("tenders.services._ai_gateway_json")
    def test_router_accepts_valid_structured_batch(self, gateway):
        gateway.return_value = ({"items": [{"source_item_id": self.item.pk, "process_id": self.process.pk, "confidence": 0.9, "reason": "known", "needs_review": False}]}, {"prompt_tokens": 10, "completion_tokens": 2})
        decisions = GatewayBatchRouter().route(tender=self.tender, items=[self.item], processes=[self.process], knowledge=[])
        self.assertEqual(decisions[0].process_id, self.process.pk)
        self.assertFalse(decisions[0].needs_review)
        self.assertEqual(gateway.call_count, 1)

    @patch("tenders.services._ai_gateway_json")
    def test_router_rejects_unknown_and_omitted_items(self, gateway):
        gateway.return_value = ({"items": [{"source_item_id": self.item.pk, "process_id": 999999, "confidence": 0.9}]}, {})
        decision = GatewayBatchRouter().route(tender=self.tender, items=[self.item], processes=[self.process], knowledge=[])[0]
        self.assertTrue(decision.needs_review)
        gateway.return_value = ({"items": []}, {})
        omitted = GatewayBatchRouter().route(tender=self.tender, items=[self.item], processes=[self.process], knowledge=[])[0]
        self.assertTrue(omitted.needs_review)

    @patch("tenders.calculation_v2_pipeline.DocumentPreview.objects.filter")
    @patch("tenders.services._ai_gateway_json")
    def test_enricher_requires_selected_source_url(self, gateway, previews):
        previews.return_value.values_list.return_value.first.return_value = "spec text"
        gateway.side_effect = [({"relevant_urls": ["https://doc"]}, {}), ({"items": [{"name": "A", "quantity": 2, "source_url": "https://wrong", "confidence": 0.9}]}, {})]
        result = GatewayDocumentEnricher().extract(tender=self.tender, aggregate=self.item, documents=[{"url": "https://doc", "name": "spec"}])
        self.assertEqual(result, [])