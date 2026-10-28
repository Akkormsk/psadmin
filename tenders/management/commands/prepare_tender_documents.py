"""Explicit, single-tender operator pilot using the existing intake executors."""
import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tender_selection.models import Tender
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality, enrich_suspicious_tender, ingest_source_items
from tenders.models import TenderComputeJob


class Command(BaseCommand):
    help = "Prepare document proposals for exactly one tender; never run routes or the worker queue."

    def add_arguments(self, parser):
        parser.add_argument("--tender-number", required=True)
        parser.add_argument("--max-cost-rub", required=True, type=Decimal)
        parser.add_argument("--max-output-tokens", type=int, default=1200)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        budget = options["max_cost_rub"]
        if not budget.is_finite() or not Decimal("0") < budget <= Decimal("3"):
            raise CommandError("Pilot budget must be positive and no greater than 3 RUB")
        if not 256 <= options["max_output_tokens"] <= 6000:
            raise CommandError("Output limit must be between 256 and 6000 tokens")
        tender = Tender.objects.filter(purchase_number=options["tender_number"]).first()
        if tender is None:
            raise CommandError("Tender not found; nothing was called")
        if not options["apply"]:
            self.stdout.write(json.dumps({"apply": False, "budget_rub": str(budget), "has_notification": bool(tender.notification_raw)}))
            return
        version = "v2-document-pilot"
        with transaction.atomic():
            Tender.objects.select_for_update().get(pk=tender.pk)
            previous = TenderComputeJob.objects.filter(tender=tender, version=version).first()
            if previous:
                self.stdout.write(json.dumps({"reused": True, "status": previous.status, "cost_rub": str(previous.total_cost)}))
                return
            job = TenderComputeJob.objects.create(tender=tender, version=version, trigger="operator_document_pilot", status=TenderComputeJob.Status.PREPARING_INPUT)
        enricher = GatewayDocumentEnricher(max_cost_rub=budget)
        enricher.DOCUMENT_OUTPUT_TOKEN_LIMIT = options["max_output_tokens"]
        try:
            source_items = ingest_source_items(tender)
            quality = assess_quality(tender, source_items)
            result = enrich_suspicious_tender(tender, quality, enricher)
            state = result["state"]
            job.status = TenderComputeJob.Status.NEEDS_REVIEW if result["created"] else TenderComputeJob.Status.READY if state == "no_data" else TenderComputeJob.Status.PARTIAL
            job.diagnostics = {"source_item_count": len(source_items), "document_proposal_count": len(result["created"]),
                               "enrichment_state": state, "enrichment": result["diagnostics"], "documents_only": True}
        except Exception as exc:
            job.status = TenderComputeJob.Status.PARTIAL
            job.error = str(exc)[:1000]
            job.diagnostics = {"enrichment": [enricher.diagnostics], "documents_only": True, "exception_type": type(exc).__name__}
        job.total_cost = Decimal(str(enricher.cost_rub or 0))
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "diagnostics", "error", "total_cost", "completed_at", "updated_at"])
        considered = (enricher.diagnostics.get("documents") or {}).get("considered", [])
        self.stdout.write(json.dumps({"status": job.status, "proposal_count": job.diagnostics.get("document_proposal_count", 0),
                                     "reading_statuses": [doc.get("reading_status") for doc in considered],
                                     "outcome": enricher.diagnostics.get("outcome"), "model": enricher.model,
                                     "cost_rub": str(job.total_cost), "usage": enricher.usage,
                                     "exception_type": job.diagnostics.get("exception_type")}, default=str))
