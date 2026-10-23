"""Add synthetic records for anonymized tasks from conversations 95/96."""
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def prepare(source, destination):
    if destination.exists():
        raise ValueError("Use a new isolated database")
    shutil.copyfile(source, destination)
    os.environ["DB_ENGINE"] = "sqlite"
    os.environ["DJANGO_SETTINGS_MODULE"] = "mysite.settings"
    from django.conf import settings
    settings.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(destination)}}
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from cash.models import CashTransaction
    from tender_selection.models import Tender
    from tenders.models import TenderEstimate, TenderLine
    user = get_user_model().objects.get(username="m10")
    records = [
        ("2026-09-02", "cash", "expense", 100, "Тестовая бумага"),
        ("2026-09-03", "card", "expense", 75, "Тестовые папки"),
        ("2026-09-04", "cash", "income", 900, "Тестовая оплата"),
        ("2026-10-20", "card", "expense", 450, "Тестовые конверты"),
        ("2026-10-21", "cash", "expense", 1250, "Тестовые коробки"),
        ("2026-10-22", "cash", "income", 800, "Тестовое поступление"),
    ]
    for day, account, direction, amount, reason in records:
        item = CashTransaction.objects.create(operation_date=day, account=account,
            direction=direction, amount=amount, reason=reason, created_by=user)
        CashTransaction.objects.filter(pk=item.pk).update(created_at=datetime.fromisoformat(day).replace(hour=12,tzinfo=timezone.utc))
    tender = Tender.objects.create(pk=2355, purchase_number="M10-REAL-001",
        title="Тестовая поставка канцтоваров", status=Tender.PUSHED)
    estimate = TenderEstimate.objects.get(pk=201)
    estimate.tender = tender
    estimate.tender_number = tender.purchase_number
    estimate.name = "Тестовый расчёт канцтоваров"
    estimate.summary_snapshot = {"is_incomplete": False}
    estimate.save()
    TenderLine.objects.create(estimate=estimate, name="Бумага А4", quantity=5, nmck_unit=400)
    TenderLine.objects.create(estimate=estimate, name="Папка картонная", quantity=12, nmck_unit=70)
    assert estimate.display_status() == "Готово"
    assert CashTransaction.objects.count() == 6 and estimate.lines.count() == 2
    print(json.dumps({"database":str(destination),"cash_records":6,"september_expense":175,
        "tender_id":2355,"estimate_id":201,"estimate_status":"Готово","source":"synthetic only"},ensure_ascii=False))


if __name__ == "__main__":
    with patch("socket.socket.connect", side_effect=RuntimeError("Offline preparation")):
        prepare(Path(sys.argv[1]), Path(sys.argv[2]))
