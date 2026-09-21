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
        run = run_pull(max_requests=16)
        logger.warning("autopull: pull — %d new, %d seen, %.0fs, ok=%s%s",
                       run.created_count, run.records_received, run.duration_seconds,
                       run.ok, f", err={run.error}" if run.error else "")
    except Exception:
        logger.exception("autopull: run_pull failed")

    if tick % _STATS_EVERY_TICKS == 0:
        close_old_connections()
        try:
            srun = collect_price_stats()
            logger.warning("autopull: stats — %d contracts, %d new, %d nmck, ok=%s",
                           srun.contracts_seen, srun.created_count, srun.filled_count, srun.ok)
        except Exception:
            logger.exception("autopull: collect_price_stats failed")

    close_old_connections()
    try:
        from .services import retry_pending_notifications
        attempted, succeeded = retry_pending_notifications()
        if attempted:
            logger.warning("autopull: notifications — %d/%d загружено", succeeded, attempted)
    except Exception:
        logger.exception("autopull: retry_pending_notifications failed")

    close_old_connections()
    try:
        from .services import retry_pending_documents
        attempted, succeeded = retry_pending_documents()
        if attempted:
            logger.warning("autopull: docs — %d/%d скачано (сеть до ЕИС нестабильна, остальное в следующий раз)",
                           succeeded, attempted)
    except Exception:
        logger.exception("autopull: retry_pending_documents failed")

    close_old_connections()
    try:
        from .services import retry_pending_outcomes
        attempted, succeeded = retry_pending_outcomes()
        if attempted:
            logger.warning("autopull: outcomes — %d/%d просчётов в «Торги» проверено", succeeded, attempted)
    except Exception:
        logger.exception("autopull: retry_pending_outcomes failed")

    close_old_connections()
    try:
        from .services import retry_pending_risks
        attempted, succeeded = retry_pending_risks()
        if attempted:
            logger.warning("autopull: риски — %d/%d тендеров на «Проверке» оценено", succeeded, attempted)
    except Exception:
        logger.exception("autopull: retry_pending_risks failed")
    close_old_connections()


def _loop() -> None:
    time.sleep(_FIRST_RUN_DELAY_SECONDS)
    tick = 0
    while True:
        _run_once(tick)
        tick += 1
        time.sleep(_PULL_EVERY_SECONDS)


def _network_probe_once() -> None:
    """Разовый сетевой зонд при старте — только для ручной диагностики (см. EIS_NETPROBE_ON_START).
    Никакого HTTP/логина не нужно, результат смотрим в логах приложения. Не трогает БД."""
    import socket

    targets = [
        ("v2test.gosplan.info", 443, "контроль — точно работает"),
        ("zakupki.gov.ru", 443, ""),
        ("int44.zakupki.gov.ru", 443, ""),
        ("www.gosuslugi.ru", 443, "контроль — другой gov.ru"),
    ]
    for host, port, note in targets:
        t0 = time.monotonic()
        try:
            conn = socket.create_connection((host, port), timeout=8)
            conn.close()
            logger.warning("netprobe: %s:%d OK %dms %s", host, port,
                           round((time.monotonic() - t0) * 1000), note)
        except Exception as exc:
            logger.warning("netprobe: %s:%d FAIL %dms %s: %s %s", host, port,
                           round((time.monotonic() - t0) * 1000), type(exc).__name__, exc, note)


def start() -> None:
    global _started
    if _started:
        return
    if any(arg in _SKIP_ARGV for arg in sys.argv):
        return

    if os.environ.get("EIS_NETPROBE_ON_START") == "1":
        _started = True
        threading.Thread(target=_network_probe_once, name="eis-netprobe", daemon=True).start()
        return  # разовый зонд — обычный автосбор в этом режиме не запускаем

    if os.environ.get("TENDER_AUTOPULL_ENABLED") != "1":
        return
    _started = True
    threading.Thread(target=_loop, name="tender-autopull", daemon=True).start()
    logger.warning("tender autopull started — pull every %d min, stats every %d h",
                   _PULL_EVERY_SECONDS // 60, _PULL_EVERY_SECONDS * _STATS_EVERY_TICKS // 3600)
