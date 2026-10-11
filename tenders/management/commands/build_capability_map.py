from django.core.management.base import BaseCommand

from tenders.capability_map import write_capability_map


class Command(BaseCommand):
    help = "Regenerate capability_map.json from installed Django metadata and registered operations."

    def handle(self, *args, **options):
        self.stdout.write(str(write_capability_map()))
