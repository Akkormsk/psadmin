import json
import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    def handle(self, *args, **options):
        self.stdout.write(json.dumps({
            name: "present" if os.getenv(name) else "missing"
            for name in ("TIMEWEB_AI_API_KEY", "TIMEWEB_AI_BASE_URL", "TIMEWEB_AI_ASSISTANT_MODEL")
        }))
