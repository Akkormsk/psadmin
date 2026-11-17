"""One direct EIS notice read from TEST; no application writes or AI calls."""
import json
import os
from urllib.parse import urlsplit

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()

from django.db import connection, transaction
from tender_selection import eis_docs, eis_source
from tender_selection.models import Tender
from tender_selection.notification import parse_notification

number = os.environ['INSPECT_TENDER_NUMBER']
if len(number) != 19 or not number.isascii() or not number.isdigit():
    raise ValueError('Expected one purchase number')
if connection.vendor != 'postgresql':
    raise RuntimeError('TEST inspection requires PostgreSQL read-only transaction')

with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
    tender = Tender.objects.get(purchase_number=number)
    report = {'number': number, 'token_configured': bool(eis_docs.token()),
              'saved_notice': bool(tender.notification_raw),
              'saved_error': eis_docs._redact(tender.notification_error or '')}
    try:
        payload = eis_source.notification(number)
        card = parse_notification(payload)
        report.update(direct_eis='ok', document_type=payload.get('doc_type'),
                      items=len(card.get('items') or []),
                      documents=len(card.get('documents') or []),
                      customer_present=bool((card.get('customer') or {}).get('name')))
    except eis_docs.EisDocsError as exc:
        report.update(direct_eis='error', error=eis_docs._redact(str(exc)))
        if 'за пределами доверенного домена' in str(exc):
            report['archive_locations'] = [
                {'scheme': urlsplit(url).scheme, 'host': urlsplit(url).hostname}
                for url in eis_docs.fetch_archive_urls(number)
            ]
    print(json.dumps(report, ensure_ascii=False))
