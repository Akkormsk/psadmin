import os
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from tender_selection.services import run_pull
from tender_selection.intake import expire_incoming


class Command(BaseCommand):
    help = 'Самостоятельный сбор ГосПлана; вторичный отсев при поступлении, без AI/ЕИС-подкачки.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--interval', type=float, default=float(os.getenv('TENDER_DISCOVERY_INTERVAL_SECONDS','300')))
        parser.add_argument('--max-seconds', type=float, default=None)

    def handle(self, *args, **options):
        if os.getenv('TENDER_DISCOVERY_ROLE') not in {'standalone','collector'} or options['interval'] <= 0:
            raise CommandError('Требуются самостоятельная роль сбора и положительный интервал')
        if options['max_seconds'] is not None and options['max_seconds'] <= 0:
            raise CommandError('Продолжительность должна быть положительной')
        os.environ['TENDER_DISCOVERY_ENRICH_AFTER_PULL'] = '0'
        while True:
            close_old_connections()
            try:
                expire_incoming()
                run = run_pull(max_seconds=options['max_seconds'])
                expire_incoming()
                if run.params.get('paused'):
                    self.stdout.write('discovery paused: no Gosplan requests')
                else:
                    self.stdout.write(f'discovery: new={run.created_count}, updated={run.updated_count}, pages={run.requests_made}, complete={run.params.get("complete",False)}, ok={run.ok}; {run.error}')
            finally:
                close_old_connections()
            if not options['watch']:
                if not run.ok:
                    raise CommandError('Обход не завершён: ' + run.error)
                return
            time.sleep(options['interval'])
