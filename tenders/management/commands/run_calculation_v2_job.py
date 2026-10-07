from django.core.management.base import BaseCommand

from tenders.calculation_v2_pipeline import run_next_tender_understanding_job


class Command(BaseCommand):
    help = "Claim and run one durable Calculation Engine V2 understanding job."

    def handle(self, *args, **options):
        job = run_next_tender_understanding_job()
        if job is None:
            self.stdout.write("No queued Calculation V2 job")
            return
        self.stdout.write(f"Completed Calculation V2 job {job.pk}: {job.status}")