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
from django.test import RequestFactory
from django.contrib.auth import get_user_model
from tender_selection.views import doc_preview
from unittest.mock import patch
from hashlib import sha256
user=get_user_model().objects.filter(is_active=True,is_superuser=True).first()
factory=RequestFactory()
for label,agent in [('desktop','Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'),('ipad','Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1')]:
 with transaction.atomic():
  with connection.cursor() as cursor: cursor.execute('SET TRANSACTION READ ONLY')
  req=factory.get('/preview/',HTTP_USER_AGENT=agent); req.user=user
  response=doc_preview(req,tender.pk,2)
  data=json.loads(response.content)
  print('DEVICE_RESPONSE',json.dumps({'device':label,'status':response.status_code,'kind':data.get('kind'),'zip_entries':len(data.get('zip_entries') or []),'body_hash':sha256(response.content).hexdigest()}))
  req=factory.get('/preview/?format=pdf',HTTP_USER_AGENT=agent); req.user=user
  with patch('tender_selection.views._fetch_doc_bytes',return_value=b'%PDF-1.4\nsynthetic-probe'):
   response=doc_preview(req,tender.pk,2)
  print('DEVICE_PDF_HEADERS',json.dumps({'device':label,'status':response.status_code,'content_type':response.get('Content-Type'),'frame_options':response.get('X-Frame-Options'),'content_disposition':response.get('Content-Disposition')}))
