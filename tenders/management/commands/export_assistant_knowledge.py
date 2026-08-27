import json
from pathlib import Path

from django.core.management.base import BaseCommand

from tenders.knowledge import export_knowledge_bundle


class Command(BaseCommand):
    help = "Экспортирует переносимую подтверждённую базу знаний ассистента"

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default="tenders/assistant_knowledge.json")

    def handle(self, *args, **options):
        path = Path(options["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        bundle = export_knowledge_bundle()
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Экспортировано примеров: {len(bundle['training_examples'])}; источников: {len(bundle['knowledge_sources'])}."
        ))
