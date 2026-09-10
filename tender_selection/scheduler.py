"""Фоновый сбор на хостинге без cron (Timeweb Cloud Apps).

Один поток внутри процесса приложения: раз в 30 минут — свежие тендеры,
каждые ~6 часов — статистика по контрактам. Включается только при
``TENDER_AUTOPULL_ENABLED=1``; под management-командами и в тестах не стартует.
На машине разработчика вместо этого запускают ``manage.py pull_tenders --loop``.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_PULL_EVERY_SECONDS = 30 * 60
_STATS_EVERY_TICKS = 12          # каждые 12 циклов ≈ 6 часов
_FIRST_RUN_DELAY_SECONDS = 60    # дать приложению подняться перед первым прогоном

_started = False
_SKIP_ARGV = {
    "migrate", "makemigrations", "collectstatic", "test", "shell",
    "createsuperuser", "makemessages", "compilemessages", "check", "dbshell",
}


def _run_once(tick: int) -> None:
    from django.db import close_old_connections

    from .services import run_pull
    from .stats import collect_price_stats

    close_old_connections()
    try:
        run_pull(max_requests=16)
    except Exception:
        logger.exception("autopull: run_pull failed")

    if tick % _STATS_EVERY_TICKS == 0:
        close_old_connections()
        try:
            collect_price_stats()
        except Exception:
            logger.exception("autopull: collect_price_stats failed")
    close_old_connections()


def _loop() -> None:
    time.sleep(_FIRST_RUN_DELAY_SECONDS)
    tick = 0
    while True:
        _run_once(tick)
        tick += 1
        time.sleep(_PULL_EVERY_SECONDS)


def start() -> None:
    global _started
    if _started or os.environ.get("TENDER_AUTOPULL_ENABLED") != "1":
        return
    if any(arg in _SKIP_ARGV for arg in sys.argv):
        return
    _started = True
    threading.Thread(target=_loop, name="tender-autopull", daemon=True).start()
    logger.info("tender autopull started — pull every %d min, stats every %d h",
                _PULL_EVERY_SECONDS // 60, _PULL_EVERY_SECONDS * _STATS_EVERY_TICKS // 3600)
