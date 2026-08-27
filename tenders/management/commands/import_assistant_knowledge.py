import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.knowledge import import_knowledge_bundle


class Command(BaseCommand):
    help = "Добавляет переносимую базу знаний ассистента без удаления локальных записей"

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default="tenders/assistant_knowledge.json")
        parser.add_argument("--user", help="Пользователь-владелец импортированных записей")

    def handle(self, *args, **options):
        try:
            bundle = json.loads(Path(options["path"]).read_text(encoding="utf-8"))
            if options["user"]:
                user = get_user_model().objects.get(username=options["user"])
            else:
                superusers = list(get_user_model().objects.filter(is_superuser=True)[:2])
                if len(superusers) != 1:
                    raise ValueError("Укажите --user: в базе должен быть ровно один администратор.")
                user = superusers[0]
            result = import_knowledge_bundle(bundle, user)
        except (OSError, ValueError, json.JSONDecodeError, get_user_model().DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Импортировано примеров: {result['examples']} (новых {result['created_examples']}); "
            f"источников: {result['sources']} (новых {result['created_sources']})."
        ))
