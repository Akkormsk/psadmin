from django.core.management.base import BaseCommand, CommandError

from tenders.catalog import CatalogSyncError, sync_gifts_categories


class Command(BaseCommand):
    help = "Обновляет только карту категорий gifts.ru из treeWithoutProducts.xml"

    def handle(self, *args, **options):
        try:
            categories = sync_gifts_categories()
        except CatalogSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Категорий gifts.ru сохранено: {len(categories)}"))
