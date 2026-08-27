from django.core.management.base import BaseCommand, CommandError

from tenders.models import ProductionTrainingExample
from tenders.services import _embedding_model, _embeddings_enabled, refresh_training_example_embedding


class Command(BaseCommand):
    help = "Обновляет смысловой индекс подтверждённых примеров"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **options):
        if not _embeddings_enabled():
            raise CommandError("Установите TIMEWEB_EMBEDDINGS_ENABLED=1.")
        examples = list(ProductionTrainingExample.objects.filter(is_active=True).order_by("pk"))
        if not options["force"]:
            examples = [value for value in examples if value.embedding_model != _embedding_model() or not value.embedding]
        if options["limit"]:
            examples = examples[:max(0, options["limit"])]
        updated = sum(1 for example in examples if refresh_training_example_embedding(example))
        self.stdout.write(self.style.SUCCESS(f"Обновлено смысловых индексов: {updated}."))
