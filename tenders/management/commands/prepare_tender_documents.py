"""Explicit, single-tender operator pilot using the existing intake executors."""
import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.urls import reverse

from tender_selection.models import Tender
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, active_calculation_items, assess_quality, enrich_suspicious_tender, ingest_source_items
from tenders.models import TenderComputeJob, TenderSourceItem


class Command(BaseCommand):
    help = "Prepare document proposals for exactly one tender; never run routes or the worker queue."

    def add_arguments(self, parser):
        parser.add_argument("--tender-number", required=True)
        parser.add_argument("--max-cost-rub", required=True, type=Decimal)
        parser.add_argument("--max-output-tokens", type=int, default=8000)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--preflight", action="store_true")

    def handle(self, *args, **options):
        budget = options["max_cost_rub"]
        if not budget.is_finite() or not Decimal("0") < budget <= Decimal("14"):
            raise CommandError("Pilot budget must be positive and no greater than 14 RUB")
        if not 256 <= options["max_output_tokens"] <= 12000:
            raise CommandError("Output limit must be between 256 and 12000 tokens")
        tender = Tender.objects.filter(purchase_number=options["tender_number"]).first()
        if tender is None:
            raise CommandError("Tender not found; nothing was called")
        if options["preflight"]:
            if options["apply"]:
                raise CommandError("Preflight cannot be combined with apply")
            spent = TenderComputeJob.objects.filter(tender=tender, version__startswith="v2-document-pilot").aggregate(total=Sum("total_cost"))["total"] or Decimal("0")
            enricher = GatewayDocumentEnricher(max_cost_rub=0)
            enricher.DOCUMENT_OUTPUT_TOKEN_LIMIT = options["max_output_tokens"]
            quality = assess_quality(tender, active_calculation_items(tender))
            enricher.extract_tender(tender=tender, source_items=active_calculation_items(tender), documents=quality["documents"])
            reservation = (enricher.diagnostics.get("budget") or {}).get("reserved_cost_rub")
            self.stdout.write(json.dumps({"preflight_only": True, "model_called": False, "model": enricher.model,
                "spent_rub": str(spent), "remaining_rub": str(budget - spent), "required_reservation_rub": reservation,
                "fits_remaining_budget": reservation is not None and Decimal(str(reservation)) <= budget - spent,
                "sources": [{"name": row.get("name"), "reading_status": row.get("reading_status"),
                             "cached_text_chars": row.get("cached_text_chars"), "sent_text_chars": row.get("sent_text_chars"),
                             "has_error": bool(row.get("error")), "error": row.get("error")}
                            for row in (enricher.diagnostics.get("documents") or {}).get("considered", [])]}, ensure_ascii=False, default=str))
            return
        if not options["apply"]:
            self.stdout.write(json.dumps({"apply": False, "budget_rub": str(budget), "has_notification": bool(tender.notification_raw)}))
            return
        version = "v2-document-pilot-components"
        with transaction.atomic():
            Tender.objects.select_for_update().get(pk=tender.pk)
            previous = TenderComputeJob.objects.filter(tender=tender, version=version).first()
            if previous:
                details = previous.diagnostics.get("enrichment", [])
                sources = [source for detail in details if isinstance(detail, dict) for source in (detail.get("documents") or {}).get("considered", [])]
                proposals = TenderSourceItem.objects.filter(tender=tender, source_type="document_proposal", is_active=False).select_related("parent")
                self.stdout.write(json.dumps({"reused": True, "status": previous.status, "cost_rub": str(previous.total_cost), "card_path": reverse("tender_selection:detail", args=[tender.pk]),
                    "sources": [{"name": source.get("name"), "reading_status": source.get("reading_status"),
                                 "cached_text_chars": source.get("cached_text_chars"), "sent_text_chars": source.get("sent_text_chars"),
                                 "has_error": bool(source.get("error"))} for source in sources],
                    "pending_proposals": [{"name": item.original_text, "quantity": item.quantity, "unit": item.unit,
                                           "notification_position": (item.parent.metadata or {}).get("notification_index", -1) + 1 if item.parent else None,
                                           "relationship": item.metadata.get("relationship"), "quantity_per_parent": item.metadata.get("quantity_per_parent")}
                                          for item in proposals]}, ensure_ascii=False, default=str))
                return
            history = TenderComputeJob.objects.filter(tender=tender, version__startswith="v2-document-pilot")
            if history.filter(status__in=["preparing_input", "running", "queued"]).exists():
                raise CommandError("Another pilot is active; nothing was called")
            spent = history.aggregate(total=Sum("total_cost"))["total"] or Decimal("0")
            remaining = budget - spent
            if remaining <= 0:
                raise CommandError("Pilot budget is exhausted; nothing was called")
            old_proposals = set()
            for old_job in history.filter(completed_at__isnull=False):
                if any(detail.get("provider_call_happened") and not detail.get("usage")
                       for detail in old_job.diagnostics.get("enrichment", []) if isinstance(detail, dict)):
                    raise CommandError("Previous paid outcome is unknown; reconcile before another call")
                old_proposals.update(old_job.diagnostics.get("proposal_ids", []))
                if not old_job.diagnostics.get("proposal_ids"):
                    old_proposals.update(TenderSourceItem.objects.filter(
                        tender=tender, source_type="document_proposal", is_active=False,
                        created_at__gte=old_job.created_at, created_at__lte=old_job.completed_at,
                    ).values_list("pk", flat=True))
            job = TenderComputeJob.objects.create(tender=tender, version=version, trigger="operator_document_pilot", status=TenderComputeJob.Status.PREPARING_INPUT)
        enricher = GatewayDocumentEnricher(max_cost_rub=remaining)
        enricher.DOCUMENT_OUTPUT_TOKEN_LIMIT = options["max_output_tokens"]
        try:
            source_items = ingest_source_items(tender)
            quality = assess_quality(tender, source_items)
            result = enrich_suspicious_tender(tender, quality, enricher)
            state = result["state"]
            job.status = TenderComputeJob.Status.NEEDS_REVIEW if result["created"] else TenderComputeJob.Status.READY if state == "no_data" else TenderComputeJob.Status.PARTIAL
            job.diagnostics = {"source_item_count": len(source_items), "document_proposal_count": len(result["created"]),
                               "enrichment_state": state, "enrichment": result["diagnostics"], "documents_only": True,
                               "proposal_ids": [item.pk for item in result["created"]], "pilot_limit_rub": str(budget), "spent_before_rub": str(spent)}
        except Exception as exc:
            job.status = TenderComputeJob.Status.PARTIAL
            job.error = str(exc)[:1000]
            job.diagnostics = {"enrichment": [enricher.diagnostics], "documents_only": True, "exception_type": type(exc).__name__}
        job.total_cost = Decimal(str(enricher.cost_rub or 0))
        job.completed_at = timezone.now()
        with transaction.atomic():
            replacement_ids = job.diagnostics.get("proposal_ids", [])
            superseded = TenderSourceItem.objects.filter(pk__in=old_proposals - set(replacement_ids),
                                                         tender=tender, source_type="document_proposal", is_active=False)
            if replacement_ids:
                job.diagnostics["superseded_pending_proposals"] = json.loads(json.dumps(list(superseded.values()), default=str))
            job.save(update_fields=["status", "diagnostics", "error", "total_cost", "completed_at", "updated_at"])
            if replacement_ids:
                superseded.delete()
        considered = (enricher.diagnostics.get("documents") or {}).get("considered", [])
        self.stdout.write(json.dumps({"status": job.status, "proposal_count": job.diagnostics.get("document_proposal_count", 0),
                                     "reading_statuses": [doc.get("reading_status") for doc in considered],
                                     "outcome": enricher.diagnostics.get("outcome"), "model": enricher.model,
                                     "cost_rub": str(job.total_cost) if enricher.usage or not enricher.diagnostics.get("provider_call_happened") else None,
                                     "usage": enricher.usage,
                                     "exception_type": job.diagnostics.get("exception_type"),
                                     "cumulative_cost_rub": str(spent + job.total_cost) if enricher.usage or not enricher.diagnostics.get("provider_call_happened") else None,
                                     "pending_proposals": [{"name": item.original_text, "quantity": item.quantity, "unit": item.unit,
                                          "notification_position": (item.parent.metadata or {}).get("notification_index", -1) + 1 if item.parent else None,
                                          "relationship": item.metadata.get("relationship"), "quantity_per_parent": item.metadata.get("quantity_per_parent")}
                                         for item in TenderSourceItem.objects.filter(pk__in=job.diagnostics.get("proposal_ids", [])).select_related("parent")]}, ensure_ascii=False, default=str))
