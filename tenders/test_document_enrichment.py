
from decimal import Decimal
from django.test import TestCase, override_settings
from tender_selection.models import DocumentPreview, FilterSettings, Tender
from .calculation_v2_pipeline import ExtractedItem, ExistingKnowledgeBatchRouter, run_next_tender_understanding_job, trigger_visible_tender
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
