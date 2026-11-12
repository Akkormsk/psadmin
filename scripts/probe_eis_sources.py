"""Bounded read-only probes of real TEST tenders; never print tokens or archive URLs."""
import io
import json
import time
import zipfile
from xml.etree import ElementTree as ET
from django.db import connection
from django.utils import timezone
from tender_selection.models import Tender
from tender_selection import eis_docs, protocols

with connection.cursor() as cursor:
    cursor.execute("SET default_transaction_read_only = on")
print(json.dumps({"eis_token_configured": bool(eis_docs.token())}))
active = Tender.objects.filter(source="eis", law="fz44", collecting_finished_at__gt=timezone.now()).order_by("-last_pulled_at").first()
archived = Tender.objects.filter(source="eis", law="fz44").exclude(protocol={}).order_by("-last_pulled_at").first()
for category, tender in (("active", active), ("with_protocol", archived)):
    if not tender:
        print(json.dumps({"sample": category, "missing": True}))
        continue
    result = {"sample": category, "number": tender.purchase_number}
    started = time.monotonic()
    try:
        urls = eis_docs.fetch_archive_urls(tender.purchase_number, timeout=20)
        result["archive_count"] = len(urls)
        if urls:
            data = eis_docs.download_archive(urls[0], timeout=40)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                result["files"] = len(archive.infolist())
                tags = []
                for info in archive.infolist():
                    if info.filename.lower().endswith(".xml") and info.file_size < 10*1024*1024:
                        try:
                            root = ET.fromstring(archive.read(info))
                            tags.append(root.tag.rsplit("}", 1)[-1])
                        except ET.ParseError:
                            pass
                result["xml_root_types"] = sorted(set(tags))
    except Exception as exc:
        result["archive_error"] = type(exc).__name__
        cause = exc.__cause__
        if cause is not None:
            reason = getattr(cause, "reason", None)
            result["transport_cause"] = type(cause).__name__
            result["transport_reason"] = str(reason)[:250] if reason is not None else type(cause).__name__
        result["archive_error_text"] = str(exc)[:150] if isinstance(exc, eis_docs.EisDocsError) else "request failed"
    result["archive_seconds"] = round(time.monotonic()-started, 2)
    if category == "with_protocol":
        started = time.monotonic()
        try:
            protocol = protocols.fetch_protocol(tender.eis_url)
            result["protocol_found"] = bool(protocol)
            result["participants"] = len((protocol or {}).get("participants", []))
        except Exception as exc:
            result["protocol_error"] = type(exc).__name__
        result["protocol_seconds"] = round(time.monotonic()-started, 2)
    print(json.dumps(result, ensure_ascii=False), flush=True)
