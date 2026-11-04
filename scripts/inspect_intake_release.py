"""Read-only release/intake audit. No model, fetching, migrations or settings writes."""
import json
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()
from django.apps import apps
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Max
from django.utils import timezone


def emit(key, data):
    print(key + ' ' + json.dumps(data, ensure_ascii=False, default=str), flush=True)


def main():
    if connection.vendor != 'postgresql':
        raise RuntimeError('Expected PostgreSQL')
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute('SHOW transaction_read_only')
            if cursor.fetchone()[0] != 'on':
                raise RuntimeError('Read-only guard missing')
            tables = set(connection.introspection.table_names(cursor))
        inventory = []
        for model in apps.get_models():
            if model._meta.managed and model._meta.db_table in tables:
                inventory.append({'model': model._meta.label, 'rows': model.objects.count(),
                    'max_pk': model.objects.aggregate(value=Max('pk'))['value'] if model._meta.pk.get_internal_type() in {'AutoField','BigAutoField','IntegerField'} else None})
        emit('DATA_INVENTORY', inventory)
        emit('SAFE_FLAGS', {name: getattr(settings, name) for name in dir(settings)
            if name.startswith('CALCULATION_V2_') and name.endswith('ENABLED')})
        with connection.cursor() as cursor:
            cursor.execute('SELECT app, name FROM django_migrations ORDER BY app,name')
            emit('APPLIED_MIGRATIONS', cursor.fetchall())
        from tender_selection.models import FilterSettings, Tender
        fields = {field.name for field in Tender._meta.concrete_fields}
        selection = FilterSettings.objects.filter(pk=1).first()
        emit('INTAKE_SETTINGS', {name: getattr(selection, name, None) for name in
            ('include_words','exclude_words','min_price','window_days','okpd2_codes','profile_triage_enabled')})
        qs = Tender.objects.all()
        emit('TENDER_COUNTS', list(qs.values('status','review').annotate(count=Count('pk')).order_by('status','review')))
        wanted = [name for name in ('pk','purchase_number','title','object_info','okpd2','status','review','collecting_finished_at','profile_signal','profile_confidence','profile_reason','profile_model') if name in fields]
        try:
            from tender_selection.services import visible_found_tenders
        except ImportError:
            from tender_selection.views import _visible_found_tenders as visible_found_tenders
        incoming = qs.filter(status=Tender.NEW, review=Tender.UNREVIEWED).defer(*[name for name in ('notification_raw','extras_raw') if name in fields])
        visible, hidden, expired = visible_found_tenders(incoming, min_price=selection.min_price,
            include_words=selection.include_words, exclude_words=selection.exclude_words)
        ids = [tender.pk for tender in visible]
        emit('INCOMING_TOTAL', {'visible': len(ids), 'word_hidden': hidden, 'expired': expired})
        emit('INCOMING_SAMPLE', list(qs.filter(pk__in=ids).order_by('-pk').values(*wanted)[:80]))
        feedback_model = next((model for model in apps.get_models() if model._meta.app_label == 'tender_selection' and model.__name__ == 'TenderDismissalFeedback'), None)
        if feedback_model:
            emit('DISMISSED_SAMPLE', list(feedback_model.objects.order_by('-pk').values('reason','created_at','tender__purchase_number','tender__title','tender__okpd2')[:100]))
        emit('HIDDEN_SAMPLE', list(qs.filter(status=Tender.NEW, review=Tender.UNREVIEWED).exclude(pk__in=ids).order_by('-pk').values(*wanted)[:50]))
        job_model = next((model for model in apps.get_models() if model._meta.label == 'tenders.TenderComputeJob'), None)
        if job_model:
            latest = {}
            for job in job_model.objects.filter(tender_id__in=ids).order_by('tender_id','-pk').values('pk','tender_id','status','error','diagnostics'):
                latest.setdefault(job['tender_id'], job)
            emit('LATEST_INTAKE_STATUS', {'with_job':len(latest),'missing_job':len(ids)-len(latest),
                'statuses': {state:sum(row['status']==state for row in latest.values()) for state in sorted({row['status'] for row in latest.values()})},
                'issues':[{'tender_id':row['tender_id'],'job':row['pk'],'status':row['status'],'error':row['error'],
                    'gaps':row['diagnostics'].get('analysis_gaps',[]), 'routing':row['diagnostics'].get('stages',{}).get('routing',{}).get('state')}
                    for row in latest.values() if row['error'] or row['diagnostics'].get('analysis_gaps') or row['status'] not in {'ready','needs_review'}]})
            from tenders import calculation_v2_pipeline
            source_route_snapshots = getattr(calculation_v2_pipeline, 'source_route_snapshots', None)
            if source_route_snapshots is None:
                emit('ROUTE_COVERAGE', {'available':False,'reason':'Deployed version has no intake route presentation'})
                emit('AUDIT_COMPLETE', {'read_only':True,'model_called':False,'time':timezone.now()})
                return
            coverage = []
            for tender in qs.filter(pk__in=ids).defer(*[name for name in ('notification_raw','extras_raw') if name in fields]).iterator():
                routes = source_route_snapshots(tender)
                coverage.append({'number':tender.purchase_number,'items':len(routes),'ready':sum(row['state']=='ready' and bool(row['steps']) for row in routes.values()),
                    'states': sorted({row['state'] for row in routes.values()})})
            emit('ROUTE_COVERAGE', coverage)
        emit('AUDIT_COMPLETE', {'read_only':True,'model_called':False,'time':timezone.now()})


main()
