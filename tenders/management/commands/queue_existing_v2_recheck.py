from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.calculation_v2_pipeline import eligible_existing_recheck_tenders, queue_existing_recheck


class Command(BaseCommand):
    help = "Queue an explicitly enabled, bounded V2 recheck of current Incoming and Kanban tenders."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1")
        if not settings.CALCULATION_V2_EXISTING_RECHECK_ENABLED:
            raise CommandError("CALCULATION_V2_EXISTING_RECHECK_ENABLED is false; nothing was queued.")
        candidates = eligible_existing_recheck_tenders(limit=options["limit"])
        if not options["apply"]:
            self.stdout.write(f"Dry run: {len(candidates)} current Incoming/Kanban tender(s)")
            return
        self.stdout.write(f"Queued {len(queue_existing_recheck(limit=options['limit']))} V2 recheck job(s)")
