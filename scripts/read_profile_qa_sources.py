"""Export explicitly selected existing sources under PostgreSQL READ ONLY; no models called."""
import json
import os
os.environ['TENDER_AUTOPULL_ENABLED'] = '0'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
import django
django.setup()
from django.db import connection
from tender_selection.models import Tender
numbers = os.environ['PROFILE_QA_NUMBERS'].split(',')
if any(len(number) != 19 or not number.isdigit() for number in numbers):
    raise ValueError('Explicit purchase numbers required')
with connection.cursor() as cursor:
    cursor.execute('SET default_transaction_read_only = on')
rows = list(Tender.objects.filter(purchase_number__in=numbers).values('pk','purchase_number','title','object_info','okpd2','notification_raw'))
if len(rows) != len(set(numbers)):
    raise ValueError('Requested source missing')
print(json.dumps(rows, ensure_ascii=False, default=str))
