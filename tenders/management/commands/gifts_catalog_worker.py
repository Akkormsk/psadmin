import os
import time

from django.core.management.base import BaseCommand

from tenders.catalog import CatalogSyncError, sync_gifts_catalog


class Command(BaseCommand):
    help = "Постоянный worker для периодической синхронизации gifts.ru"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=int(os.getenv("GIFTS_SYNC_INTERVAL_SECONDS", "86400")))

    def handle(self, *args, **options):
        while True:
            try:
                run = sync_gifts_catalog()
                self.stdout.write(self.style.SUCCESS(
                    f"gifts.ru: получено {run.received_count}; новых {run.created_count}; обновлено {run.updated_count}."
                ), flush=True)
            except CatalogSyncError as exc:
                self.stderr.write(self.style.ERROR(f"gifts.ru: {exc}"), flush=True)
            if options["once"]:
                return
            time.sleep(max(60, options["interval"]))
