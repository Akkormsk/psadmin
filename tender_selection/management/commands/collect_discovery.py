import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from tender_selection.services import run_pull, run_keyword_pull


class Command(BaseCommand):
    help = "Единственный фоновый сбор первичных карточек с доставкой общего потока"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--interval", type=float, default=float(os.getenv("TENDER_DISCOVERY_INTERVAL_SECONDS", "1800")))

    def handle(self, *args, **options):
        if os.getenv("TENDER_DISCOVERY_ROLE") != "collector" or options["interval"] <= 0:
            raise CommandError("Требуются collector role и положительный интервал")
        while True:
            close_old_connections()
            try:
                for collect in (run_pull, run_keyword_pull):
                    run = collect()
                    self.stdout.write(f"discovery {collect.__name__}: new={run.created_count}, requests={run.requests_made}, ok={run.ok}")
            finally:
                close_old_connections()
            if not options["watch"]:
                return
            time.sleep(options["interval"])
