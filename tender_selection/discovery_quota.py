"""Shared request pacing and observational HTTP receipts for discovery only."""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from .discovery_feed import feed_path


def _directory():
    directory = feed_path() or Path("/tmp/psadmin-discovery-quota")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _receipt(directory, event):
    with (directory / "requests.jsonl").open("a") as stream:
        stream.write(json.dumps({"at": time.time(), **event}) + "\n")


def reserve_request(path, params):
    rate = float(os.getenv("GOSPLAN_REQUESTS_PER_MINUTE", "7"))
    if not 0 < rate <= 10:
        raise ValueError("GOSPLAN_REQUESTS_PER_MINUTE должен быть больше 0 и не больше бесплатного лимита 10")
    directory = _directory()
    with (directory / ".quota-lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = directory / ".last-request"
        previous = float(state.read_text()) if state.exists() else 0
        delay = previous + 60 / rate - time.time()
        if delay > 0:
            time.sleep(delay)
        state.write_text(str(time.time()))
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
        _receipt(directory, {"event": "request", "path": path, "query_hash": fingerprint,
                             "after": params.get("published_after"), "before": params.get("published_before")})


def record_response(status, headers):
    directory = _directory()
    with (directory / ".quota-lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _receipt(directory, {"event": "response", "status": status,
                             "remaining": headers.get("x-ratelimit-remaining-minute")})
