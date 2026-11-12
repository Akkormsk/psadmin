"""Observational report only: no collection, AI, or changes to policy."""
import json
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from tender_selection.discovery_feed import feed_path
from tender_selection.models import PullRun, Tender


class Command(BaseCommand):
    help = "READ ONLY: фактические HTTP-запросы, завершённые окна и доставка общего потока"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=24)

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=options["hours"])
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            rows = list(PullRun.objects.filter(started_at__gte=since).values("ok", "finished_at", "params", "requests_made"))
            discovery = [r for r in rows if "jobs_total" in r["params"]]
            report = {"since": since.isoformat(), "measured_hours": options["hours"],
                      "discovery_runs": len(discovery),
                      "completed_runs": sum(r["ok"] and r["params"].get("complete") is True for r in discovery),
                      "incomplete_runs": sum(not r["ok"] or r["params"].get("complete") is not True for r in discovery),
                      "legacy_runs_without_coverage_evidence": sum("jobs_total" not in r["params"] and r["params"].get("mode") != "shared_discovery" for r in rows),
                      "http_attempts": None, "http_statuses": {}, "feed_records": 0,
                      "feed_missing_locally": 0, "feed_different_locally": 0,
                      "global_eis_total": None,
                      "conclusion": "Полное покрытие не доказано; отчёт не меняет настройки сбора."}
            directory = feed_path()
            if directory and directory.is_dir():
                receipt = directory / "requests.jsonl"
                if receipt.exists():
                    attempts = 0
                    statuses = Counter()
                    for line in receipt.open():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            report["malformed_http_receipts"] = report.get("malformed_http_receipts", 0) + 1
                            continue
                        if event["at"] < since.timestamp():
                            continue
                        if event["event"] == "request":
                            attempts += 1
                        elif event["event"] == "response":
                            statuses[str(event.get("status"))] += 1
                    report["http_attempts"] = attempts
                    report["http_statuses"] = dict(statuses)
                for path in directory.glob("*.json"):
                    payload = json.loads(path.read_bytes())
                    record = payload["record"]
                    local = Tender.objects.filter(law=payload["law"], purchase_number=record["purchase_number"]).values_list("raw", flat=True).first()
                    report["feed_records"] += 1
                    report["feed_missing_locally"] += int(local is None)
                    report["feed_different_locally"] += int(local is not None and local != record)
            report["completed_windows"] = [r["params"].get("windows") for r in discovery if r["ok"] and r["params"].get("complete")]
        self.stdout.write(json.dumps(report, ensure_ascii=False, default=str))
