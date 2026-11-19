"""Export only saved intake inputs for local replay. Never deploy, write, or call a model."""
import base64
import json
import os
import zlib


def collect_snapshot():
    from django.core import serializers
    from django.db.models import Q
    from tender_selection.models import DocumentPreview, Tender, TenderDocument
    from tender_selection.notification import parse_notification
    from tenders.models import (CalculationComponent, ComponentOperationStep, ComponentRoutePlan,
        ProcessDefinition, TenderCommercialItem, TenderComputeJob, TenderSourceItem)
    from tenders.calculation_v2_pipeline import _knowledge_for, _route_owner_answer_map

    tenders = Tender.objects.exclude(status=Tender.DISMISSED).filter(v2_compute_jobs__isnull=False).distinct().order_by('pk')
    tender_ids = list(tenders.values_list('pk', flat=True))
    sources = TenderSourceItem.objects.filter(tender_id__in=tender_ids).order_by('pk')
    links = TenderDocument.objects.filter(tender_id__in=tender_ids).order_by('pk')
    urls = {doc['url'] for tender in tenders for doc in parse_notification(tender.notification_raw).get('documents', []) if doc.get('url')}
    fixtures = []
    def append(query, fields=None):
        fixtures.extend(json.loads(serializers.serialize('json', query, **({'fields': fields} if fields else {}))))
    append(tenders, ['purchase_number', 'law', 'title', 'object_info', 'notification_raw', 'status', 'review', 'conditions', 'outcome_status'])
    append(ProcessDefinition.objects.order_by('pk'))
    append(TenderComputeJob.objects.filter(tender_id__in=tender_ids).order_by('pk'))
    append(sources)
    append(DocumentPreview.objects.filter(Q(pk__in=links.values('preview_id')) | Q(url__in=urls)).distinct().order_by('pk'))
    append(links)
    append(TenderCommercialItem.objects.filter(tender_id__in=tender_ids).order_by('pk'))
    append(CalculationComponent.objects.filter(commercial_item__tender_id__in=tender_ids).order_by('pk'))
    plans = ComponentRoutePlan.objects.filter(commercial_item__tender_id__in=tender_ids).order_by('pk')
    append(plans)
    append(ComponentOperationStep.objects.filter(route_plan__in=plans).order_by('pk'))
    return {'fixtures': fixtures, 'tender_ids': tender_ids, 'knowledge': _knowledge_for([]),
        'owner_answers': _route_owner_answer_map(list(sources)), 'read_only': True, 'model_called': False}


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
    import django
    django.setup()
    from django.db import connection, transaction
    if connection.vendor != 'postgresql':
        raise RuntimeError('Snapshot requires a PostgreSQL read-only transaction')
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
        snapshot = collect_snapshot()
        encoded = base64.b64encode(zlib.compress(json.dumps(snapshot, ensure_ascii=False).encode())).decode()
        print(json.dumps({'offline_intake_snapshot': encoded, 'tenders': len(snapshot['tender_ids']),
            'records': len(snapshot['fixtures']), 'read_only': True, 'model_called': False}), flush=True)


if __name__ == '__main__':
    main()
