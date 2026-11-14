"""Bounded primary-list probe; no ingestion, cursor updates, EIS or AI calls."""
import fcntl
import hashlib
import json
import os
import time
from collections import Counter
from unittest.mock import patch

from django.db import connection
from django.utils import timezone

from tender_selection import gosplan
from tender_selection.discovery_feed import feed_path
from tender_selection.filtering import parse_terms
from tender_selection.models import FilterSettings
from tender_selection.services import build_params, effective_laws, effective_okpd2


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main():
    with connection.cursor() as cursor:
        cursor.execute("SET default_transaction_read_only = on")
    settings = FilterSettings.objects.get(pk=1)
    snapshot = timezone.now().replace(microsecond=0)
    bases = {law: build_params(days=settings.window_days, stage=1,
             min_price=settings.min_price, regions=settings.regions, law=law, now=snapshot)
             for law in effective_laws(settings)}
    jobs = [("classifier", law, {**base, "classifier": [code]})
            for code in effective_okpd2(settings) for law, base in bases.items()]
    words = list(dict.fromkeys(entry[0] for entry in parse_terms(settings.include_words) if entry and entry[0]))
    keyword_base = build_params(days=settings.window_days, stage=1, min_price=settings.min_price,
                                regions=settings.regions, law="fz44", now=snapshot)
    jobs += [("keyword", "fz44", {**keyword_base, "object_info": word}) for word in words]
    emit("plan", snapshot=snapshot.isoformat(), days=settings.window_days,
         classifier_queries=sum(mode == "classifier" for mode, _, _ in jobs),
         keyword_queries=len(words), queries_total=len(jobs), rate_per_minute=9,
         database_read_only=True, card_writes=False, persistent_settings_changed=False)

    # The live collector uses this same lock. Never run a competing independent sweep.
    directory = feed_path()
    if directory is None:
        raise RuntimeError("Shared collector directory is required")
    with (directory / ".collector-lock").open("a+") as lock:
        waiting_until = time.monotonic() + float(os.getenv("PROBE_LOCK_WAIT_SECONDS", "300"))
        announced = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    emit("waiting_for_existing_collector")
                    announced = True
                if time.monotonic() >= waiting_until:
                    emit("not_started", reason="collector_busy", complete=False)
                    return
                time.sleep(5)
        run_probe(jobs, snapshot)


def run_probe(jobs, snapshot):
    started = time.monotonic()
    deadline = started + float(os.getenv("PROBE_MAX_SECONDS", "1200"))
    maximum_attempts = int(os.getenv("PROBE_MAX_ATTEMPTS", "150"))
    attempts = successes = received = completed = 0
    unique = set()
    errors = Counter()
    failed = []
    original_fetch = gosplan.fetch_page
    query_pages = []

    class BudgetEnded(gosplan.GosplanError):
        pass

    def fetch(params, law):
        nonlocal attempts, successes, received
        network_failures = 0
        while True:
            if attempts >= maximum_attempts or time.monotonic() >= deadline:
                raise BudgetEnded("probe_budget")
            attempts += 1
            try:
                page = original_fetch(params, law)
            except gosplan.RateLimitError:
                errors["HTTP429"] += 1
                emit("retry_same_page", query=current_query, skip=params["skip"], reason="HTTP429")
                # fetch_page records provider remaining/reset in the shared quota state.
                # Its next call waits there before sending the same request again.
                continue
            except gosplan.GosplanError as exc:
                errors[type(exc).__name__] += 1
                network_failures += 1
                if network_failures >= 3:
                    raise
                time.sleep(15)
                continue
            successes += 1
            received += len(page)
            date_keys = ("updated_after", "updated_before", "published_after", "published_before")
            query_pages.append({"skip": params["skip"], "got": len(page),
                                "window": {key: params[key] for key in date_keys if key in params}})
            emit("page", query=current_query, skip=params["skip"], got=len(page))
            return page

    # Process-local test pacing only. Existing reserve_request and provider cooldown remain active.
    with patch.dict(os.environ, {"GOSPLAN_REQUESTS_PER_MINUTE": "9"}), \
            patch.object(gosplan, "_fetch_with_retry", side_effect=fetch):
        for current_query, (mode, law, params) in enumerate(jobs, 1):
            query_pages = []
            count_before = len(unique)
            fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
            try:
                for record in gosplan.iter_purchases(params=params, law=law):
                    number = str(record.get("purchase_number") or "").strip()
                    if not number:
                        raise gosplan.GosplanError("missing_purchase_number")
                    unique.add((law, number))
            except gosplan.GosplanError as exc:
                reason = "probe_budget" if isinstance(exc, BudgetEnded) else type(exc).__name__
                failed.append({"query": current_query, "reason": reason})
                emit("query_incomplete", query=current_query, mode=mode, law=law, fingerprint=fingerprint,
                     reason=reason, pages=query_pages)
                if isinstance(exc, BudgetEnded):
                    break
                continue
            completed += 1
            emit("query_complete", query=current_query, mode=mode, law=law, fingerprint=fingerprint,
                 unique_added=len(unique) - count_before, pages=query_pages)
    emit("summary", snapshot=snapshot.isoformat(), duration_seconds=round(time.monotonic() - started, 1),
         queries_total=len(jobs), queries_completed=completed, incomplete=failed,
         queries_not_attempted=len(jobs) - completed - len(failed), attempts=attempts,
         successful_pages=successes, errors=dict(errors), cards_received=received,
         unique_cards=len(unique), complete=bool(jobs) and completed == len(jobs),
         scope="configured publication window and primary filters; not global EIS total",
         note="live source can change during pagination; no immutable snapshot guarantee")


if __name__ in {"__main__", "django.core.management.commands.shell"}:
    main()
