import os,json,re
from unittest.mock import patch
os.environ.setdefault('DJANGO_SETTINGS_MODULE','mysite.settings')
import django
django.setup()
from django.db import connection,transaction
from tender_selection.models import Tender
from tenders.models import TenderComputeJob
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher,assess_quality,document_analysis_sources,source_route_snapshots
if connection.settings_dict.get('HOST')!='test-db': raise RuntimeError('TEST only')
with transaction.atomic():
 with connection.cursor() as cursor: cursor.execute('SET TRANSACTION READ ONLY')
 tender=Tender.objects.get(purchase_number=os.environ['INSPECT_TENDER_NUMBER'])
 jobs=TenderComputeJob.objects.filter(tender=tender).order_by('-pk')
 job=next((j for j in jobs if j.diagnostics.get('stages',{}).get('structure',{}).get('result')),None)
 if not job: raise RuntimeError('No stored structure')
 sources=list(document_analysis_sources(tender)[0].values())
 engine=GatewayDocumentEnricher(max_cost_rub=0)
 with patch('tender_selection.services.documents_html'):
  engine.prepare_stages(tender,sources,assess_quality(tender,sources)['documents'])
 structure=job.diagnostics['stages']['structure']['result']
 print('KIT_RESULT',json.dumps({'job':job.pk,'status':job.status,'sources':[{'id':s.pk,'name':s.original_text,'quantity':str(s.quantity),'price':str(s.unit_price) if hasattr(s,'unit_price') else None} for s in sources], 'structure':structure,'routes':{str(k):{'state':v.get('state'),'steps':len(v.get('steps',[]))} for k,v in source_route_snapshots(tender).items()},'gaps':job.diagnostics.get('analysis_gaps',[])},ensure_ascii=False,default=str))
 for i,d in engine.document_lookup.items():
  text=d.get('evidence_text','')
  if text: print('KIT_DOCUMENT',json.dumps({'id':i,'name':d['name'],'matches':[text[max(0,m.start()-100):m.start()+650] for m in list(re.finditer('набор|комплект',text,re.I))[:5]]},ensure_ascii=False))
