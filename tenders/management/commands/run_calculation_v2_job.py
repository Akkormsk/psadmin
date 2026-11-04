import os
import time
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from tenders.calculation_v2_pipeline import run_next_tender_understanding_job


class Command(BaseCommand):
    help = "Run existing durable intake jobs once, or consume them continuously within an explicit daily budget."

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--daily-budget-rub', type=Decimal, default=Decimal(os.getenv('CALCULATION_V2_AUTO_DAILY_BUDGET_RUB', '0')))
        parser.add_argument('--poll-seconds', type=int, default=10)

    def handle(self, *args, **options):
        if not options['watch']:
            job = run_next_tender_understanding_job()
            self.stdout.write(f'Completed intake job {job.pk}: {job.status}' if job else 'No queued Calculation V2 job')
            return
        budget = options['daily_budget_rub']
        if not budget.is_finite() or budget < 0 or not 1 <= options['poll_seconds'] <= 60:
            raise CommandError('Finite nonnegative daily budget and poll interval1..60 required')
        from tenders.intake_worker import run_budgeted_job
        import signal
        stop = False
        def stopping(*_args):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, stopping)
        signal.signal(signal.SIGINT, stopping)
        while not stop:
            close_old_connections()
            job = run_budgeted_job(budget) if settings.CALCULATION_V2_ENABLED and budget > 0 else None
            if job:
                self.stdout.write(f'Completed intake job {job.pk}: {job.status}')
            else:
                time.sleep(options['poll_seconds'])
