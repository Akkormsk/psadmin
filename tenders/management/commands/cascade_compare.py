import json
import time

from django.core.management.base import BaseCommand, CommandError

from tenders.cascade import Cascade
from tenders.gateway_budget import spend_rub
from tenders.models import TenderLine

CONFIGS = {
    "baseline": {},
    "jev_ladder": {"6": {"engine": "jev_agent"}, "triage": {"engine": "jev", "no_below": 0.2}},
}


class Command(BaseCommand):
    """Живой прогон каскада по реальным позициям — база для сверки, не
    разовый скрипт. Пример: `manage.py cascade_compare 1809 1774 --out
    cascade_compare_result.json`. Тратит настоящий баланс шлюза AI —
    прежде чем запускать на большом пуле (сотни карточек), прикинь
    стоимость на малом (`--top` не помогает, каждая карточка пула
    оценивается целиком)."""

    help = "Реальный прогон каскада (без Джева / с малым+большим Джевом) по указанным TenderLine — для сравнения качества и стоимости."

    def add_arguments(self, parser):
        parser.add_argument("line_ids", nargs="+", type=int)
        parser.add_argument("--out", default="cascade_compare_result.json")
        parser.add_argument("--configs", nargs="+", choices=list(CONFIGS), default=list(CONFIGS))
        parser.add_argument("--max-cost-rub", type=float, default=0, help="Потолок расхода на один прогон (0 — без потолка).")
        parser.add_argument("--stop-if-balance-below", type=float, default=0, help="Прервать серию, если остаток на счёте упал ниже этого значения.")

    def handle(self, *args, **options):
        from tenders.gateway_budget import account_balance

        output = {}
        for line_id in options["line_ids"]:
            try:
                line = TenderLine.objects.get(pk=line_id)
            except TenderLine.DoesNotExist:
                raise CommandError(f"TenderLine {line_id} не найден.")
            payload = {
                "name": line.name, "quantity": str(line.quantity), "nmck_unit": str(line.nmck_unit),
                "requirements": line.requirements if isinstance(line.requirements, dict) else {},
            }
            output[str(line_id)] = {"name": line.name, "quantity": str(line.quantity), "runs": {}}
            for config_name in options["configs"]:
                if options["stop_if_balance_below"]:
                    balance, _ = account_balance(force=True)
                    if balance is not None and balance < options["stop_if_balance_below"]:
                        self.stdout.write(self.style.WARNING(f"Остаток {balance:.2f} ₽ ниже порога — останавливаюсь."))
                        with open(options["out"], "w", encoding="utf-8") as handle:
                            json.dump(output, handle, ensure_ascii=False, indent=2)
                        return
                self.stdout.write(f"{line_id} {line.name} · {config_name}…")
                output[str(line_id)]["runs"][config_name] = self._run_one(payload, CONFIGS[config_name], options["max_cost_rub"])

        with open(options["out"], "w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
        self.stdout.write(self.style.SUCCESS(f"Сохранено: {options['out']}"))

    @staticmethod
    def _run_one(line, step_settings, max_cost_rub=0):
        cascade = Cascade(line, top=10, step_settings=step_settings, max_cost_rub=max_cost_rub)
        started = time.perf_counter()
        result = cascade.run()
        elapsed = time.perf_counter() - started
        cost_rub = sum(
            spend_rub(usage, model) or 0 for model, usage in (result.usage_by_model or {}).items()
        )
        return {
            "elapsed_seconds": round(elapsed, 1),
            "candidates_shown": len(result.candidates),
            "candidates_exact": sum(1 for c in result.candidates if c.get("fit") == "exact"),
            "removed_by_triage": sum(1 for c in (result.removed or []) if "Джев-триаж" in (c.get("reason") or "")),
            "diagnostics": result.diagnostics,
            "usage_by_model": result.usage_by_model,
            "estimated_cost_rub": round(cost_rub, 3),
            "top3": [
                {"name": c.get("name", "")[:70], "fit": c.get("fit"), "match": c.get("match_count"),
                 "mismatch": c.get("mismatch_count"), "unknown": c.get("unknown_count"), "price": c.get("price")}
                for c in result.candidates[:3]
            ],
            "error": result.error,
        }
