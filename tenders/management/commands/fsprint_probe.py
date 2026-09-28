import json
from pathlib import Path

from django.core.management.base import BaseCommand

from tenders.integrations.fsprint import FSPrintError, calculate

SAMPLE_PAYLOAD_PATH = Path(__file__).resolve().parent.parent.parent / "integrations" / "fsprint_sample_payload.json"


class Command(BaseCommand):
    """Смоук-тест адаптера FSPrint: воспроизводит ИЗВЕСТНЫЙ реальный POST
    на calc.fsprint.ru (product_id=packet, живой пример из DevTools
    28.09.2026) без изменения значений — цель первого прогона только
    доказать, что можно повторить тот же расчёт server-to-server, без
    браузерных cookies. Печатает сырой ответ целиком для изучения
    структуры вручную — ничего из него ещё не разбирается."""

    help = "Разовая проверка адаптера FSPrint на известном реальном payload."

    def handle(self, *args, **options):
        payload = json.loads(SAMPLE_PAYLOAD_PATH.read_text(encoding="utf-8"))
        self.stdout.write(f"POST {len(payload)} полей, product_id={payload.get('product_id')}…")
        try:
            result = calculate(payload)
        except FSPrintError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return
        self.stdout.write(self.style.SUCCESS("Ответ получен."))
        self.stdout.write(f"Похоже на JSON: {result.raw_json is not None}")
        if result.error:
            self.stdout.write(self.style.WARNING(result.error))
        self.stdout.write("--- сырой ответ ---")
        self.stdout.write(result.raw_text)
