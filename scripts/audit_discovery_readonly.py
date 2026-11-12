"""Small read-only collection report; compatible with the pre-upgrade runtime."""
import io
import json
import os
import time
from pathlib import Path
from datetime import timedelta
from django.core.management import call_command, get_commands
from django.db import connection
from django.db.models import Sum
from django.utils import timezone
from tender_selection.models import PullRun, FilterSettings, Tender

with connection.cursor() as cursor:
    cursor.execute("SET default_transaction_read_only = on")
if "audit_discovery_coverage" in get_commands():
    stream = io.StringIO()
    call_command("audit_discovery_coverage", hours=24, stdout=stream)
    report = json.loads(stream.getvalue())
    windows = report.pop("completed_windows", [])
    report["completed_window_count"] = len(windows)
else:
    rows = PullRun.objects.filter(started_at__gte=timezone.now()-timedelta(hours=24))
    report = {"legacy_pull_runs": rows.count(), "legacy_successful": rows.filter(ok=True).count(),
              "legacy_recorded_requests": rows.aggregate(total=Sum("requests_made"))["total"],
              "conclusion": "Новая телеметрия ещё не установлена; полное покрытие и число HTTP attempts не подтверждены."}
print(json.dumps({"checked_at": timezone.now().isoformat(), **report}, ensure_ascii=False, default=str))
print(json.dumps({"collection_safety": {
    "profile_triage_enabled": FilterSettings.objects.filter(pk=1).values_list("profile_triage_enabled", flat=True).first(),
    "autopull_enabled": os.getenv("TENDER_AUTOPULL_ENABLED"),
    "web_autopull_enabled": os.getenv("TENDER_AUTOPULL_WEB_ENABLED"),
    "calculation_v2_enabled": os.getenv("CALCULATION_V2_ENABLED"),
}}, ensure_ascii=False))
directory = os.getenv("TENDER_DISCOVERY_FEED_DIR")
verification = {"role": os.getenv("TENDER_DISCOVERY_ROLE"), "sample_present": False}
for marker in ("/app/.release-sha", "/app/.twin-version"):
    if Path(marker).is_file():
        verification[Path(marker).name] = Path(marker).read_text().strip()
if directory and Path(directory).is_dir():
    files = list(Path(directory).glob("*.json"))
    verification["feed_files"] = len(files)
    cutoff = time.time() - 2 * float(os.getenv("TENDER_MIRROR_INTERVAL_SECONDS", "30"))
    settled = missing = different = 0
    for path in files:
        if path.stat().st_mtime > cutoff:
            continue
        payload = json.loads(path.read_bytes())
        if path.stat().st_mtime > cutoff:
            continue
        record = payload["record"]
        local = Tender.objects.filter(law=payload["law"], purchase_number=record["purchase_number"]).values_list("raw", flat=True).first()
        settled += 1
        missing += int(local is None)
        different += int(local is not None and local != record)
    verification.update({"settled_feed_records": settled, "settled_missing": missing, "settled_different": different})
    if files:
        path = min(files, key=lambda item: item.stat().st_mtime_ns)
        payload = json.loads(path.read_bytes())
        record = payload["record"]
        local = Tender.objects.filter(law=payload["law"], purchase_number=record["purchase_number"]).values_list("raw", flat=True).first()
        verification["sample_present"] = local is not None
        verification["sample_matches"] = local == record
        verification["sample_key"] = path.stem
print(json.dumps({"delivery_verification": verification}, ensure_ascii=False))
if directory:
    receipt = Path(directory) / "requests.jsonl"
    if receipt.is_file():
        from collections import deque
        events = []
        for line in deque(receipt.open(), maxlen=20):
            try:
                event = json.loads(line)
                events.append({key: event.get(key) for key in ("at", "event", "status", "remaining", "retry_after", "reset", "limit", "age")})
            except ValueError:
                pass
        print(json.dumps({"rate_receipts_tail": events}))
