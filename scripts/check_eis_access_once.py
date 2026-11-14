import json
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from django.db import connection
from tender_selection import eis_docs
from tender_selection.models import Tender

with connection.cursor() as cursor:
    cursor.execute('SET default_transaction_read_only = on')
tender = Tender.objects.filter(source='eis', law='fz44').order_by('-last_pulled_at').first()
result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'soap_requests': 0}
if tender is None or not eis_docs.token():
    raise SystemExit('Missing test purchase or runtime key; no request sent')
result['soap_requests'] = 1
try:
    root = ET.fromstring(eis_docs._soap_call(tender.purchase_number, timeout=20))
    errors = []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] == 'errorInfo':
            fields = {child.tag.rsplit('}', 1)[-1]: child.text or '' for child in node}
            errors.append({'code': fields.get('code'), 'message': fields.get('message', '').replace(eis_docs.token(), '[redacted]')[:400]})
    result['service_errors'] = errors
    result['archive_count'] = sum(1 for node in root.iter() if node.tag.rsplit('}', 1)[-1].lower() == 'archiveurl' and node.text)
    result['response_received'] = True
except Exception as exc:
    result['error'] = type(exc).__name__
    result['message'] = str(exc).replace(eis_docs.token(), '[redacted]')[:300]
print(json.dumps(result, ensure_ascii=False), flush=True)
