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
from collections import Counter
from copy import deepcopy
from django.core.exceptions import ObjectDoesNotExist
job=TenderComputeJob.objects.get(pk=141,tender=tender)
engine.document_lookup={key:{**value,'image_indexes':[]} for key,value in engine.document_lookup.items()}
c=Counter(); examples=[]
for row in (job.diagnostics.get('stages',{}).get('tz',{}).get('result') or []):
 for fact in row.get('characteristics',[]) if isinstance(row,dict) else []:
  quote=str(fact.get('quote') or '').strip()
  if not quote: c['without_quote']+=1; continue
  c['with_quote']+=1
  candidates=[]
  for doc_id in engine.document_lookup:
   try: engine.stage_quote({'doc_id':doc_id,'quote':quote}); candidates.append(doc_id)
   except ValueError: pass
  stated=fact.get('doc_id')
  if stated in candidates: c['matches_stated']+=1
  elif candidates:
   c['wrong_id_but_found']+=1
   if len(candidates)>1: c['wrong_id_ambiguous']+=1
  else: c['not_found_in_text']+=1
  value=fact.get('value')
  if isinstance(value,str) and value.strip():
   c['string_value']+=1
   if ' '.join(value.casefold().split()) not in ' '.join(quote.casefold().split()): c['value_not_literal_quote']+=1
  if stated not in candidates and len(examples)<4: examples.append({'name':fact.get('name'),'stated':stated,'candidates':candidates})
print('SAVED_FACT_REVIEW',json.dumps({'counts':c,'examples':examples},ensure_ascii=False))
found=[]
for anchor,fragment in getattr(engine,'source_fragments',{}).items():
 text=' '.join(fragment.get('text','').casefold().split())
 if 'требуется гарантия качества товара, работы, услуги' in text and 'нет' in text:
  try:
   result=engine.stage_quote({'fragment_id':anchor,'doc_id':4,'quote':'Требуется гарантия качества товара, работы, услуги','value':'Нет'})
   found.append({'anchor':anchor,'section':result['page_or_section'],'correct_document':result['document_url']==engine.document_lookup[5]['url']})
  except ValueError: pass
print('FRAGMENT_WARRANTY_PROOF',json.dumps({'matches':found},ensure_ascii=False))
job=TenderComputeJob.objects.filter(pk=144,tender=tender).first()
if job:
 for r in job.diagnostics.get('stages',{}).get('tz',{}).get('result') or []:
  for f in r.get('characteristics',[]) if isinstance(r,dict) else []:
   if f.get('name') in ['Режим поставки','Гарантия качества товара','Экспертиза','Изображение на циферблате (требование макета)']:
    ref=f.get('fragment_id'); source=getattr(engine,'source_fragments',{}).get(ref,{})
    print('LIVE_FRAGMENT_FACT',json.dumps({'key':r.get('key'),'fact':f,'fragment':source},ensure_ascii=False,default=str))
if job:
 goods=engine.reconcile_structure(job.diagnostics['stages']['structure'].get('result') or [])
 compiled=engine.reconcile_specifications(job.diagnostics['stages']['tz'].get('result') or [],goods)
 print('FRAGMENT_REAL_REPLAY',json.dumps({'goods':len(goods),'compiled':len(compiled),'gaps':engine.specification_gaps,'warranties':[{'key':key,'value':fact.get('value'),'source':fact.get('provenance',{}).get('document_url')} for key,data in compiled.items() for fact in data.get('characteristics',[]) if fact.get('name')=='Гарантия качества товара']},ensure_ascii=False))
