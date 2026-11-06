import os,json,re
from unittest.mock import patch
os.environ.setdefault('DJANGO_SETTINGS_MODULE','mysite.settings')
import django
django.setup()
from django.db import connection,transaction
from tender_selection.models import Tender
from tenders.models import TenderComputeJob
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher,assess_quality,document_analysis_sources
if connection.settings_dict.get('HOST')!='test-db': raise RuntimeError('TEST only')
tender=Tender.objects.get(purchase_number='0148300000626001129')
with transaction.atomic():
 with connection.cursor() as cursor: cursor.execute('SET TRANSACTION READ ONLY')
 engine=GatewayDocumentEnricher(max_cost_rub='0')
 sources=list(document_analysis_sources(tender)[0].values())
 with patch('tender_selection.services.documents_html'):
  engine.prepare_stages(tender,sources,assess_quality(tender,sources)['documents'])
 for n in (141,142):
  job=TenderComputeJob.objects.filter(pk=n,tender=tender).first()
  if not job: continue
  for key,stage in job.diagnostics.get('stages',{}).items():
   if not key.startswith('tz'):continue
   for row in stage.get('result') or []:
    for fact in row.get('characteristics',[]) if isinstance(row,dict) else []:
     if 'Гарантия качества товара' in str(fact.get('name','')):
      print('FACT',json.dumps({'job':n,'stage':key,'key':row.get('key'),'fact':fact},ensure_ascii=False,default=str))
 for i,doc in engine.document_lookup.items():
  txt=doc.get('evidence_text','')
  hits=[txt[max(0,m.start()-110):m.start()+250] for m in list(re.finditer('гарант',txt,re.I))[:4]]
  print('DOCUMENT',json.dumps({'id':i,'name':doc['name'],'exact_quote_present':'требуется гарантия качества товара, работы, услуги' in txt.lower(),'excerpts':hits},ensure_ascii=False))
for n in (141,142):
 job=TenderComputeJob.objects.filter(pk=n,tender=tender).first()
 if not job: continue
 d=(job.diagnostics.get('stages',{}).get('documents',{}).get('result') or {}).get('documents') or []
 print('SAVED_MANIFEST',json.dumps({'job':n,'contract':job.diagnostics.get('intake_contract'),'docs':[{'id':i+1,'name':x.get('name'),'reading_status':x.get('reading_status')} for i,x in enumerate(d)]},ensure_ascii=False))
