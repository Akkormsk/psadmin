"""Подстраховка веб-хука на хостинге без cron (Timeweb Cloud Apps).

Один поток внутри процесса приложения: раз в 20 минут дозабирает входящие
платежи из Модульбанка на случай, если веб-хук не дошёл. Первый прогон через
минуту после старта — он же первичная выгрузка за последний месяц.
Включается только при ``MODULBANK_AUTOSYNC_ENABLED=1``; под management-командами
и в тестах не стартует. Локально вместо этого — ``manage.py sync_modulbank``.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_EVERY_SECONDS = 20 * 60
_FIRST_RUN_DELAY_SECONDS = 60

_started = False
_SKIP_ARGV = {
    "migrate", "makemigrations", "collectstatic", "test", "shell",
    "createsuperuser", "makemessages", "compilemessages", "check", "dbshell",
    "sync_modulbank",
}


def _run_once() -> None:
    from django.db import close_old_connections

    from . import modulbank

    close_old_connections()
    try:
        count = modulbank.sync()
        logger.warning("modulbank autosync: обработано платежей %d", count)
    except modulbank.ModulbankError as error:
        modulbank.record_sync_error(str(error))
        logger.warning("modulbank autosync: %s", error)
    except Exception:
        logger.exception("modulbank autosync: непредвиденная ошибка")
    close_old_connections()


def _loop() -> None:
    time.sleep(_FIRST_RUN_DELAY_SECONDS)
    while True:
        _run_once()
        time.sleep(_EVERY_SECONDS)


def start() -> None:
    global _started
    if _started or os.environ.get("MODULBANK_AUTOSYNC_ENABLED") != "1":
        return
    if any(arg in _SKIP_ARGV for arg in sys.argv):
        return
    _started = True
    threading.Thread(target=_loop, name="modulbank-autosync", daemon=True).start()
    logger.warning("modulbank autosync started — every %d min", _EVERY_SECONDS // 60)
