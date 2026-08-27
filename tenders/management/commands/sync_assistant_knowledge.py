import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.knowledge import import_knowledge_bundle


class Command(BaseCommand):
    help = "Скачивает серверную базу знаний вместе с embeddings и импортирует её локально"

    def add_arguments(self, parser):
        parser.add_argument("--url", default=os.getenv("KNOWLEDGE_SYNC_URL", "https://admin.psodin.ru/tenders/knowledge/sync/"))
        parser.add_argument("--token", default=os.getenv("KNOWLEDGE_SYNC_TOKEN") or self._local_token())
        parser.add_argument("--user")
        parser.add_argument("--path", default="tenders/assistant_knowledge.json")

    def handle(self, *args, **options):
        if not options["url"] or not options["token"]:
            raise CommandError("Нужны KNOWLEDGE_SYNC_URL и KNOWLEDGE_SYNC_TOKEN.")
        request = urllib.request.Request(options["url"], headers={"Authorization": f"Bearer {options['token']}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                bundle = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CommandError(f"Не удалось получить базу знаний: {exc}") from exc

        try:
            if options["user"]:
                user = get_user_model().objects.get(username=options["user"])
            else:
                superusers = list(get_user_model().objects.filter(is_superuser=True)[:2])
                if len(superusers) != 1:
                    raise ValueError("Укажите --user: в базе должен быть ровно один администратор.")
                user = superusers[0]
            result = import_knowledge_bundle(bundle, user)
            path = Path(options["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
                json.dump(bundle, temp, ensure_ascii=False, indent=2)
                temp_path = Path(temp.name)
            temp_path.replace(path)
        except (OSError, ValueError, get_user_model().DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Синхронизировано примеров: {result['examples']}; источников: {result['sources']}."
        ))

    @staticmethod
    def _local_token():
        try:
            return Path(".knowledge_sync_token").read_text(encoding="utf-8").strip()
        except OSError:
            return None
