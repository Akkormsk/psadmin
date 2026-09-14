from django.core.management.base import BaseCommand, CommandError

from tenders.catalog import rebuild_catalog_embeddings
from tenders.services import TenderAIError


class Command(BaseCommand):
    help = "Построить/обновить смысловой индекс каталога (для гибридного поиска шага 3 в лаборатории)"

    def add_arguments(self, parser):
        parser.add_argument("--supplier", choices=("oasis", "gifts"))
        parser.add_argument("--limit", type=int, default=None, help="Сколько товаров обработать за этот запуск")

    def handle(self, *args, **options):
        try:
            result = rebuild_catalog_embeddings(options.get("supplier"), limit=options.get("limit"))
        except TenderAIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Всего: {result['total']}; пропущено (без изменений): {result['skipped']}; "
            f"проиндексировано: {result['embedded']}; осталось: {result['remaining']}"
        ))
