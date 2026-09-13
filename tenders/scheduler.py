"""Фоновая синхронизация каталога Oasis на хостинге без cron (Timeweb Cloud Apps).

Один поток внутри процесса приложения: раз в 6 часов — полная синхронизация
каталога Oasis (товары, категории, остатки), в конце которой
``sync_oasis_catalog`` сама пересобирает ``family_key``/``variant_axes``
(``rebuild_catalog_families``) — без этого потока на проде обе операции не
запускались никогда. Включается только при ``OASIS_AUTOSYNC_ENABLED=1``; под
management-командами и в тестах не стартует. Локально вместо этого —
``manage.py sync_oasis_catalog``.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_EVERY_SECONDS = 6 * 60 * 60
_FIRST_RUN_DELAY_SECONDS = 60

_started = False
_SKIP_ARGV = {
    "migrate", "makemigrations", "collectstatic", "test", "shell",
    "createsuperuser", "makemessages", "compilemessages", "check", "dbshell",
    "sync_oasis_catalog",
}


def _run_once() -> None:
    from django.db import close_old_connections

    from .catalog import CatalogSyncError, sync_oasis_catalog

    close_old_connections()
    try:
        run = sync_oasis_catalog()
        logger.warning(
            "oasis autosync: получено %d, создано %d, обновлено %d, отключено %d",
            run.received_count, run.created_count, run.updated_count, run.deactivated_count,
        )
    except CatalogSyncError as error:
        logger.warning("oasis autosync: %s", error)
    except Exception:
        logger.exception("oasis autosync: непредвиденная ошибка")
    close_old_connections()


def _loop() -> None:
    time.sleep(_FIRST_RUN_DELAY_SECONDS)
    while True:
        _run_once()
        time.sleep(_EVERY_SECONDS)


def start() -> None:
    global _started
    if _started or os.environ.get("OASIS_AUTOSYNC_ENABLED") != "1":
        return
    if any(arg in _SKIP_ARGV for arg in sys.argv):
        return
    _started = True
    threading.Thread(target=_loop, name="oasis-autosync", daemon=True).start()
    logger.warning("oasis autosync started — every %d h", _EVERY_SECONDS // 3600)
