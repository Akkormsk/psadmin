"""Universal metadata-backed query engine; plans never contain SQL or code."""
from decimal import Decimal
from django.apps import apps
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum, Q

class QueryPlanError(Exception): pass

OPS={"eq","ne","contains","gte","gt","lte","lt","in","isnull"}; AGGS={"count":Count,"sum":Sum,"min":Min,"max":Max,"avg":Avg}; MAX_LIMIT=100
EXCLUDED={"admin","auth","contenttypes","sessions"}; SCALARS=(models.CharField,models.TextField,models.IntegerField,models.DecimalField,models.DateField,models.DateTimeField,models.BooleanField,models.ForeignKey)

def _entity(identifier):
    if not isinstance(identifier,str) or "." not in identifier: raise QueryPlanError("Недоступная сущность")
    app,name=identifier.split(".",1)
    if app in EXCLUDED: raise QueryPlanError("Недоступная сущность")
    try: model=apps.get_model(app,name)
    except LookupError as exc: raise QueryPlanError("Недоступная сущность") from exc
    fields={"id":model._meta.pk.attname}; searchable=[]
    for field in model._meta.fields:
        if isinstance(field,SCALARS):
            fields[field.name]=field.attname
            if isinstance(field,(models.CharField,models.TextField)): searchable.append(field.name)
    return model,f"{app}.{model.__name__}",fields,searchable

def catalogue_for(_user):
    return [{"id":f"{m._meta.app_label}.{m.__name__}","fields":sorted(_entity(f"{m._meta.app_label}.{m.__name__}")[2])} for m in apps.get_models() if m._meta.app_label not in EXCLUDED]

def _value(value): return str(value) if isinstance(value,Decimal) else value

def execute_plan(user,plan):
    if not user.is_superuser: raise QueryPlanError("Для универсального доступа к данным нужны права администратора")
    allowed={"entity","filters","search","fields","order_by","page","page_size","aggregates","group_by"}
    if not isinstance(plan,dict) or set(plan)-allowed: raise QueryPlanError("Некорректный план запроса")
    model,entity,fields,searchable=_entity(plan.get("entity")); requested=plan.get("fields") or list(fields)
    if not isinstance(requested,list) or not requested or any(f not in fields for f in requested): raise QueryPlanError("Запрошены недоступные поля")
    qs=model.objects.all(); search=plan.get("search")
    if search not in (None,""):
        if not isinstance(search,str) or not searchable: raise QueryPlanError("Некорректный поиск")
        condition=Q()
        for field in searchable: condition|=Q(**{f"{field}__icontains":search[:200]})
        qs=qs.filter(condition)
    for item in plan.get("filters",[]):
        if not isinstance(item,dict) or set(item)!={"field","op","value"} or item.get("field") not in fields or item.get("op") not in OPS: raise QueryPlanError("Некорректный фильтр")
        lookup=fields[item["field"]]; op=item["op"]
        qs=qs.filter(**{lookup+("__icontains" if op=="contains" else "" if op=="eq" else f"__{op}"):item["value"]})
    annotations={}
    for item in plan.get("aggregates",[]):
        if not isinstance(item,dict) or set(item)!={"op","field"} or item.get("op") not in AGGS or item.get("field") not in fields: raise QueryPlanError("Некорректная агрегация")
        annotations[f"{item['op']}_{item['field']}"]=AGGS[item["op"]](fields[item["field"]])
    groups=plan.get("group_by",[])
    if not isinstance(groups,list) or any(f not in fields for f in groups): raise QueryPlanError("Некорректная группировка")
    total=qs.count()
    if groups:
        rows=list(qs.values(*[fields[f] for f in groups]).annotate(**annotations).order_by(*[fields[f] for f in groups])[:MAX_LIMIT])
        records=[{**{f:_value(row.get(fields[f])) for f in groups},**{key:_value(row[key]) for key in annotations}} for row in rows]
        return {"entity":entity,"total_count":total,"returned_count":len(records),"aggregates":{},"records":records,"pagination":{"page":1,"page_size":MAX_LIMIT},"evidence":[]}
    values={key:_value(value) for key,value in qs.aggregate(**annotations).items()} if annotations else {}
    order=plan.get("order_by","-id"); desc=isinstance(order,str) and order.startswith("-"); key=order[1:] if desc else order
    if key not in fields: raise QueryPlanError("Недоступная сортировка")
    page,size=plan.get("page",1),plan.get("page_size",20)
    if not isinstance(page,int) or not isinstance(size,int) or page<1 or not 1<=size<=MAX_LIMIT: raise QueryPlanError("Некорректная пагинация")
    rows=list(qs.order_by(("-" if desc else "")+fields[key]).values(*[fields[f] for f in requested])[(page-1)*size:page*size]); records=[{f:_value(row.get(fields[f])) for f in requested} for row in rows]
    return {"entity":entity,"total_count":total,"returned_count":len(records),"aggregates":values,"records":records,"pagination":{"page":page,"page_size":size},"evidence":[{"entity":entity,"id":row["id"]} for row in records if row.get("id") is not None]}
