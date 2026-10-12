from django.core.management.base import BaseCommand

from tenders.agent_gaps import export_gaps


class Command(BaseCommand):
    help = "Export AgentGap database records to the single agent_gaps.jsonl artifact."

    def handle(self, *args, **options):
        self.stdout.write(str(export_gaps()))
