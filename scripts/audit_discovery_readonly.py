"""Small read-only collection report; compatible with the pre-upgrade runtime."""
import io
import json
import os
from pathlib import Path
from datetime import timedelta
from django.core.management import call_command, get_commands
from django.db import connection
from django.db.models import Sum
from django.db.models import Count
from django.apps import apps
from django.utils import timezone
from tender_selection.models import PullRun, FilterSettings, Tender

with connection.cursor() as cursor:
    cursor.execute("SET default_transaction_read_only = on")
try:
    History = apps.get_model("tender_selection", "TenderHistory")
    installed = True
except LookupError:
    History = apps.get_model("tender_selection", "IncomingTrace")
    installed = False
Stat = apps.get_model("tender_selection", "ContractStat")
identity = ["source", "law", "purchase_number"] if installed else ["law", "purchase_number"]
storage = {
    "schema_installed": installed, "working_cards": Tender.objects.count(),
    "history_rows": History.objects.count(),
    "duplicate_identity_groups": History.objects.values(*identity).annotate(n=Count("pk")).filter(n__gt=1).count(),
    "own_contract_stats": Stat.objects.filter(own_funnel=True).count(),
}
if installed:
    storage.update(working_cards_missing_history=Tender.objects.filter(history_record__isnull=True).count(),
                   own_stats_missing_history=Stat.objects.filter(own_funnel=True, history__isnull=True).count(),
                   unknown_source_rows=History.objects.filter(source="unknown").count())
    retained = {"incoming_traces": 0, "contract_stats": 0, "dismissal_feedback": 0}
    for payload in History.objects.values_list("legacy_data", flat=True).iterator(chunk_size=500):
        for key in retained:
            retained[key] += len(payload.get(key, []))
    storage["retained_source_rows"] = retained
print(json.dumps({"storage_history": storage}, ensure_ascii=False))
if hasattr(FilterSettings, "primary_filters"):
    from tender_selection.primary_search import build_primary_params, split_primary_requests
    from tender_selection.filtering import parse_terms
    from tender_selection.services import effective_laws
    selection = FilterSettings.objects.filter(pk=1).first()
    if selection:
        try:
            plans = {law: split_primary_requests(build_primary_params(selection, law, timezone.now()),
                                                selection.include_words, law) for law in effective_laws(selection)}
            groups = {law: len(queries) for law, queries in plans.items()}
            max_pattern = max((len(query.get("object_info", "")) for queries in plans.values() for query in queries), default=0)
            print(json.dumps({"primary_search_plan": {"valid": True,
                "keyword_entries": len(parse_terms(selection.include_words)), "or_groups_by_law": groups,
                "max_pattern_chars": max_pattern,
                "selected_classifiers": len(selection.okpd2_codes or []),
                "jobs": sum(groups.values()) * max(1, len(selection.okpd2_codes or [])),
                "role": os.getenv("TENDER_DISCOVERY_ROLE"), "upstream_called": False}}, ensure_ascii=False))
        except ValueError as exc:
            print(json.dumps({"primary_search_plan": {"valid": False, "error": str(exc), "upstream_called": False}}, ensure_ascii=False))
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
from urllib.parse import urlsplit
from tender_selection import gosplan
print(json.dumps({"collection_safety": {
    "discovery_role": os.getenv("TENDER_DISCOVERY_ROLE"),
    "gosplan_disabled": os.getenv("GOSPLAN_API_DISABLED"),
    "gosplan_host": urlsplit(gosplan.base_url()).hostname,
    "gosplan_key_configured": bool(gosplan.api_key()),
    "discovery_enrichment": os.getenv("TENDER_DISCOVERY_ENRICH_AFTER_PULL"),
    "quota_shared": bool(os.getenv("GOSPLAN_QUOTA_DIR")),
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
                events.append({key: event.get(key) for key in ("at", "event", "status", "remaining", "retry_after", "reset", "age", "limit", "client", "query_hash")})
            except ValueError:
                pass
        print(json.dumps({"rate_receipts_tail": events}))

if hasattr(FilterSettings, "primary_filters"):
    history = apps.get_model("tender_selection", "TenderHistory")
    latest_runs = list(PullRun.objects.order_by("-pk").values("started_at", "finished_at", "ok", "requests_made", "records_received", "created_count", "updated_count", "error", "params")[:1])
    for entry in latest_runs:
        params = entry.pop("params")
        entry["coverage"] = {key: params.get(key) for key in ("jobs_total", "jobs_completed", "complete", "filter_config_version")}
    print(json.dumps({"secondary_intake": {
        "decided": history.objects.filter(filter_snapshot__has_key="intake").count(),
        "working_without_decision": Tender.objects.exclude(history_record__filter_snapshot__has_key="intake").count(),
        "current_archived_reasons": list(history.objects.filter(status="dismissed").values("reason_code").annotate(n=Count("pk"))),
        "recent_runs": latest_runs,
    }}, ensure_ascii=False, default=str))
