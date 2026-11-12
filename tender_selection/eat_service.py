"""EAT source adapter for the existing Tender + PullRun domain.

EAT list processing is asynchronous, so manual update starts this independently
of the Gosplan pull and uses the same site-wide selection settings.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from django.db import close_old_connections, transaction
from django.utils import timezone as dj_timezone

from . import eat_api
from .filtering import parse_terms
from .models import FilterSettings, PullRun, Tender

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_RUNNING = False


def run_eat_pull(*, days=None, detail_limit=200):
    from .services import effective_sources, effective_laws
    selection = FilterSettings.load()
    if Tender.EAT not in effective_sources(selection):
        return None
    days = int(days or selection.window_days)
    started = dj_timezone.now()
    run = PullRun.objects.create(
        started_at=started,
        params={"source": "eat", "days": days, "min_price": str(selection.min_price)},
    )
    stats = {"total": 0, "shortlisted": 0, "checked": 0, "law_unknown": 0, "created": 0,
             "updated": 0, "skipped": 0, "mode": "", "partial": False}
    try:
        now = dj_timezone.now()
        orders, mode = eat_api.listing(days=days, now=now)
        stats["total"] = len(orders)
        stats["mode"] = mode
        stats["partial"] = mode != "published"
        candidates = eat_api.shortlist(
            orders, minimum=selection.min_price, include=[], exclude=[],
            now=now, days=days, mode=mode,
        )
        stats["shortlisted"] = len(candidates)
        # No false '44-FZ' classification from the list endpoint.
        # Limit detail requests per press, but do not mark the pull complete
        # if there is more to check than the bounded request budget.
        if len(candidates) > detail_limit:
            stats["partial"] = True
        for item in candidates[:detail_limit]:
            detail = eat_api.notification(item["purchase_number"])
            stats["checked"] += 1
            law = eat_api.legal_basis(detail)
            if law not in effective_laws(selection):
                stats["law_unknown"] += 1
                continue
            fields = eat_api.notification_fields(detail)
            normalized = eat_api.normalized_notification(detail)
            normalized["card"]["title"] = item["title"]
            url = fields["WWWReference"]
            if not url.startswith("https://agregatoreat.ru/"):
                url = ""
            normalized["card"]["eis_href"] = url
            number = item["purchase_number"]
            attrs = {
                "title": item["title"],
                "object_info": item["title"],
                "max_price": item["max_price"],
                "currency_code": "RUB",
                "collecting_finished_at": item["collecting_finished_at"],
                "published_at": item["published_at"],
                "source": Tender.EAT,
                "eis_url": url,  # Legacy link field; UI labels it by platform.
                "raw": {"eat": fields, "source": "eat", "law_verified": True,
                        "session_start": item["session_start"].isoformat() if item.get("session_start") else None},
                "last_pulled_at": started,
                "first_seen_at": started,
                "notification_raw": normalized,
                "notification_checked_at": started,
                "notification_error": "" if normalized["card"]["items"] else "ЕАТ не вернул перечень товаров; полнота не подтверждена.",
                "customer_inn": normalized["card"]["customer"]["inn"],
                "okpd2": sorted({row["code"] for row in normalized["card"]["items"] if row.get("code")}),
                "purchase_type": fields["PurchaseMethod"],
            }
            with transaction.atomic():
                existing = Tender.objects.select_for_update().filter(
                    law=law, purchase_number=number,
                ).first()
                if existing and existing.source != Tender.EAT:
                    stats["skipped"] += 1
                    continue
                if existing:
                    for key in ("customer_inn", "eis_url", "purchase_type"):
                        if not attrs[key] and getattr(existing, key):
                            attrs.pop(key)
                    previous_items = (existing.notification_raw.get("card") or {}).get("items", [])
                    if previous_items and not normalized["card"]["items"]:
                        attrs.pop("notification_raw")
                        attrs.pop("notification_checked_at")
                        attrs.pop("okpd2")
                    # Preserve user decisions/flags and initial discovery timestamp.
                    attrs.pop("first_seen_at", None)
                    for key, value in attrs.items():
                        setattr(existing, key, value)
                    existing.save(update_fields=list(attrs))
                    stats["updated"] += 1
                else:
                    Tender.objects.create(law=law, purchase_number=number, **attrs)
                    stats["created"] += 1
        run.ok = True
    except eat_api.EatError as exc:
        run.error = str(exc)[:400]
        stats["partial"] = True
        run.ok = False
    except Exception:
        log.exception("EAT import crashed (response and secrets suppressed)")
        run.error = "EAT import failed unexpectedly; inspect server logs"
        stats["partial"] = True
        run.ok = False
    finally:
        run.finished_at = dj_timezone.now()
        run.requests_made = stats["checked"] + 1
        run.records_received = stats["total"]
        run.created_count = stats["created"]
        run.updated_count = stats["updated"]
        run.duration_seconds = (run.finished_at - started).total_seconds()
        run.params = {**run.params, "diagnostics": stats}
        run.save()
    return run


def start_eat_pull():
    """Enqueue one in-process background job; never block the site's update button.

    The run is idempotent. A worker restart can interrupt it; subsequent
    explicit updates retry safely using the unique tender identifier.
    """
    global _RUNNING
    from .services import effective_sources
    if Tender.EAT not in effective_sources(FilterSettings.load()):
        return "disabled"
    if os.getenv("EAT_PULL_ENABLED") != "1":
        return "disabled"
    if not os.getenv("EAT_API_TOKEN") or not os.getenv("EAT_EXT_SYSTEM"):
        return "not_configured"
    with _LOCK:
        if _RUNNING:
            return "already_running"
        _RUNNING = True

    def work():
        global _RUNNING
        close_old_connections()
        try:
            run_eat_pull()
        finally:
            close_old_connections()
            with _LOCK:
                _RUNNING = False

    threading.Thread(target=work, name="eat-readonly-pull", daemon=True).start()
    return "started"
