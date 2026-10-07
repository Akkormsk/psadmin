from django.test import TestCase
from tender_selection.models import Tender
from .models import OwnerInteraction, TenderComputeJob, TenderSourceItem
from .analysis_presentation import resolve_tender_analysis_status


class TenderAnalysisPresentationTests(TestCase):
    def tender(self):
        return Tender.objects.create(purchase_number=f"status-{Tender.objects.count()+1}", title="Test")

    def job(self, tender, status, diagnostics=None):
        return TenderComputeJob.objects.create(tender=tender, version="v2", status=status, diagnostics=diagnostics or {})

    def test_no_job_is_not_checked(self):
        result = resolve_tender_analysis_status(self.tender())
        self.assertEqual(result.code, "NOT_CHECKED")
        self.assertEqual(result.label, "Не проверено")

    def test_active_job_is_processing(self):
        result = resolve_tender_analysis_status(self.tender())
        self.assertEqual(result.code, "NOT_CHECKED")
        self.job(result.tender, TenderComputeJob.Status.QUEUED)
        self.assertEqual(resolve_tender_analysis_status(result.tender).code, "PROCESSING")

    def test_successful_unchanged_job_is_checked(self):
        tender = self.tender()
        self.job(tender, TenderComputeJob.Status.READY, {"enrichment_state": "no_data", "enrichment_used": False})
        self.assertEqual(resolve_tender_analysis_status(tender).code, "CHECKED_NO_CHANGES")

    def test_split_and_enrichment_are_enriched(self):
        split_tender = self.tender()
        TenderSourceItem.objects.create(tender=split_tender, source_key="doc:split", source_type="document_extraction", original_text="Part")
        self.job(split_tender, TenderComputeJob.Status.READY, {"enrichment_state": "success"})
        self.assertEqual(resolve_tender_analysis_status(split_tender).code, "ENRICHED")
        enriched_tender = self.tender()
        parent = TenderSourceItem.objects.create(tender=enriched_tender, source_key="source", source_type="notification", original_text="Item", requirements={"characteristics": []}, is_active=False)
        TenderSourceItem.objects.create(tender=enriched_tender, source_key="doc:enriched", source_type="document_enrichment", original_text="Item", parent=parent, requirements={"document_requirements": {"Material": "paper"}})
        self.job(enriched_tender, TenderComputeJob.Status.READY, {"enrichment_state": "success"})
        self.assertEqual(resolve_tender_analysis_status(enriched_tender).code, "ENRICHED")

    def test_historical_document_row_without_new_fact_is_checked_not_enriched(self):
        tender = self.tender()
        parent = TenderSourceItem.objects.create(tender=tender, source_key="source", source_type="notification", original_text="Item", requirements={"characteristics": [{"name": "Material", "value": "paper"}]}, is_active=False)
        TenderSourceItem.objects.create(tender=tender, source_key="doc:unchanged", source_type="document_enrichment", original_text="Item", parent=parent, requirements={"document_requirements": {"Material": "paper"}})
        self.job(tender, TenderComputeJob.Status.READY, {"enrichment_state": "success", "enrichment_used": True})
        self.assertEqual(resolve_tender_analysis_status(tender).code, "CHECKED_NO_CHANGES")

    def test_composite_is_enriched_for_presentation(self):
        from .models import TenderCommercialItem
        tender = self.tender()
        TenderCommercialItem.objects.create(tender=tender, source_key="composite", display_name="Commercial", structure="composite")
        self.job(tender, TenderComputeJob.Status.READY, {"enrichment_state": "success"})
        self.assertEqual(resolve_tender_analysis_status(tender).code, "ENRICHED")

    def test_valid_open_questions_win_over_enriched_and_count(self):
        tender = self.tender()
        job = self.job(tender, TenderComputeJob.Status.NEEDS_REVIEW, {"enrichment_state": "success", "enrichment_used": True})
        OwnerInteraction.objects.create(tender=tender, question="First")
        OwnerInteraction.objects.create(tender=tender, question="Second")
        result = resolve_tender_analysis_status(tender)
        self.assertEqual(result.code, "NEEDS_OWNER_INPUT")
        self.assertEqual(result.question_count, 2)

    def test_incomplete_wins_over_stale_question(self):
        tender = self.tender()
        self.job(tender, TenderComputeJob.Status.PARTIAL, {"enrichment_state": "system_extraction_failure"})
        OwnerInteraction.objects.create(tender=tender, question="Stale")
        self.assertEqual(resolve_tender_analysis_status(tender).code, "ANALYSIS_INCOMPLETE")

    def test_validation_failure_is_incomplete(self):
        tender = self.tender()
        self.job(tender, TenderComputeJob.Status.PARTIAL, {"enrichment_state": "validation_failure"})
        self.assertEqual(resolve_tender_analysis_status(tender).code, "ANALYSIS_INCOMPLETE")

    def test_result_is_presentation_ready_not_raw_job(self):
        tender = self.tender()
        self.job(tender, TenderComputeJob.Status.RUNNING)
        result = resolve_tender_analysis_status(tender)
        self.assertEqual(set(result.__dict__), {"tender", "code", "label", "kind", "question_count"})
        self.assertNotIn("running", result.__dict__.values())
