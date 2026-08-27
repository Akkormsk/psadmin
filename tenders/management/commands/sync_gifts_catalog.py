import os

from django.core.management.base import BaseCommand, CommandError

from tenders.catalog import CatalogSyncError, sync_gifts_catalog


class Command(BaseCommand):
    help = "Импортирует каталог gifts.ru в компактную локальную таблицу товаров"

    def add_arguments(self, parser):
        parser.add_argument("--category", default=os.getenv("GIFTS_XML_CATEGORY", ""))

    def handle(self, *args, **options):
        try:
            run = sync_gifts_catalog(category=options["category"] or None)
        except CatalogSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"gifts.ru: получено {run.received_count}; новых {run.created_count}; обновлено {run.updated_count}."
        ))
