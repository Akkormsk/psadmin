"""Клиент к API Модульбанка — только чтение входящих платежей.

Токен с правами ``account-info`` + ``operation-history`` не может двигать деньги,
поэтому здесь принципиально нет ни одного метода записи в банк.

Документация: https://api.modulbank.ru/
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

API_ROOT = "https://api.modulbank.ru/v1"
MOSCOW = ZoneInfo("Europe/Moscow")

# Направление платежа в терминах Модульбанка.
INCOMING = "Debet"

# Окно, за которое показываем и синхронизируем платежи, — «последний месяц».
WINDOW_DAYS = 31


class ModulbankError(Exception):
    """Любая проблема при обращении к банку — сеть, авторизация, формат ответа."""


def _request(path, payload):
    token = (settings.MODULBANK_TOKEN or "").strip()
    if not token:
        raise ModulbankError("Не задан MODULBANK_TOKEN")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{API_ROOT}/{path}", data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    if settings.MODULBANK_SANDBOX:
        request.add_header("sandbox", "on")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise ModulbankError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ModulbankError(f"Сеть недоступна: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise ModulbankError("Банк вернул не JSON") from error


def list_accounts():
    """Компании пользователя и их счета — нужно один раз, чтобы взять accountId."""
    return _request("account-info", {})


def _to_decimal(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_moscow(value):
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timezone.is_aware(naive):
        return naive
    return naive.replace(tzinfo=MOSCOW)


def fetch_incoming(account_id, date_from, date_till):
    """Все входящие операции по счёту за период (с постраничной догрузкой)."""
    collected = []
    skip = 0
    while True:
        page = _request(
            f"operation-history/{account_id}",
            {
                "category": INCOMING,
                "from": date_from.isoformat(),
                "till": date_till.isoformat(),
                "records": 50,
                "skip": skip,
            },
        )
        if not isinstance(page, list) or not page:
            break
        collected.extend(page)
        if len(page) < 50:
            break
        skip += 50
    return collected


def upsert_operation(operation):
    """Сохранить одну транзакцию из банка. Возвращает (BankPayment, created) или None."""
    from .models import BankPayment

    if not isinstance(operation, dict) or not operation.get("id"):
        return None
    if operation.get("category") != INCOMING:
        return None

    executed_at = _parse_moscow(operation.get("executed") or operation.get("created"))
    operation_date = executed_at.astimezone(MOSCOW).date() if executed_at else timezone.localdate()

    payment, created = BankPayment.objects.update_or_create(
        external_id=operation["id"],
        defaults={
            "status": operation.get("status") or "",
            "direction": operation.get("category") or "",
            "amount": _to_decimal(operation.get("amount")),
            "currency": operation.get("currency") or "RUR",
            "counterparty_name": operation.get("contragentName") or "",
            "counterparty_inn": operation.get("contragentInn") or "",
            "counterparty_account": operation.get("contragentBankAccountNumber") or "",
            "counterparty_bank": operation.get("contragentBankName") or "",
            "payment_purpose": operation.get("paymentPurpose") or "",
            "doc_number": operation.get("docNumber") or "",
            "account_number": operation.get("bankAccountNumber") or "",
            "executed_at": executed_at,
            "operation_date": operation_date,
            "raw": operation,
        },
    )
    return payment, created


def _account_ids():
    raw = (settings.MODULBANK_ACCOUNT_ID or "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def sync(days=WINDOW_DAYS):
    """Забрать входящие платежи за последние ``days`` дней и разложить по строкам."""
    from .models import BankSyncState

    account_ids = _account_ids()
    if not account_ids:
        raise ModulbankError("Не задан MODULBANK_ACCOUNT_ID")

    till = timezone.localdate()
    since = till - timedelta(days=days)
    touched = 0
    for account_id in account_ids:
        for operation in fetch_incoming(account_id, since, till):
            if upsert_operation(operation):
                touched += 1

    state = BankSyncState.load()
    state.last_synced_at = timezone.now()
    state.last_status = f"Готово, платежей обработано: {touched}"
    state.save(update_fields=["last_synced_at", "last_status"])
    return touched


def record_sync_error(message):
    from .models import BankSyncState

    state = BankSyncState.load()
    state.last_synced_at = timezone.now()
    state.last_status = f"Ошибка: {message}"[:255]
    state.save(update_fields=["last_synced_at", "last_status"])


def verify_webhook(operation_id, provided_hash):
    """Подпись веб-хука для токена, сгенерированного в ЛК: SHA1(token[:10] + '&' + id)."""
    token = (settings.MODULBANK_TOKEN or "").strip()
    if not (token and operation_id and provided_hash):
        return False
    expected = hashlib.sha1(f"{token[:10]}&{operation_id}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, str(provided_hash).lower())
