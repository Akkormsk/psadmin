import json
from pathlib import Path

from django.core.management.base import BaseCommand

from tenders.integrations.fsprint import FSPrintError, calculate

SAMPLE_PAYLOAD_PATH = Path(__file__).resolve().parent.parent.parent / "integrations" / "fsprint_sample_payload.json"


class Command(BaseCommand):
    """Смоук-тест адаптера FSPrint: воспроизводит ИЗВЕСТНЫЙ реальный POST
    на calc.fsprint.ru (product_id=packet, живой пример из DevTools
    28.09.2026) без изменения значений, затем сам делает второй запрос
    (show_variant) и печатает разобранный результат — цены по срокам
    изготовления и построчные статьи себестоимости."""

    help = "Разовая проверка адаптера FSPrint на известном реальном payload."

    def handle(self, *args, **options):
        payload = json.loads(SAMPLE_PAYLOAD_PATH.read_text(encoding="utf-8"))
        self.stdout.write(f"POST {len(payload)} полей, product_id={payload.get('product_id')}…")
        try:
            result = calculate(payload)
        except FSPrintError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return
        if result.error:
            self.stdout.write(self.style.WARNING(result.error))
            self.stdout.write("--- сырой ответ ---")
            self.stdout.write(result.raw_text)
            return
        self.stdout.write(self.style.SUCCESS(f"Расчёт №{result.record}"))
        for option in result.timeline_options:
            self.stdout.write(f"  {option.label} (+{option.markup_percent}%): {option.total_cost} ₽, {option.price_per_unit} ₽/шт.")
        self.stdout.write(f"Статей себестоимости: {len(result.fields)}")
        for label, value in result.fields[:10]:
            self.stdout.write(f"  {label}: {value}")
