"""Offline synthetic context-byte measurement; no provider calls or billing claims."""
import json, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
import django
django.setup()
from tenders.calculation_v2_pipeline import GatewayDocumentEnricher
from tenders.source_memory import SourceMemory, size, plan_packets
out=[]
for count in (1,90,300):
 blocks=[{'id':str(i),'version':'v','doc_id':1,'location':f'Строка {i}',
          'text':f'Товар {i}: '+ ('Полный исходный документ ' * 160) +f' Характеристика {i}: синий'} for i in range(count)]
 inventory=[{'source_id':i,'label':f'Товар {i}'} for i in range(count)]
 memory=SourceMemory(blocks,inventory,'fixture')
 for page,key,batch in memory.pending():
  memory.save([{'id':b['id'],'scope':'item','source_ids':[int(b['location'].split()[-1])],
               'facts':[{'name':'Цвет','quote':f"Характеристика {b['location'].split()[-1]}: синий"}]} for b in batch],batch,page,key)
 ids=list(range(count))
 engine=GatewayDocumentEnricher(max_cost_rub=10)
 engine.source_memory=memory
 engine.all_document_images=[]
 memory.source_map={1:{'name':'Fixture'}}
 def fits(batch):
  engine._memory_context({'requested_source_ids':batch})
  return len(engine.shared_context.encode()) + size(batch) + 4096 <= 12000
 batches=list(plan_packets(ids,fits))
 contexts=[]
 for batch in batches:
  engine._memory_context({'requested_source_ids':batch})
  contexts.append(engine.shared_context)
 out.append({'fixture_items':count,'original_bytes':size(blocks),'context_builds':len(contexts),
             'legacy_whole_source_bytes_if_repeated':size(blocks)*len(contexts),
             'addressed_context_bytes':sum(len(c.encode()) for c in contexts),
             'original_blocks_read':memory.snapshot()['read'],'original_blocks':memory.snapshot()['total'],
             'repeated_raw_blocks_after_reading':0,'paid_calls':0,'actual_tokens':None,'actual_cost_rub':None})
print(json.dumps({'method':'synthetic deterministic context construction; extraction records are fixtures, not model output','results':out},ensure_ascii=False,indent=2))
