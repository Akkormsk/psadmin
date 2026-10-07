from django.core.management.base import BaseCommand, CommandError

from tenders.calculation_v2_pipeline import eligible_backfill_tenders, queue_visible_backfill


class Command(BaseCommand):
    help = "Queue a bounded backfill of unprocessed eligible incoming tenders for Calculation Engine V2."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--apply", action="store_true", help="Create jobs; without it this command is read-only.")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be at least 1")
        candidates = eligible_backfill_tenders(limit=limit)
        if not options["apply"]:
            self.stdout.write(f"Dry run: {len(candidates)} eligible unprocessed tender(s)")
            for tender in candidates:
                self.stdout.write(f"{tender.pk}: {tender.purchase_number} — {tender.title}")
            return
        jobs = queue_visible_backfill(limit=limit)
        self.stdout.write(f"Queued {len(jobs)} Calculation V2 job(s)")
        for job in jobs:
            self.stdout.write(f"{job.pk}: tender {job.tender_id}")
