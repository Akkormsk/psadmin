import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from tender_selection.discovery_feed import import_feed


class Command(BaseCommand):
    help = "Перенести исходные карточки из общего потока в локальную TEST-базу"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--interval", type=float, default=float(os.getenv("TENDER_MIRROR_INTERVAL_SECONDS", "30")))

    def handle(self, *args, **options):
        if os.getenv("TENDER_DISCOVERY_ROLE") != "mirror":
            raise CommandError("Импорт общего потока разрешён только в режиме mirror")
        if options["interval"] <= 0:
            raise CommandError("Интервал должен быть положительным")
        while True:
            close_old_connections()
            run = import_feed()
            self.stdout.write(f"shared discovery: new={run.created_count}, updated={run.updated_count}, ok={run.ok}")
            if not options["watch"]:
                if not run.ok:
                    raise CommandError(run.error)
                return
            time.sleep(options["interval"])
