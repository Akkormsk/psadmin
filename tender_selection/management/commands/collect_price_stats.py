import time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone

from tender_selection.stats import collect_price_stats

_MSK = ZoneInfo("Europe/Moscow")


class Command(BaseCommand):
    help = (
        "Собрать статистику снижения цен по завершённым закупкам. "
        "С --loop работает как фоновый сборщик (по умолчанию раз в сутки)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--since-days", type=int, default=30, help="За сколько дней брать свежие контракты")
        parser.add_argument("--catalog-requests", type=int, default=12,
                            help="Потолок запросов на шаг 1 — свежие контракты по категориям")
        parser.add_argument("--nmck-requests", type=int, default=48,
                            help="Потолок запросов на шаг 2 — добор начальной цены (по одной карточке закупки)")
        parser.add_argument("--loop", action="store_true", help="Крутиться в цикле")
        parser.add_argument("--interval", type=int, default=1440, help="Пауза между прогонами в цикле, минут (деф. сутки)")

    def _one(self, options):
        run = collect_price_stats(
            since_days=options["since_days"],
            catalog_requests=options["catalog_requests"],
            nmck_requests=options["nmck_requests"],
        )
        stamp = timezone.now().astimezone(_MSK).strftime("%d.%m %H:%M МСК")
        status = "OK" if run.ok else f"ОШИБКА: {run.error}"
        self.stdout.write(
            f"[{stamp}] статистика #{run.id}: запросов {run.requests_made}, контрактов {run.contracts_seen}, "
            f"новых {run.created_count}, добрано НМЦК {run.filled_count}, за {run.duration_seconds} с — {status}"
        )
        return run

    def handle(self, *args, **options):
        if not options["loop"]:
            self._one(options)
            return

        interval = max(30, options["interval"]) * 60
        self.stdout.write(self.style.SUCCESS(
            f"Сбор статистики запущен, интервал {options['interval']} мин. Ctrl+C — остановить."
        ))
        while True:
            try:
                self._one(options)
            except KeyboardInterrupt:
                self.stdout.write("\nОстановлено.")
                return
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"Прогон упал: {exc} — продолжаю через интервал.")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                self.stdout.write("\nОстановлено.")
                return
