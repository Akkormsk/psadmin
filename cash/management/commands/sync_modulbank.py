"""Забирает входящие платежи из Модульбанка. Вешается на cron раз в 15-30 минут.

    python manage.py sync_modulbank                # синхронизация за последний месяц
    python manage.py sync_modulbank --days 7       # только за неделю
    python manage.py sync_modulbank --list-accounts  # показать счета и их id
"""

from django.core.management.base import BaseCommand

from cash import modulbank


class Command(BaseCommand):
    help = "Синхронизация входящих платежей с Модульбанком"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=modulbank.WINDOW_DAYS)
        parser.add_argument("--list-accounts", action="store_true", help="Показать компании и счета")

    def handle(self, *args, **options):
        try:
            if options["list_accounts"]:
                for company in modulbank.list_accounts():
                    self.stdout.write(self.style.MIGRATE_HEADING(company.get("companyName") or "—"))
                    for account in company.get("bankAccounts", []):
                        self.stdout.write(
                            f'  {account.get("number", "")}  {account.get("category", "")}  '
                            f'{account.get("currency", "")}  id={account.get("id", "")}'
                        )
                return
            touched = modulbank.sync(days=options["days"])
        except modulbank.ModulbankError as error:
            modulbank.record_sync_error(str(error))
            raise SystemExit(f"Модульбанк: {error}")
        self.stdout.write(self.style.SUCCESS(f"Готово. Платежей обработано: {touched}"))
