from django.core.management.base import BaseCommand

from tenders.catalog import rebuild_catalog_families


class Command(BaseCommand):
    help = "Перестроить нормализованные семейства и оси вариантов локального каталога"

    def add_arguments(self, parser):
        parser.add_argument("--supplier", choices=("oasis", "gifts"))

    def handle(self, *args, **options):
        result = rebuild_catalog_families(options.get("supplier"))
        self.stdout.write(self.style.SUCCESS(
            f"Товаров: {result['products']}; обновлено: {result['updated']}; семейств: {result['families']}"
        ))
