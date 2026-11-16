"""Explicit operator trial; never scheduled or invoked by application requests."""
import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from tenders.tender_data_transfer import apply_transfer_plan, collect_transfer_plan, transfer_report


class Command(BaseCommand):
    help = 'Preview tender-data transfer; writes are allowed only to an explicitly configured isolated database alias.'

    def add_arguments(self, parser):
        parser.add_argument('--database', default=DEFAULT_DB_ALIAS, choices=tuple(connections))
        parser.add_argument('--tender', type=int, action='append', help='Tender ID; repeat to restrict the trial.')
        parser.add_argument('--apply', action='store_true', help='Apply to a quiescent isolated database, retaining all legacy fields.')

    def handle(self, *args, **options):
        using = options['database']
        if options['apply'] and using == DEFAULT_DB_ALIAS:
            raise CommandError('Для пробной записи задайте отдельный alias изолированной базы, не default.')
        plan = collect_transfer_plan(using=using, tender_ids=options['tender'])
        try:
            report = apply_transfer_plan(plan, using=using) if options['apply'] else transfer_report(plan)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        except ValidationError as exc:
            raise CommandError('Перенос отменён: проверка целостности не пройдена. Исходные данные сохранены.') from exc
        report['applied'] = options['apply']
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
