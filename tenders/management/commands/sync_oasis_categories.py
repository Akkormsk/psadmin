from django.core.management.base import BaseCommand

from tenders.catalog import sync_oasis_categories


class Command(BaseCommand):
    help = "Обновляет только зафиксированную карту категорий Oasis без загрузки товаров и изображений"

    def handle(self, *args, **options):
        categories = sync_oasis_categories()
        self.stdout.write(self.style.SUCCESS(f"Категорий Oasis сохранено: {len(categories)}"))
