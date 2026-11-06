import os, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE','mysite.settings')
import django
django.setup()
from django.db import transaction, connection
from tender_selection.models import Tender, DocumentPreview
from tender_selection.notification import parse_notification
with transaction.atomic():
 with connection.cursor() as cursor: cursor.execute('SET TRANSACTION READ ONLY')
 tender = Tender.objects.get(purchase_number='0101200010526000014')
 docs = parse_notification(tender.notification_raw or {}).get('documents', [])
 for idx, doc in enumerate(docs):
  if 'описание объекта закупки' not in doc.get('name','').lower(): continue
  cache = DocumentPreview.objects.filter(url=doc.get('url','')).first()
  print('ARCHIVE_DIAG',json.dumps({'index':idx,'name':doc.get('name'),'cached_kind':cache.kind if cache else None,'cached_html_prefix':(cache.html or '')[:180] if cache else None,'shortcut_used':bool(cache and cache.kind not in {'zip','zip-content','rar'}),'updated':str(cache.updated_at) if cache and hasattr(cache,'updated_at') else None},ensure_ascii=False))
