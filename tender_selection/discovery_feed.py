"""Public discovery records shared by the collector and isolated TEST database."""
import hashlib
import json
import os
import tempfile
import fcntl
from functools import wraps
import threading
import logging
from pathlib import Path

from django.db import transaction
from django.utils import timezone

_background_lock = threading.Lock()
_seen_versions = {}


def start_discovery_pull():
    """A full discovery pass must not occupy a web request until gunicorn kills it."""
    if not _background_lock.acquire(blocking=False):
        return False
    def collect():
        from django.db import close_old_connections
        from .services import run_pull, run_keyword_pull
        close_old_connections()
        try:
            run_pull()
            run_keyword_pull()
        except Exception:
            logging.getLogger(__name__).exception("Discovery background collection failed")
        finally:
            close_old_connections()
            _background_lock.release()
    threading.Thread(target=collect, daemon=True).start()
    return True


def feed_path():
    value = os.getenv("TENDER_DISCOVERY_FEED_DIR", "").strip()
    return Path(value) if value else None


def publish(record, law):
    """Write before local DB ingestion, so a DB failure can be replayed."""
    directory = feed_path()
    if os.getenv("TENDER_DISCOVERY_ROLE") != "collector":
        return
    if directory is None:
        raise ValueError("Для collector требуется TENDER_DISCOVERY_FEED_DIR")
    number = str(record.get("purchase_number") or "").strip()
    if law not in {"fz44", "fz223"} or not number.isdecimal():
        raise ValueError("Invalid discovery identifier")
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"law": law, "record": record}, ensure_ascii=False, sort_keys=True).encode()
    target = directory / (hashlib.sha256(f"{law}:{number}".encode()).hexdigest() + ".json")
    if target.exists() and target.read_bytes() == body:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = stream.name
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def discovery_run(function):
    """One collector at a time; mirror cannot accidentally query upstream."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        from .models import PullRun
        role = os.getenv("TENDER_DISCOVERY_ROLE", "standalone")
        if role == "mirror":
            if function.__name__ == "run_pull":
                return import_feed()
            return PullRun.objects.create(started_at=timezone.now(), finished_at=timezone.now(), ok=True,
                                          params={"mode": "shared_discovery", "disabled": True})
        directory = feed_path()
        if role == "collector" and directory is None:
            raise ValueError("Для collector требуется TENDER_DISCOVERY_FEED_DIR")
        if directory is None:
            return function(*args, **kwargs)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".collector-lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return PullRun.objects.create(started_at=timezone.now(), finished_at=timezone.now(), ok=False,
                                              params={"mode": "discovery_busy"}, error="Предыдущий сбор ещё выполняется")
            return function(*args, **kwargs)
    return wrapped


def import_feed():
    """Only upstream card fields are copied; all local decisions stay local."""
    from .models import PullRun, Tender
    from .services import _record_to_fields

    run = PullRun.objects.create(started_at=timezone.now(), params={"mode": "shared_discovery", "source": "eis"})
    directory = feed_path()
    try:
        if directory is None or not directory.is_dir():
            raise ValueError("Общий поток закупок ещё не подключён или недоступен")
        for path in sorted(directory.glob("*.json")):
            metadata = path.stat()
            if path.is_symlink() or metadata.st_size > 2 * 1024 * 1024:
                raise ValueError("Некорректная запись общего потока")
            version = (metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
            run.records_received += 1
            if _seen_versions.get(str(path)) == version:
                continue
            payload = json.loads(path.read_bytes())
            law, record = payload["law"], payload["record"]
            number = str(record.get("purchase_number") or "").strip()
            if law not in {"fz44", "fz223"} or not number.isdecimal():
                raise ValueError("Некорректный идентификатор общего потока")
            existing = Tender.objects.filter(law=law, purchase_number=number).first()
            if existing and existing.raw == record:
                _seen_versions[str(path)] = version
                continue
            with transaction.atomic():
                _, created = Tender.objects.update_or_create(
                    law=law, purchase_number=number,
                    defaults={**_record_to_fields(record, timezone.now(), law), "source": Tender.EIS},
                )
            run.created_count += int(created)
            run.updated_count += int(not created)
            _seen_versions[str(path)] = version
        run.ok = True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        run.error = str(exc)[:500]
    run.finished_at = timezone.now()
    run.duration_seconds = (run.finished_at - run.started_at).total_seconds()
    run.save()
    return run
