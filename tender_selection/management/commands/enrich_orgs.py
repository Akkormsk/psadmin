from django.core.management.base import BaseCommand

from tender_selection.services import enrich_organizations


class Command(BaseCommand):
    help = "Подтянуть названия и города заказчиков по ИНН (по одному запросу на ИНН, с паузами)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20, help="Сколько новых ИНН обработать за раз (деф. 20)")

    def handle(self, *args, **options):
        saved = enrich_organizations(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(f"Обновлено карточек заказчиков: {saved}"))
