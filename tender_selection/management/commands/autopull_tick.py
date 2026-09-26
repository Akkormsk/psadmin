from django.core.management.base import BaseCommand

from tender_selection import scheduler


class Command(BaseCommand):
    help = "Один цикл автосбора для cron: закупки, извещения, документы, итоги, риски, уборка."

    def handle(self, *args, **options):
        # Price stats have their own cron entry (collect_price_stats), so pick a non-stats tick.
        scheduler._run_once(1)
