"""Bounded TEST QA through the existing classifier; never creates lessons or manual decisions."""
import json
import os
from decimal import Decimal

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
import django
django.setup()
from django.db import connection
from tender_selection.models import Tender
from tender_selection.profile_triage import triage_tenders
from tender_selection.services import notification_for

if connection.vendor != 'postgresql' or connection.settings_dict.get('HOST') != 'test-db':
    raise RuntimeError('Profile verification is restricted to TEST')
numbers = os.environ['PROFILE_QA_NUMBERS'].split(',')
if not numbers or any(len(number) != 19 or not number.isdigit() for number in numbers):
    raise ValueError('Explicit purchase numbers are required')
budget = Decimal(os.environ['PROFILE_QA_BUDGET'])
if not budget.is_finite() or not 0 < budget <= Decimal('3.12'):
    raise ValueError('Remaining approved profile QA allocation cannot exceed 3.12 RUB')
tenders = list(Tender.objects.filter(purchase_number__in=numbers))
if len(tenders) != len(set(numbers)):
    raise ValueError('Some requested tenders are missing from TEST')
manual = {tender.pk: (tender.status, tender.review) for tender in tenders}
for tender in tenders:
    if not tender.notification_raw:
        notification_for(tender)
metrics = {}
checked = triage_tenders([tender.pk for tender in tenders], max_cost_rub=budget, diagnostics=metrics)
rows = list(Tender.objects.filter(pk__in=manual).values('pk','purchase_number','status','review','profile_signal','profile_reason'))
if any((row['status'], row['review']) != manual[row['pk']] for row in rows):
    raise RuntimeError('Manual decisions changed during classification')
print('PROFILE_QA ' + json.dumps({'checked': checked, 'metrics': metrics, 'signals': rows, 'manual_decisions_preserved': True}, ensure_ascii=False, default=str))
