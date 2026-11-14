import io
import json
import os
import time
import zipfile
from collections import Counter, deque
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import urlsplit
from django.db import connection
from tender_selection import eis_docs, eis_source
from tender_selection.models import Tender
from tender_selection.notification import parse_notification

with connection.cursor() as cursor:
    cursor.execute('SET default_transaction_read_only = on')
path = Path(os.environ['TENDER_DISCOVERY_FEED_DIR']) / 'requests.jsonl'
events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
times = sorted(e['at'] for e in events if e.get('event') == 'request' and e['at'] >= time.time()-86400)
window = deque()
maximum = 0
for at in times:
    while window and window[0] <= at-60:
        window.popleft()
    window.append(at)
    maximum = max(maximum, len(window))
print(json.dumps({'own_requests_last24h':len(times), 'max_own_requests_in_rolling60s':maximum,
                  'minimum_request_gap_seconds':round(min((b-a for a,b in zip(times,times[1:])), default=0), 4)}, ensure_ascii=False))
from urllib.request import getproxies
print(json.dumps({'proxy_protocols':sorted(getproxies().keys())}))
tender = Tender.objects.filter(law='fz44', source='eis').order_by('-last_pulled_at').first()
report = {'eis_soap_requests':1, 'archives_downloaded':0}
try:
    response = ET.fromstring(eis_docs._soap_call(tender.purchase_number, timeout=20))
    urls = [(node.text or '').strip() for node in response.iter() if node.tag.rsplit('}', 1)[-1] == 'archiveUrl' and node.text]
    report['archive_host'] = urlsplit(urls[0]).hostname
    data = eis_docs.download_archive(urls[0], timeout=25)
    report['archives_downloaded'] = 1
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        report['file_extensions'] = dict(Counter(Path(info.filename).suffix.lower() for info in archive.infolist() if not info.is_dir()))
    rows = eis_source.parse_package([data])
    report['structured_types'] = sorted({row['doc_type'] for row in rows})
    notices = [row for row in rows if 'notification' in row['doc_type'].lower()]
    report['notices'] = len(notices)
    if notices:
        card = parse_notification(notices[-1])
        report['items'] = len(card.get('items', []))
        report['document_links'] = len(card.get('documents', []))
except Exception as exc:
    report['error_type'] = type(exc).__name__
    cause = exc.__cause__
    if cause is not None:
        report['transport_cause'] = type(cause).__name__
        report['transport_reason'] = str(getattr(cause, 'reason', ''))[:200]
    if isinstance(exc, eis_docs.EisDocsError):
        report['error'] = str(exc).replace(eis_docs.token(), '[redacted]')[:300]
print(json.dumps(report,ensure_ascii=False),flush=True)
