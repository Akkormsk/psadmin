"""Small read-only collection report; compatible with the pre-upgrade runtime."""
import io
import json
from datetime import timedelta
from django.core.management import call_command, get_commands
from django.db import connection
from django.db.models import Sum
from django.utils import timezone
from tender_selection.models import PullRun

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
