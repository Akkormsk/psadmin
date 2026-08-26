from django.core.management.base import BaseCommand, CommandError

from tenders.catalog import CatalogSyncError, sync_oasis_catalog


class Command(BaseCommand):
    help = "Синхронизирует компактный поисковый каталог Oasis"

    def handle(self, *args, **options):
        try:
            run = sync_oasis_catalog()
        except CatalogSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Oasis: получено {run.received_count}, создано {run.created_count}, "
            f"обновлено {run.updated_count}, отключено {run.deactivated_count}."
        ))
