import io
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from tender_selection.models import Tender
from .models import TenderComputeJob, TenderSourceItem


@override_settings(CALCULATION_V2_ENABLED=False)
class DocumentPilotTests(TestCase):
    def setUp(self):
        self.tender = Tender.objects.create(purchase_number="1234567890123456789", notification_raw={"source": {}})

    def test_dry_run_does_not_create_jobs_or_call_model(self):
        with patch("tenders.services._ai_gateway_json") as gateway:
            call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("3"), stdout=io.StringIO())
        self.assertFalse(TenderComputeJob.objects.exists())
        gateway.assert_not_called()

    def test_apply_is_single_tender_once_without_routes_or_global_flag(self):
        proposal = TenderSourceItem.objects.create(tender=self.tender, source_key="proposal:mock", source_type="document_proposal", is_active=False)
        with patch("tenders.management.commands.prepare_tender_documents.ingest_source_items", return_value=[]), \
                patch("tenders.management.commands.prepare_tender_documents.assess_quality", return_value={}), \
                patch("tenders.management.commands.prepare_tender_documents.enrich_suspicious_tender", return_value={"created": [proposal], "state": "needs_review", "diagnostics": []}) as enrich:
            for _ in range(2):
                call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("3"), max_output_tokens=6000, apply=True, stdout=io.StringIO())
        self.assertEqual(enrich.call_count, 1)
        self.assertEqual(TenderComputeJob.objects.count(), 1)
        self.assertEqual(TenderComputeJob.objects.get().status, "needs_review")
        from django.conf import settings
        self.assertFalse(settings.CALCULATION_V2_ENABLED)

    def test_budget_above_permission_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("15"), apply=True)
        self.assertFalse(TenderComputeJob.objects.exists())

    @patch("tenders.services._ai_gateway_json")
    def test_preflight_reads_full_input_and_remaining_budget_without_mutation(self, gateway):
        from tender_selection.models import DocumentPreview
        from .models import TenderSourceItem
        self.tender.notification_raw = {"source": {"attachmentsInfo": {"attachmentInfo": [{"url": "https://zakupki.gov.ru/full-source", "fileName": "spec.docx"}]}}}
        self.tender.save(update_fields=["notification_raw"])
        DocumentPreview.objects.create(url="https://zakupki.gov.ru/full-source", kind="docx", html="<p>" + "Полный текст. " * 2000 + "КОНЕЦ</p>")
        TenderSourceItem.objects.create(tender=self.tender, source_key="n:1", original_text="Комплект")
        job = TenderComputeJob.objects.create(tender=self.tender, version="v2-document-pilot", status="needs_review", total_cost=Decimal("0.414"))
        output = io.StringIO()
        call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("3"), max_output_tokens=6000, preflight=True, stdout=output)
        import json
        result = json.loads(output.getvalue())
        self.assertEqual(Decimal(result["remaining_rub"]), Decimal("2.586"))
        self.assertEqual(result["sources"][0]["cached_text_chars"], result["sources"][0]["sent_text_chars"])
        self.assertGreater(result["sources"][0]["sent_text_chars"], 18000)
        self.assertEqual(TenderComputeJob.objects.count(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "needs_review")
        gateway.assert_not_called()

    def test_full_repeat_deducts_previous_spend_and_archives_only_old_pilot_drafts(self):
        from django.utils import timezone
        original = TenderSourceItem.objects.create(tender=self.tender, source_key="notification:1", original_text="Комплект", metadata={"notification_index": 0})
        old_job = TenderComputeJob.objects.create(tender=self.tender, version="v2-document-pilot", status="needs_review", total_cost=Decimal("0.414"))
        old = TenderSourceItem.objects.create(tender=self.tender, parent=original, source_key="proposal:old", source_type="document_proposal", original_text="Старая подсказка", is_active=False)
        old_job.completed_at = timezone.now()
        old_job.save(update_fields=["completed_at"])
        def enrich(tender, quality, enricher):
            self.assertEqual(enricher.max_cost_rub, Decimal("13.586"))
            new = TenderSourceItem.objects.create(tender=tender, parent=original, source_key="proposal:new", source_type="document_proposal", original_text="Товар", is_active=False)
            return {"created": [new], "state": "needs_review", "diagnostics": []}
        with patch("tenders.management.commands.prepare_tender_documents.ingest_source_items", return_value=[original]), \
                patch("tenders.management.commands.prepare_tender_documents.assess_quality", return_value={}), \
                patch("tenders.management.commands.prepare_tender_documents.enrich_suspicious_tender", side_effect=enrich):
            call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("14"), apply=True, stdout=io.StringIO())
        latest = TenderComputeJob.objects.order_by("-pk").first()
        self.assertEqual(latest.diagnostics["superseded_pending_proposals"][0]["id"], old.pk)
        self.assertFalse(TenderSourceItem.objects.filter(pk=old.pk).exists())
        original.refresh_from_db()
        self.assertTrue(original.is_active)
        self.assertEqual(original.source_type, "notification")
        self.assertTrue(TenderSourceItem.objects.filter(source_key="proposal:new", is_active=False).exists())

    def test_spent_budget_blocks_another_pilot(self):
        TenderComputeJob.objects.create(tender=self.tender, version="v2-document-pilot", status="partial", total_cost=Decimal("14"))
        with self.assertRaises(CommandError):
            call_command("prepare_tender_documents", tender_number=self.tender.purchase_number, max_cost_rub=Decimal("14"), apply=True, stdout=io.StringIO())
        self.assertEqual(TenderComputeJob.objects.count(), 1)
