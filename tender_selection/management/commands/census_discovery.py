"""Operator-only bounded census using the normal ingestion and intake services."""
import json
import os
import time
import signal
import math
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db.models import Q

from tender_selection import gosplan
from tender_selection.models import FilterSettings, Tender
from tender_selection.primary_search import PrimaryFilterForm
from tender_selection.services import run_pull, save_selection_settings


class Command(BaseCommand):
    help = 'Полный контроль выдачи и повторная сверка; импорт в TEST, без ЕИС/ЕАТ/AI.'

    def add_arguments(self, parser):
        parser.add_argument('--preset', required=True)
        parser.add_argument('--max-seconds', type=float, default=1800)
        parser.add_argument('--passes', type=int, default=2)

    def handle(self, *args, **options):
        if os.getenv('TENDER_DISCOVERY_ROLE') != 'standalone' or os.getenv('GOSPLAN_API_DISABLED') == '1':
            raise CommandError('Требуется отдельный разрешённый TEST-процесс со standalone ролью')
        if options['max_seconds'] <= 0 or options['passes'] < 1:
            raise CommandError('Бюджет и число проходов должны быть положительными')
        os.environ['TENDER_DISCOVERY_ENRICH_AFTER_PULL'] = '0'
        payload = json.loads(Path(options['preset']).read_text())
        selection = FilterSettings.load()
        appended = payload.pop('append_exclude_words', '')
        payload['exclude_words'] = '\n'.join(dict.fromkeys(
            line.strip() for line in (selection.exclude_words + '\n' + appended).splitlines() if line.strip()))
        # A census preset describes the entire primary scope, not a partial patch.
        payload['primary_filters'] = {**dict.fromkeys(PrimaryFilterForm.base_fields), **payload['primary_filters']}
        save_selection_settings(payload)
        signal.alarm(math.ceil(options['max_seconds']) + 30)
        overall_deadline = time.monotonic() + options['max_seconds']
        previous = None
        summary = {'complete': False, 'passes': []}
        original = gosplan.fetch_page
        for index in range(options['passes']):
            counts = Counter(http_attempts=0, http_errors=0, successful_pages=0, terminal_pages=0)
            statuses = Counter()
            seen = set()
            groups = {}

            def measured(params, law='fz44'):
                counts['http_attempts'] += 1
                try:
                    rows = original(params, law)
                except Exception as exc:
                    status = getattr(exc.__cause__, 'code', None)
                    statuses[str(status) if status else 'network_or_other'] += 1
                    counts['http_errors'] += 1
                    self.stdout.write(json.dumps({'pass': index+1, 'attempts': counts['http_attempts'],
                        'error': str(exc)[:300], 'page_not_skipped': True}, ensure_ascii=False))
                    raise
                statuses['200'] += 1
                counts['successful_pages'] += 1
                counts['terminal_pages'] += int(len(rows) < gosplan.PAGE_SIZE)
                group = groups.setdefault(law+':'+params.get('object_info', ''), set())
                for row in rows:
                    key = (law, str(row.get('purchase_number') or '').strip())
                    if not key[1]:
                        raise gosplan.GosplanError('Нет номера извещения; полнота не подтверждена')
                    seen.add(key)
                    group.add(key)
                self.stdout.write(json.dumps({'pass': index+1, 'attempts': counts['http_attempts'],
                    'successful_pages': counts['successful_pages'], 'unique': len(seen),
                    'received': len(rows)}, ensure_ascii=False))
                return rows

            remaining = overall_deadline-time.monotonic()
            if remaining <= 0:
                raise CommandError('Время контроля истекло; полнота не подтверждена')
            with patch.object(gosplan, 'fetch_page', side_effect=measured):
                run = run_pull(full_scan=True, max_seconds=remaining)
            identity = Q(pk__in=[])
            for law in {law for law, _ in seen}:
                identity |= Q(law=law, purchase_number__in=[number for key_law, number in seen if key_law == law])
            records = Tender.objects.filter(identity, source=Tender.EIS)
            metrics = {**counts, 'statuses': dict(statuses), 'unique_records': len(seen),
                'group_unique_counts': {key: len(value) for key, value in groups.items()},
                'expired_deadlines': records.filter(collecting_finished_at__lt=timezone.now()).count(),
                'future_deadlines': records.filter(collecting_finished_at__gte=timezone.now()).count(),
                'unknown_deadlines': records.filter(collecting_finished_at__isnull=True).count(),
                'stored_archived': records.filter(status=Tender.DISMISSED).count(),
                'stored_incoming': records.filter(status=Tender.NEW).count()}
            if previous is not None:
                metrics['missing_in_repeat'] = len(previous-seen)
                metrics['new_in_repeat'] = len(seen-previous)
                metrics['same_notice_set'] = previous == seen
            run.params['census'] = metrics
            run.save(update_fields=['params'])
            result = {'run_id':run.pk, 'complete':bool(run.ok and run.params.get('complete')),
                'jobs_total':run.params.get('jobs_total'), 'jobs_completed':run.params.get('jobs_completed'),
                'created':run.created_count, 'updated':run.updated_count, **metrics}
            summary['passes'].append(result)
            self.stdout.write(json.dumps({'census_pass':result}, ensure_ascii=False))
            if not result['complete']:
                raise CommandError('Обход не завершён: '+run.error)
            previous = seen
        summary['complete'] = True
        summary['http_attempts_total'] = sum(p['http_attempts'] for p in summary['passes'])
        summary['coverage'] = 'Выдача ГосПлана по сохранённым условиям; не доказательство глобальной полноты ЕИС.'
        signal.alarm(0)
        self.stdout.write(json.dumps({'census_result':summary}, ensure_ascii=False))
