import os, json, collections
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
import django
django.setup()
from django.db import connection
from tenders.models import TenderComputeJob, CalculationComponent
from tenders.calculation_v2_pipeline import GatewayBatchRouter, _route_owner_answer_map
if connection.settings_dict.get('HOST') != 'test-db': raise RuntimeError('TEST only')
job = TenderComputeJob.objects.select_related('tender').get(pk=134)
for name, stage in job.diagnostics.get('stages', {}).items():
 for row in stage.get('result') or []:
  if isinstance(row,dict) and row.get('key') == '382:0': print('SOURCE_RECORD', name, json.dumps(row,ensure_ascii=False,default=str))
items = list(CalculationComponent.objects.filter(commercial_item__tender=job.tender,status='active').select_related('source_item','commercial_item'))
answers = _route_owner_answer_map([i.source_item for i in items])
groups=collections.Counter()
for i in items:
 requirements={**i.requirements,'owner_answers':answers[i.source_item_id]}
 key=json.dumps([i.name,str(i.effective_quantity),i.unit,GatewayBatchRouter._requirements_payload(requirements)],ensure_ascii=False,sort_keys=True,default=str)
 groups[key]+=1
print('ROUTE_GROUPS',json.dumps({'total':len(items),'distinct':len(groups),'duplicate_sizes':sorted([v for v in groups.values() if v>1],reverse=True)},ensure_ascii=False))
from unittest.mock import patch
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher, assess_quality, document_analysis_sources
sources=list(document_analysis_sources(job.tender)[0].values())
engine=GatewayDocumentEnricher(max_cost_rub=0)
with patch('tender_selection.services.documents_html'):
 engine.prepare_stages(job.tender,sources,assess_quality(job.tender,sources)['documents'])
goods=engine.reconcile_structure(job.diagnostics['stages']['structure']['result'])
compiled=engine.reconcile_specifications(job.diagnostics['stages']['tz']['result'],goods)
print('REPLAY_FACT',json.dumps({'baseline':engine.baselines.get(382),'item':compiled.get('382:0'),'gaps':[g for g in engine.specification_gaps if g.get('key')=='382:0']},ensure_ascii=False,default=str))
