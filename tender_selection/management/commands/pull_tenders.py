import time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand
from django.utils import timezone

from tender_selection.services import enrich_organizations, run_pull
from tender_selection.stats import collect_price_stats

_MSK = ZoneInfo("Europe/Moscow")


class Command(BaseCommand):
    help = "Выгрузить закупки из ГосПлан API по настройкам подбора. С --loop работает как автообновление."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Окно по дате публикации, дней (деф. из настроек)")
        parser.add_argument("--min-price", type=float, default=None, help="НМЦК не меньше указанной (деф. из настроек)")
        parser.add_argument("--okpd2", default="", help="Свой список кодов ОКПД2 через запятую (переопределяет настройки)")
        parser.add_argument("--max-requests", type=int, default=25, help="Потолок числа запросов к API за прогон")
        parser.add_argument("--loop", action="store_true", help="Крутиться в цикле — автообновление")
        parser.add_argument("--interval", type=int, default=30, help="Пауза между прогонами в цикле, минут (деф. 30)")
        parser.add_argument("--enrich", action="store_true", help="Заодно добирать имена заказчиков (по 8 за прогон)")
        parser.add_argument("--stats-every", type=int, default=0,
                            help="Каждый N-й прогон дособирать статистику цен (0 — не собирать). Для --loop.")
        parser.add_argument("--targeted", action="store_true", help="Не используется (оставлено для совместимости)")

    def _one_pull(self, options):
        classifiers = [c.strip() for c in options["okpd2"].split(",") if c.strip()] or None
        run = run_pull(
            days=options["days"],
            min_price=options["min_price"],
            classifiers=classifiers,
            max_requests=options["max_requests"],
        )
        stamp = timezone.now().astimezone(_MSK).strftime("%d.%m %H:%M МСК")
        self.stdout.write(
            f"[{stamp}] выгрузка #{run.id}: запросов {run.requests_made}, записей {run.records_received}, "
            f"новых {run.created_count}, за {run.duration_seconds} с — {'OK' if run.ok else 'ОШИБКА: ' + run.error}"
        )
        if options["enrich"]:
            try:
                saved = enrich_organizations(limit=8)
                if saved:
                    self.stdout.write(f"           имён заказчиков добрано: {saved}")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"           enrich: {exc}")
        return run

    def _maybe_stats(self, options, iteration):
        every = options["stats_every"]
        if not every or iteration % every != 0:
            return
        try:
            run = collect_price_stats()
            stamp = timezone.now().astimezone(_MSK).strftime("%d.%m %H:%M МСК")
            self.stdout.write(
                f"[{stamp}] статистика: запросов {run.requests_made}, контрактов {run.contracts_seen}, "
                f"новых {run.created_count}, добрано НМЦК {run.filled_count} — {'OK' if run.ok else run.error}"
            )
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"           статистика: {exc}")

    def handle(self, *args, **options):
        if not options["loop"]:
            self._one_pull(options)
            self._maybe_stats(options, iteration=1)
            return

        interval = max(5, options["interval"]) * 60
        self.stdout.write(self.style.SUCCESS(
            f"Автообновление запущено, интервал {options['interval']} мин. Ctrl+C — остановить."
        ))
        iteration = 0
        while True:
            iteration += 1
            try:
                self._one_pull(options)
                self._maybe_stats(options, iteration)
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
