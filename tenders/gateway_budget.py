"""Контроль расхода AI Gateway по конкретным цифрам, а не на глаз.

- `account_balance()` — остаток на счёте Timeweb Cloud (₽), кэш 2 минуты.
- `spend_rub(usage, model)` — оценка стоимости одного вызова из `usage`
  (точные токены из ответа шлюза) × тариф модели.
- `preflight(min_rub)` — бросает TenderAIError, если остаток ниже порога,
  чтобы подбор не уходил в минус.

Тарифы AI Gateway (₽ за 1M токенов) в консоли: AI Gateway → ключ → вкладка
«Модели». Пока не заполнены — `spend_rub` возвращает None и в лог идёт
только счётчик токенов.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Под тестами не ходим в сеть за балансом.
_UNDER_TEST = "test" in sys.argv[1:2] or "pytest" in sys.argv[0]

_FINANCES_URL = "https://api.timeweb.cloud/api/v1/account/finances"
_cache: dict = {"at": 0.0, "value": None, "meta": {}}

# ₽ за 1M токенов: (входящие, исходящие). Из дашборда AI Gateway, 2026-09-10.
RATES_RUB_PER_M: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4-5": (405, 2025),
    "anthropic/claude-sonnet-4-6": (405, 2025),
    "anthropic/claude-haiku-4-5": (135, 1080),
    "openai/gpt-4.1-mini": (54, 216),
    "openai/gpt-4.1-nano": (27, 169),
    "gemini/gemini-2.5-flash-lite": (34, 203),
    "gemini/gemini-3.1-flash-lite": (34, 203),
}


def account_balance(*, force: bool = False) -> tuple[float | None, dict]:
    """(остаток_₽, сырой ответ). None при ошибке/отсутствии токена."""
    if _UNDER_TEST and not force:
        return None, {}
    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < 120:
        return _cache["value"], _cache["meta"]
    token = os.getenv("TIMEWEB_API_TOKEN", "").strip()
    if not token:
        return None, {}
    request = urllib.request.Request(
        _FINANCES_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        logger.warning("Timeweb finances API недоступен", exc_info=True)
        return _cache["value"], _cache["meta"]
    finances = data.get("finances", {}) if isinstance(data, dict) else {}
    balance = finances.get("total_balance", finances.get("balance"))
    balance = float(balance) if isinstance(balance, (int, float)) else None
    _cache.update(at=now, value=balance, meta=finances)
    return balance, finances


def spend_rub(usage: dict, model: str) -> float | None:
    """Оценка стоимости одного вызова, ₽. None если тариф модели не задан."""
    rate = RATES_RUB_PER_M.get(model)
    if not rate or not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    return round(prompt_tokens / 1_000_000 * rate[0] + completion_tokens / 1_000_000 * rate[1], 4)


def preflight(min_rub: float | None = None) -> None:
    """Останавливает подбор, если остаток ниже порога."""
    from .services import TenderAIError

    threshold = float(os.getenv("CASCADE_MIN_BALANCE_RUB", "20")) if min_rub is None else min_rub
    if threshold <= 0:
        return
    balance, _ = account_balance()
    if balance is not None and balance < threshold:
        raise TenderAIError(
            f"Баланс Timeweb {balance:.0f} ₽ ниже порога {threshold:.0f} ₽ — "
            f"подбор остановлен, чтобы не уйти в минус. Пополните счёт или "
            f"снизьте CASCADE_MIN_BALANCE_RUB."
        )


def report_line(usage: dict, models: dict[str, dict] | None = None) -> str:
    """Строка для лога после прогона: токены и (если есть тарифы) ₽."""
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    parts = [f"{prompt_tokens} in / {completion_tokens} out токенов"]
    total = 0.0
    known = False
    for model, model_usage in (models or {}).items():
        cost = spend_rub(model_usage, model)
        if cost is not None:
            total += cost
            known = True
    if known:
        parts.append(f"~{total:.2f} ₽")
    return "; ".join(parts)
