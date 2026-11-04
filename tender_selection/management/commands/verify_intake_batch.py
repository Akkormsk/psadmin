"""Bounded TEST acceptance through the existing refresh executor, never a new analyser."""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Sum

from tender_selection.models import FilterSettings, Tender
from tender_selection.services import _run_document_analysis, refresh_document_analysis, intake_readiness_for
from tender_selection.intake_readiness import summarize
from tenders.models import TenderComputeJob
from tenders.analysis_presentation import analysis_cost


class Command(BaseCommand):
    help = 'Verify current TEST incoming tenders with a persistent total spending ceiling; stops at first incomplete result.'

    def add_arguments(self, parser):
        parser.add_argument('--max-cost-rub', required=True, type=Decimal)
        parser.add_argument('--max-job-cost-rub', type=Decimal, default=Decimal('6'))
        parser.add_argument('--run-id', required=True)
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--profile-only', action='store_true')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql' or connection.settings_dict.get('HOST') != 'test-db':
            raise CommandError('Acceptance command is restricted to the isolated TEST database')
        budget, per_job = options['max_cost_rub'], options['max_job_cost_rub']
        if not budget.is_finite() or not per_job.is_finite() or min(budget, per_job) <= 0 or not 1 <= options['limit'] <= 1000:
            raise CommandError('Finite positive spending ceilings and a bounded limit are required')
        from tender_selection.scheduler import _web_autopull_enabled
        if _web_autopull_enabled():
            raise CommandError('TEST autopull must be stopped during the bounded acceptance run')
        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).first()
        if not actor:
            raise CommandError('No active administrator')
        cohort = intake_readiness_for(actor)
        ids = [row['tender_id'] for row in cohort['pending']][:options['limit']]
        selection = FilterSettings.objects.get(pk=1)
        selection.profile_triage_enabled = True
        selection.save(update_fields=['profile_triage_enabled'])
        run_id = options['run_id']
        if options['profile_only']:
            from tender_selection.profile_triage import triage_tenders
            from tender_selection.services import notification_for
            for tender in Tender.objects.filter(pk__in=ids):
                if not tender.notification_raw:
                    notification_for(tender)
            metrics = {}
            checked = triage_tenders(ids, max_cost_rub=budget, diagnostics=metrics)
            self.stdout.write(json.dumps({'profile_only': True, 'checked': checked, 'metrics': metrics,
                'signals': list(Tender.objects.filter(pk__in=ids).values('purchase_number','profile_signal','profile_reason'))}, ensure_ascii=False, default=str))
            return
        rows = []
        for pk in ids:
            spent = TenderComputeJob.objects.filter(diagnostics__acceptance_run=run_id).aggregate(value=Sum('total_cost'))['value'] or Decimal('0')
            remaining = budget - spent - Decimal('.01')
            if remaining <= 0:
                raise CommandError('Acceptance spending ceiling reached; no further model request was made')
            with patch('threading.Thread'):
                started = refresh_document_analysis(actor, pk)
            if not started['started']:
                raise CommandError('Selected tender already has an active job; no duplicate run started')
            job = TenderComputeJob.objects.get(pk=started['job_id'])
            job.trigger = 'bounded_intake_acceptance'
            job.diagnostics.update(acceptance_run=run_id, max_total_cost_rub=str(min(per_job, remaining)))
            job.save(update_fields=['trigger','diagnostics','updated_at'])
            _run_document_analysis(job.pk)
            job.refresh_from_db()
            tender = Tender.objects.get(pk=pk)
            proof = summarize([tender])
            excluded = bool(job.diagnostics.get('profile_excluded'))
            report = {'number': tender.purchase_number, 'job_id': job.pk, 'status': job.status,
                'cost': analysis_cost(job), 'profile_excluded': excluded, 'readiness': proof,
                'gaps': job.diagnostics.get('analysis_gaps', []),
                'stages': {key: {field: value.get(field) for field in ('state','outcome','usage','cost_rub','model','started_at','reused_from')} for key,value in job.diagnostics.get('stages',{}).items()}}
            rows.append(report)
            self.stdout.write('INTAKE_ACCEPTANCE ' + json.dumps(report, ensure_ascii=False, default=str))
            if not excluded and not proof['all_complete']:
                raise CommandError('First incomplete result saved; paid batch stopped for diagnosis')
        final = intake_readiness_for(actor)
        self.stdout.write('INTAKE_ACCEPTANCE_COMPLETE ' + json.dumps({'run_id': run_id, 'processed': len(rows),
            'readiness': final}, ensure_ascii=False, default=str))
        if not final['all_complete']:
            raise CommandError('Current incoming cohort is not fully verified; background rollout remains blocked')
