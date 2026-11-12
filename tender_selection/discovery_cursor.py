"""Advance an upstream update cursor only after complete, durable ingestion."""
import hashlib
import json

from .models import DiscoveryCursor


def prepare(law, params, days):
    identity = {key: value for key, value in params.items() if key not in {"published_after", "published_before"}}
    key = hashlib.sha256(json.dumps({"law": law, "days": days, "filter": identity}, sort_keys=True).encode()).hexdigest()
    cursor = DiscoveryCursor.objects.filter(query_key=key).first()
    if cursor:
        # Updates catch late arrivals with an older publication date as well.
        params = {**params, "updated_after": cursor.through_at.isoformat(), "updated_before": params["published_before"]}
    return key, params


def advance(key, through_at):
    cursor, created = DiscoveryCursor.objects.get_or_create(query_key=key, defaults={"through_at": through_at})
    if not created:
        DiscoveryCursor.objects.filter(pk=cursor.pk, through_at__lt=through_at).update(through_at=through_at)
