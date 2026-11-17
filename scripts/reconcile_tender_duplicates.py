"""Explicit single-TEST-tender retirement of proven equal groups; preserves history."""
import json
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()
from django.db import connection, transaction
from tender_selection.models import Tender
from tenders.tender_data import reconcile_duplicate_notice_items
from tenders.calculation_v2_pipeline import active_calculation_items

number = os.environ['INSPECT_TENDER_NUMBER']
mode = os.environ['SOURCE_DUPLICATE_REPAIR']
if len(number) != 19 or not number.isascii() or not number.isdigit() or mode not in {'preview', 'apply'}:
    raise ValueError('Expected one purchase number and preview/apply')
if connection.vendor != 'postgresql':
    raise RuntimeError('TEST operation requires PostgreSQL')
with transaction.atomic():
    if mode == 'preview':
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
    tender = Tender.objects.get(purchase_number=number)
    before = {'active_sources': len(active_calculation_items(tender)),
              'active_goods': tender.v2_commercial_items.filter(status='active').count()}
    plan = reconcile_duplicate_notice_items(tender, apply=mode == 'apply')
    after = {'active_sources': len(active_calculation_items(tender)),
             'active_goods': tender.v2_commercial_items.filter(status='active').count()}
    print(json.dumps({'number': number, 'mode': mode, 'before': before, 'plan': plan, 'after': after}))
