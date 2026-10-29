
from decimal import Decimal
from django.test import TestCase, override_settings
from unittest.mock import patch
from tender_selection.models import DocumentPreview, FilterSettings, Tender
from .calculation_v2_pipeline import ExtractedItem, ExistingKnowledgeBatchRouter, GatewayDocumentEnricher, run_next_tender_understanding_job, trigger_visible_tender
from .document_enrichment import document_table_payload
from .models import OwnerInteraction, TenderComputeJob, TenderSourceItem


def _payload():
    return {"source": {"commonInfo": {"purchaseObjectInfo": "Supply"}, "notificationInfo": {"purchaseObjectsInfo": {"notDrugPurchaseObjectsInfo": {"purchaseObject": [{"name": "Package", "quantity": {"value": "500"}, "OKEI": {"nationalCode": "796"}, "OKPD2": {"characteristics": {"characteristicsUsingTextForm": [{"name": "Orientation", "value": "landscape"}]}}}]}}}, "attachmentsInfo": {"attachmentInfo": [{"fileName": "Specification", "url": "https://zakupki.gov.ru/spec"}]}}}


class TableEnricher:
    def __init__(self):
        self.diagnostics = {"outcome": "success", "triage": {"selected_urls": ["https://zakupki.gov.ru/spec"]}, "extraction": {"accepted_count": 1, "rejected_count": 0}}
        self.cost_rub = 0
    def extract(self, **kwargs):
        return [ExtractedItem("Package", Decimal("500"), "796", {"Height": "32–35 cm", "Width": "40–45 cm", "Bottom": "11–14 cm", "Paper density": "200–250 g/m²", "Orientation": "landscape"}, {"document_url": "https://zakupki.gov.ru/spec", "page_or_section": "Table 1", "extraction_version": "test"}, Decimal("0.95"))]


class FailedEnricher:
    def __init__(self):
        self.diagnostics = {"outcome": "system_extraction_failure", "triage": {"selected_urls": ["https://zakupki.gov.ru/spec"]}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
        self.cost_rub = 0
    def extract(self, **kwargs):
        return []


class NoDataEnricher:
    def __init__(self):
        self.diagnostics = {"outcome": "no_data", "triage": {"selected_urls": []}, "extraction": {"accepted_count": 0, "rejected_count": 0}}
        self.cost_rub = 0
    def extract(self, **kwargs):
        return []


class BudgetBlockedEnricher:
    def __init__(self):
        self.diagnostics = {"outcome": "unpriced_model", "budget": {"average_target_rub": "1.0"}}
        self.cost_rub = 0

    def extract_tender(self, **kwargs):
        return []


class DocumentEnrichmentRegressionTests(TestCase):
    def setUp(self):
        settings = FilterSettings.load()
        settings.min_price = 0
        settings.include_words = ""
        settings.exclude_words = ""
        settings.save()

    def tender(self):
        return Tender.objects.create(purchase_number=f"enrich-{Tender.objects.count()+1}", title="Supply", notification_raw=_payload())

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_table_shape_is_preserved_and_existing_item_is_enriched_with_provenance(self):
        rows = document_table_payload("<table><tr><th rowspan='2'>Product</th><th>Height</th></tr><tr><td>32–35 cm</td></tr></table>")[0]["rows"]
        self.assertEqual(rows[1][0], "Product")
        tender = self.tender()
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=TableEnricher())
        self.assertNotEqual(job.status, TenderComputeJob.Status.PARTIAL)
        enriched = TenderSourceItem.objects.get(tender=tender, source_type="document_enrichment")
        self.assertEqual(enriched.requirements["document_requirements"]["Height"], "32–35 cm")
        self.assertEqual(enriched.metadata["field_provenance"]["Paper density"]["source"]["document_url"], "https://zakupki.gov.ru/spec")
        self.assertEqual(job.diagnostics["enrichment_state"], "success")

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_technical_extraction_failure_never_creates_owner_question(self):
        tender = self.tender()
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=FailedEnricher())
        self.assertEqual(job.status, TenderComputeJob.Status.PARTIAL)
        self.assertEqual(job.diagnostics["enrichment_state"], "system_extraction_failure")
        self.assertFalse(OwnerInteraction.objects.filter(tender=tender).exists())

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_true_no_data_can_reach_router_and_create_question(self):
        tender = self.tender()
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=NoDataEnricher())
        self.assertEqual(job.status, TenderComputeJob.Status.NEEDS_REVIEW)
        self.assertTrue(OwnerInteraction.objects.filter(tender=tender).exists())

    @override_settings(CALCULATION_V2_ENABLED=True)
    def test_unpriced_document_model_does_not_continue_to_routing(self):
        tender = self.tender()
        trigger_visible_tender(tender)
        job = run_next_tender_understanding_job(router=ExistingKnowledgeBatchRouter(), enricher=BudgetBlockedEnricher())
        self.assertEqual(job.status, TenderComputeJob.Status.PARTIAL)
        self.assertEqual(job.diagnostics["enrichment_state"], "unpriced_model")


class GatewayDocumentEnricherBudgetTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="gateway-enrichment", title="Gift sets")
        self.source = TenderSourceItem.objects.create(
            tender=self.tender, source_key="notification:1", source_type="notification",
            original_text="Gift set", quantity=Decimal("2"), unit="set",
        )
        DocumentPreview.objects.create(
            url="https://example.test/unusual-attachment", filename="anything.bin",
            html="<table><tr><th>Item</th></tr><tr><td>T-shirt</td></tr></table>",
        )
        DocumentPreview.objects.create(
            url="https://example.test/second", filename="miscellaneous", html="<p>One mug in each set.</p>",
        )
        self.documents = [
            {"url": "https://example.test/unusual-attachment", "name": "anything.bin"},
            {"url": "https://example.test/second", "name": "miscellaneous"},
        ]

    @patch("tenders.services._ai_gateway_json")
    def test_uses_one_selected_assistant_call_and_considers_every_document(self, gateway):
        gateway.return_value = ({"items": [{
            "parent_source_item_id": self.source.pk, "name": "T-shirt", "quantity": None,
            "unit": "piece", "requirements": {}, "confidence": 0.95,
            "source_url": "https://example.test/unusual-attachment", "page_or_section": "table",
            "evidence": "T-shirt", "supply_kind": "kit", "structure_reason": "Distinct goods in a kit", "relationship": "component", "quantity_per_parent": 1,
        }]}, {"prompt_tokens": 100, "completion_tokens": 100})

        enricher = GatewayDocumentEnricher()
        result = enricher.extract_tender(tender=self.tender, source_items=[self.source], documents=self.documents)

        self.assertEqual(enricher.model, "anthropic/claude-haiku-5-5")
        self.assertEqual(len(result), 1)
        self.assertEqual(gateway.call_count, 1)
        prompt = gateway.call_args.args[0]
        self.assertIn("unusual-attachment", prompt)
        self.assertIn("https://example.test/second", prompt)
        self.assertEqual(gateway.call_args.kwargs["max_tokens"], 16000)
        self.assertEqual(gateway.call_args.kwargs["network_attempts"], 1)
        self.assertEqual(gateway.call_args.kwargs["json_attempts"], 1)
        self.assertEqual(enricher.diagnostics["documents"]["considered"].__len__(), 2)

    @patch("tenders.services._ai_gateway_json")
    def test_unpriced_model_is_blocked_before_any_gateway_call(self, gateway):
        enricher = GatewayDocumentEnricher(model="unpriced/model")

        result = enricher.extract_tender(tender=self.tender, source_items=[self.source], documents=self.documents)

        self.assertEqual(result, [])
        self.assertEqual(enricher.diagnostics["outcome"], "unpriced_model")
        gateway.assert_not_called()
