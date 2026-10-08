from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from tenders.preparation import requeue_stale_preparations, run_next_preparation, sweep_preparation


class Command(BaseCommand):
    help = "Plan or execute bounded Calculation V2 preparation work. Dry-run is the default."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--run", type=int, default=0, help="Claim and run at most this many queued preparations")
        parser.add_argument("--lifecycle", choices=["all", "incoming", "working"], default="all")
        parser.add_argument("--max-ai-rub", type=float)

    def handle(self, *args, **options):
        if options["limit"] < 1 or options["run"] < 0:
            raise CommandError("--limit must be positive and --run must not be negative")
        if (options["apply"] or options["run"]) and not settings.CALCULATION_V2_PREPARATION_ENABLED:
            raise CommandError("CALCULATION_V2_PREPARATION_ENABLED is false")
        budget = options["max_ai_rub"]
        if budget is not None and budget < 0:
            raise CommandError("--max-ai-rub must not be negative")
        with override_settings(CALCULATION_V2_PREPARATION_MAX_AI_RUB=budget) if budget is not None else _NullContext():
            result = sweep_preparation(limit=options["limit"], dry_run=not options["apply"], lifecycle=options["lifecycle"])
            result["requeued_stale"] = requeue_stale_preparations() if options["apply"] else 0
            result["ran"] = 0
            for _ in range(options["run"]):
                if run_next_preparation() is None:
                    break
                result["ran"] += 1
        self.stdout.write(self.style.SUCCESS(str(result)))


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
