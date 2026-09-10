from django.core.management.base import BaseCommand

from tenders.gateway_budget import account_balance


class Command(BaseCommand):
    help = "Показать остаток на счёте Timeweb Cloud (₽) и параметры тарификации."

    def handle(self, *args, **options):
        balance, meta = account_balance(force=True)
        if balance is None:
            self.stdout.write(self.style.ERROR(
                "Не удалось получить баланс. Проверьте TIMEWEB_API_TOKEN в .env."
            ))
            return
        self.stdout.write(self.style.SUCCESS(f"Баланс: {balance:.2f} руб."))
        for key in ("hourly_cost", "monthly_cost", "hours_left", "autopay_card_info"):
            if key in meta:
                self.stdout.write(f"  {key}: {meta[key]}")
