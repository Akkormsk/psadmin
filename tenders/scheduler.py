"""Фоновая синхронизация каталога Oasis на хостинге без cron (Timeweb Cloud Apps).

Один поток внутри процесса приложения: раз в 6 часов запускает полную
синхронизацию каталога Oasis (товары, категории, остатки) как ОТДЕЛЬНЫЙ
процесс ОС (``manage.py sync_oasis_catalog``), по образцу ручного полного
ресинка Gifts (``tenders.views.gifts_import_test``) — не вызывает
``sync_oasis_catalog()`` напрямую в своём потоке.

Так и должно быть: тариф хоста — 1 CPU / 1 ГБ на один gunicorn-воркер, а
полная синхронизация Oasis — это ~37 тыс. товаров, постраничная выгрузка и
`bulk_create`, вперемешку с двумя другими фоновыми синхронизациями в том же
процессе. 14.09.2026 именно так и уронило прод: поток внутри воркера съел
всю память, воркер получил SIGKILL от OOM-killer'а, сайт не отвечал.
Отдельный процесс ОС не делит память/GIL с воркером, который обслуживает
сайт — если синхронизация сама упрётся в память, погибнет только она.

Прогон синхронный внутри потока (ждём завершения дочернего процесса, не
плодим параллельные попытки), с жёстким потолком по времени
``_RUN_TIMEOUT_SECONDS`` на случай зависания. Включается только при
``OASIS_AUTOSYNC_ENABLED=1``; под management-командами и в тестах не
стартует. Локально вместо этого — ``manage.py sync_oasis_catalog``; на
проде без ожидания следующего тика — GET ``/tenders/catalog/oasis/import-test/``
(тот же admin-эндпоинт с Bearer-токеном, что и у Gifts).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_EVERY_SECONDS = 6 * 60 * 60
_FIRST_RUN_DELAY_SECONDS = 60
_RUN_TIMEOUT_SECONDS = 30 * 60  # локальный прогон занял ~11 мин на 37 тыс. товаров

_started = False
_SKIP_ARGV = {
    "migrate", "makemigrations", "collectstatic", "test", "shell",
    "createsuperuser", "makemessages", "compilemessages", "check", "dbshell",
    "sync_oasis_catalog",
}


def _run_once() -> None:
    manage_path = Path(__file__).resolve().parent.parent / "manage.py"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(manage_path), "sync_oasis_catalog"],
            cwd=str(manage_path.parent), timeout=_RUN_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        seconds = time.monotonic() - started
        if result.returncode == 0:
            logger.warning("oasis autosync: подпроцесс завершён успешно за %.0f с", seconds)
        else:
            logger.warning("oasis autosync: подпроцесс завершился с кодом %d за %.0f с",
                           result.returncode, seconds)
    except subprocess.TimeoutExpired:
        logger.warning("oasis autosync: подпроцесс не уложился в %d с, прерван",
                       _RUN_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("oasis autosync: не удалось запустить подпроцесс")


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
    logger.warning("oasis autosync started — every %d h, subprocess timeout %d min",
                   _EVERY_SECONDS // 3600, _RUN_TIMEOUT_SECONDS // 60)
