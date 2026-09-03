import os
import urllib.error
import urllib.request
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Скачивает каталог поставщиков (Oasis + Gifts) с production и заменяет локальные таблицы каталога"

    def add_arguments(self, parser):
        parser.add_argument("--url", default=os.getenv("CATALOG_SYNC_URL", "https://admin.psodin.ru/tenders/catalog/sync/"))
        parser.add_argument("--token", default=os.getenv("KNOWLEDGE_SYNC_TOKEN") or self._local_token())
        parser.add_argument("--path", default="psadmin-local-export-catalog.json")

    def handle(self, *args, **options):
        if not options["url"] or not options["token"]:
            raise CommandError("Нужны CATALOG_SYNC_URL и токен (KNOWLEDGE_SYNC_TOKEN или файл .knowledge_sync_token).")
        request = urllib.request.Request(options["url"], headers={"Authorization": f"Bearer {options['token']}"})
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                payload = response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise CommandError(f"Не удалось получить каталог: {exc}") from exc

        path = Path(options["path"])
        path.write_bytes(payload)

        from tenders.models import CatalogCategory, CatalogProduct, CatalogSupplier

        CatalogSupplier.objects.all().delete()
        call_command("loaddata", str(path), verbosity=0)

        self.stdout.write(self.style.SUCCESS(
            f"Загружено: поставщиков {CatalogSupplier.objects.count()}, "
            f"категорий {CatalogCategory.objects.count()}, товаров {CatalogProduct.objects.count()}."
        ))

    @staticmethod
    def _local_token():
        try:
            return Path(".knowledge_sync_token").read_text(encoding="utf-8").strip()
        except OSError:
            return None
