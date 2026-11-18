"""Summarize saved national-regime coverage; no document downloads or AI calls."""
import json
import os
from collections import Counter
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()
from django.db import connection, transaction
from tender_selection.models import Tender
from tender_selection.national_regime import tender_items, risk_projection
from tenders.models import TenderComputeJob, TenderSourceItem, TenderCommercialItem
from tenders.source_memory import signature
from tenders.document_enrichment import document_proposal_changes

number = os.environ['INSPECT_TENDER_NUMBER']
if len(number) != 19 or not number.isascii() or not number.isdigit():
    raise ValueError('Expected one purchase number')
if connection.vendor != 'postgresql':
    raise RuntimeError('Inspection requires PostgreSQL read-only transaction')
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
    tender = Tender.objects.get(purchase_number=number)
    job = TenderComputeJob.objects.filter(tender=tender, version__startswith='v2').order_by('-pk').first()
    diagnostics = job.diagnostics if job else {}
    reading = diagnostics.get('source_reading') or {}
    risk = tender.risk_assessment or {}
    projected = risk_projection(tender, risk)
    print(json.dumps({'number': number, 'regime_counts': dict(Counter(row['regime']['status'] for row in tender_items(tender))),
        'national_summary': projected.get('national_regime'),
        'national_checked': projected.get('national_regime_checked'),
        'display_factor_codes': [row.get('code') for row in projected.get('risk_factors', [])],
        'risk_flags': {key: risk.get(key) for key in ('stale', 'preliminary', 'degraded')},
        'risk_documents_sufficient': (risk.get('risk_facts') or {}).get('documents_sufficient'),
        'risk_factor_codes': [row.get('code') for row in risk.get('risk_factors', [])],
        'job_status': job.status if job else None,
        'stages': {key: {field: stage.get(field) for field in ('state', 'outcome')} for key, stage in diagnostics.get('stages', {}).items()},
        'gap_stages': [row.get('stage') for row in diagnostics.get('analysis_gaps', [])],
        'reading_counts': {key: reading.get(key) for key in ('read', 'total', 'complete', 'storage')},
        'reading_keys': list(reading),
        'source_rows': [{'id': row.pk, 'parent': row.parent_id, 'supersedes': row.supersedes_id,
            'type': row.source_type, 'active': row.is_active, 'key': row.source_key,
            'index': row.metadata.get('notification_index'), 'code': row.metadata.get('code'),
            'notice_regime_key': signature({key: (row.metadata.get('notification_national_regime') or {}).get(key) for key in ('status', 'basis')}), 'name_key': signature(row.original_text),
            'quantity': str(row.quantity), 'relationship': row.metadata.get('relationship'),
            'replaces': row.metadata.get('replaces_source_ids'),
            'requirements_key': signature(row.requirements), 'review_count': len(row.metadata.get('review_history', []))}
            for row in TenderSourceItem.objects.filter(tender=tender).order_by('pk')],
        'pending_review': [{'id': row.pk, 'name': row.original_text, 'quantity': str(row.quantity), 'unit': row.unit,
            'replaces': row.metadata.get('replaces_source_ids'),
            'changes': {key: value for key, value in document_proposal_changes(row).items() if key not in {'rows', 'removed'}},
            'added': [fact.get('name') for fact in document_proposal_changes(row).get('rows', [])],
            'removed': [fact.get('name') for fact in document_proposal_changes(row).get('removed', [])]}
            for row in TenderSourceItem.objects.filter(tender=tender, source_type='document_proposal').select_related('parent').order_by('pk')],
        'working_rows': [{'id': row.pk, 'status': row.status, 'source_ids': list(row.source_items.values_list('pk', flat=True)),
            'draft': row.provenance.get('draft')}
            for row in TenderCommercialItem.objects.filter(tender=tender).order_by('pk')],
    }, ensure_ascii=False))
