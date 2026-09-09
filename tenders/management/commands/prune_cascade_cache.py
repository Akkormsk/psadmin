from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tenders.models import CascadeCache


class Command(BaseCommand):
    help = "Удалить записи кэша каскада подбора старше N дней (по умолчанию 30)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=max(1, options["days"]))
        deleted, _ = CascadeCache.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(self.style.SUCCESS(f"Удалено записей кэша каскада: {deleted}"))
