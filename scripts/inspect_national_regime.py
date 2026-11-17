"""Summarize saved national-regime coverage; no document downloads or AI calls."""
import json
import os
from collections import Counter
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()
from django.db import connection, transaction
from tender_selection.models import Tender
from tender_selection.national_regime import tender_items
from tenders.models import TenderComputeJob

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
    print(json.dumps({'number': number, 'regime_counts': dict(Counter(row['regime']['status'] for row in tender_items(tender))),
        'risk_flags': {key: risk.get(key) for key in ('stale', 'preliminary', 'degraded')},
        'risk_documents_sufficient': (risk.get('risk_facts') or {}).get('documents_sufficient'),
        'risk_factor_codes': [row.get('code') for row in risk.get('risk_factors', [])],
        'job_status': job.status if job else None,
        'stages': {key: {field: stage.get(field) for field in ('state', 'outcome')} for key, stage in diagnostics.get('stages', {}).items()},
        'gap_stages': [row.get('stage') for row in diagnostics.get('analysis_gaps', [])],
        'reading_counts': {key: reading.get(key) for key in ('read', 'total', 'complete', 'storage')},
        'reading_keys': list(reading),
    }, ensure_ascii=False))
