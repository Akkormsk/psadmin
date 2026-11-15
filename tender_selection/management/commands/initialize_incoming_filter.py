import json
from django.core.management.base import BaseCommand
from tender_selection.intake import initialize_existing


class Command(BaseCommand):
    help = 'Зафиксировать старые карточки без переотбора; без сети и ИИ.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        self.stdout.write(json.dumps({'applied': options['apply'], **initialize_existing(apply=options['apply'])}))
