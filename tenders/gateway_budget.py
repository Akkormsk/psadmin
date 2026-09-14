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

# ₽ за 1M токенов: (входящие, исходящие). Из дашборда AI Gateway (вкладка
# «Модели»), снимок 2026-09-14. Шлюз эту цену по API не отдаёт — только
# список моделей (available_models/_fetch_chat_models ниже), поэтому тариф
# приходится сверять руками. Модель, которой здесь нет, просто не покажет
# цену (spend_rub → None) — это не значит, что она недоступна, значит только
# «тариф ещё не занесли». Обновлять по мере расширения консоли Timeweb.
RATES_RUB_PER_M: dict[str, tuple[float, float]] = {
    "anthropic/claude-fable-5": (1350, 6750),
    "anthropic/claude-fable-5-1": (1350, 6750),
    "anthropic/claude-haiku-4-5": (135, 1080),
    "anthropic/claude-opus-4-6": (675, 3375),
    "anthropic/claude-opus-4-7": (675, 3375),
    "anthropic/claude-opus-4-8": (675, 3375),
    "anthropic/claude-opus-5": (675, 3375),
    # sonnet-4-5 не было отдельной строкой в снимке прайса — но 4.6/5 обе
    # идут по 405/2025 (как и вся линейка Opus по одной ставке 4.6-4.8-5),
    # так что это подстановка по образцу, а не факт из консоли. Текущий
    # дефолт _AGENT_MODEL/_STRONG_MODEL — без этой строки preflight-проверка
    # стоимости в cascade._call_ai для него молча не работает (spend_rub →
    # None → 0 ₽), а не просто "не показывает цену в списке".
    "anthropic/claude-sonnet-4-5": (405, 2025),
    "anthropic/claude-sonnet-4-6": (405, 2025),
    "anthropic/claude-sonnet-5": (405, 2025),
    "dashscope/qwen3-max": (162, 810),
    "dashscope/qwen3.5-flash": (14, 52),
    "dashscope/qwen3.5-plus": (52, 324),
    "dashscope/qwen3.6-flash": (33.75, 202.5),
    "dashscope/qwen3.6-plus": (68, 405),
    "dashscope/qwen3.7-max": (337.5, 1012.5),
    "dashscope/qwen3.7-plus": (54, 216),
    "dashscope/qwen3.8-max": (270, 810),
    "deepseek/deepseek-v4-flash": (59.4, 178.2),
    "deepseek/deepseek-v4-pro": (178.2, 534.6),
    "gemini/gemini-3.1-flash-lite": (34, 203),
    "gemini/gemini-3.1-pro-preview": (270, 1620),
    "gemini/gemini-3.5-flash": (202.5, 1215),
    "gemini/gemini-3.6-flash": (202.5, 1012.5),
    "gemini/gemini-3.7-flash": (101.25, 506.25),
    "gemini/gemini-3.8-flash": (101.25, 506.25),
    "moonshot/kimi-k2.6": (236, 979),          # прямой маршрут Moonshot
    "moonshot/kimi-k2.7-code": (128.25, 540),
    "moonshot/kimi-k3": (405, 2025),
    "openai/gpt-4.1": (270, 1080),
    "openai/gpt-4.1-mini": (54, 216),
    "openai/gpt-4o": (675, 2700),
    "openai/gpt-5.1": (162, 1350),
    "openai/gpt-5.2": (236, 1890),
    "openai/gpt-5.4": (338, 2025),
    "openai/gpt-5.4-mini": (101, 608),
    "openai/gpt-5.4-nano": (27, 169),
    "openai/gpt-5.5": (676, 4050),
    "openai/gpt-5.6-luna": (27, 162),
    "openai/gpt-5.6-sol": (675, 4050),
    "openai/gpt-5.6-terra": (270, 1620),
    "openai/gpt-6-astra": (1350, 6750),
    "timeweb/gemma4:31b": (189, 540),
    "timeweb/glm-5.2": (189, 594),             # хостинг Timeweb, дешевле прямого
    "timeweb/gpt-oss-120b": (20, 82),
    "timeweb/kimi-k2.6": (128.25, 540),        # хостинг Timeweb, дешевле прямого
    "xai/grok-4.3": (168.75, 337.5),
    "xai/grok-4.5": (270, 810),
    "xai/grok-4.6": (540, 1620),
    "xai/grok-code-fast-1": (27, 203),
    "yandex/aliceai-llm-latest": (510, 2030),
    "yandex/yandexgpt-lite": (210, 210),
    "yandex/yandexgpt-pro-5.1": (410, 410),
    "zai/glm-4.7": (81, 297),
    "zai/glm-4.7-flashx": (9.45, 54),
    "zai/glm-5.2": (267, 1120),                # прямой маршрут Z.AI, дороже
    "zai/glm-5.3": (189, 594),
    "zai/glm-5.3-flash": (20.25, 67.5),
}

# Человекочитаемые названия для тех же моделей — тоже не из API (шлюз отдаёт
# только технический id вида "anthropic/claude-sonnet-4-6"), вручную из
# консоли. Модель без записи здесь просто показывается по своему id — тоже
# не «недоступна», значит только «название ещё не занесли».
MODEL_LABELS: dict[str, str] = {
    "anthropic/claude-fable-5": "Claude Fable 5",
    "anthropic/claude-fable-5-1": "Claude Fable 5.1",
    "anthropic/claude-haiku-4-5": "Claude Haiku 4.5",
    "anthropic/claude-opus-4-5": "Claude Opus 4.5",
    "anthropic/claude-opus-4-6": "Claude Opus 4.6",
    "anthropic/claude-opus-4-7": "Claude Opus 4.7",
    "anthropic/claude-opus-4-8": "Claude Opus 4.8",
    "anthropic/claude-opus-5": "Claude Opus 5",
    "anthropic/claude-sonnet-4-5": "Claude Sonnet 4.5",
    "anthropic/claude-sonnet-4-6": "Claude Sonnet 4.6",
    "anthropic/claude-sonnet-5": "Claude Sonnet 5",
    "dashscope/qwen3-max": "Qwen3 Max",
    "dashscope/qwen3.5-flash": "Qwen3.5 Flash",
    "dashscope/qwen3.5-plus": "Qwen3.5 Plus",
    "dashscope/qwen3.6-flash": "Qwen3.6 Flash",
    "dashscope/qwen3.6-plus": "Qwen3.6 Plus",
    "dashscope/qwen3.7-max": "Qwen3.7 Max",
    "dashscope/qwen3.7-plus": "Qwen3.7 Plus",
    "dashscope/qwen3.8-max": "Qwen3.8 Max",
    "deepseek/deepseek-flash": "DeepSeek Flash",
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "gemini/gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini/gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini/gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini/gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite",
    "gemini/gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
    "gemini/gemini-3.5-flash": "Gemini 3.5 Flash",
    "gemini/gemini-3.6-flash": "Gemini 3.6 Flash",
    "gemini/gemini-3.7-flash": "Gemini 3.7 Flash",
    "gemini/gemini-3.8-flash": "Gemini 3.8 Flash",
    "moonshot/kimi-k2.6": "Kimi K2.6 · Moonshot",
    "moonshot/kimi-k2.7-code": "Kimi K2.7 Code",
    "moonshot/kimi-k3": "Kimi K3",
    "openai/gpt-4.1": "GPT-4.1",
    "openai/gpt-4.1-mini": "GPT-4.1 mini",
    "openai/gpt-4o": "GPT-4o",
    "openai/gpt-5": "GPT-5",
    "openai/gpt-5-mini": "GPT-5 mini",
    "openai/gpt-5-nano": "GPT-5 nano",
    "openai/gpt-5.1": "GPT-5.1",
    "openai/gpt-5.2": "GPT-5.2",
    "openai/gpt-5.4": "GPT-5.4",
    "openai/gpt-5.4-mini": "GPT-5.4 mini",
    "openai/gpt-5.4-nano": "GPT-5.4 nano",
    "openai/gpt-5.5": "GPT-5.5",
    "openai/gpt-5.6-luna": "GPT-5.6 Luna",
    "openai/gpt-5.6-sol": "GPT-5.6 Sol",
    "openai/gpt-5.6-terra": "GPT-5.6 Terra",
    "openai/gpt-6-astra": "GPT-6 Astra",
    "timeweb/gemma4:31b": "Gemma 4 (31B)",
    "timeweb/glm-4-6-357b": "GLM-4-6 (357B)",
    "timeweb/glm-5.2": "GLM-5.2 · Timeweb",
    "timeweb/gpt-oss-120b": "GPT-OSS 120B",
    "timeweb/kimi-k2-instruct": "Kimi K2 Instruct",
    "timeweb/kimi-k2.6": "Kimi K2.6 · Timeweb",
    "timeweb/llama-3-3-70b-instruct": "Llama 3.3 70B Instruct",
    "timeweb/qwen3.6-35b-a3b": "Qwen3.6 35B A3B",
    "xai/grok-4.3": "Grok 4.3",
    "xai/grok-4.5": "Grok 4.5",
    "xai/grok-4.6": "Grok 4.6",
    "xai/grok-code-fast-1": "Grok Code Fast",
    "yandex/aliceai-llm-latest": "Alice AI LLM",
    "yandex/yandexgpt-lite": "YandexGPT 5.1 Lite",
    "yandex/yandexgpt-pro-5.1": "YandexGPT 5.1 Pro",
    "zai/glm-4.7": "GLM-4.7",
    "zai/glm-4.7-flashx": "GLM-4.7 FlashX",
    "zai/glm-5.2": "GLM-5.2 · Z.AI",
    "zai/glm-5.3": "GLM-5.3",
    "zai/glm-5.3-flash": "GLM-5.3 Flash",
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


_MODELS_URL_ENV = "TIMEWEB_AI_BASE_URL"
_models_cache: dict = {"at": 0.0, "value": None}  # value = кортеж словарей {id, max_input_tokens, max_output_tokens}
# Список на случай, если шлюз недоступен (офлайн-дев, нет ключа) — не список
# «разрешённых», а последний известный рабочий набор, чтобы форма не осталась
# пустой.
_FALLBACK_ROWS: tuple[dict, ...] = tuple(
    {"id": model_id, "max_input_tokens": None, "max_output_tokens": None} for model_id in (
        "anthropic/claude-sonnet-4-5", "anthropic/claude-haiku-4-5",
        "openai/gpt-4.1-mini", "openai/gpt-4.1-nano", "gemini/gemini-3.1-flash-lite",
    )
)
_FALLBACK_MODELS = tuple(row["id"] for row in _FALLBACK_ROWS)

# id-подстроки моделей, которые заведомо не годятся для чата (эмбеддинги,
# картинки, речь) и на которых шлюз не всегда проставляет "mode" — тогда
# смотрим на сам id. Не список продуктовых категорий (то, что каскаду
# запрещено хардкодить) — список типов API-вызова, это другое: у "chat" vs
# "embedding" vs "image_generation" нет тысячи случайных вариантов.
_NON_CHAT_ID_MARKERS = ("embed", "bge", "flux", "tts", "transcribe", "image")


def _is_chat_row(row: dict) -> bool:
    mode = row.get("mode")
    if mode:
        return mode == "chat"
    model_id = str(row.get("id", "")).lower()
    return not any(marker in model_id for marker in _NON_CHAT_ID_MARKERS)


def _fetch_chat_rows(*, force: bool = False) -> tuple[dict, ...]:
    """Модели шлюза, пригодные для чата (не embedding/image/audio/…),
    с их max_input_tokens/max_output_tokens — тем, что шлюз реально
    отдаёт про "мощность" модели. Кэш 6 часов. Список не захардкожен —
    шлюз добавляет модели чаще, чем мы правим код. При недоступности
    шлюза — последний известный набор."""
    if _UNDER_TEST and not force:
        return _FALLBACK_ROWS
    now = time.monotonic()
    if not force and _models_cache["value"] is not None and now - _models_cache["at"] < 21_600:
        return _models_cache["value"]
    key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    base_url = os.getenv(_MODELS_URL_ENV, "https://api.timeweb.ai/v1").rstrip("/")
    if not key:
        return _models_cache["value"] or _FALLBACK_ROWS
    request = urllib.request.Request(
        f"{base_url}/models", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        logger.warning("AI Gateway /models недоступен", exc_info=True)
        return _models_cache["value"] or _FALLBACK_ROWS
    raw_rows = data.get("data", data) if isinstance(data, dict) else data
    rows = tuple(sorted((
        {
            "id": row["id"],
            "max_input_tokens": row.get("max_input_tokens"),
            "max_output_tokens": row.get("max_output_tokens"),
        }
        for row in raw_rows if isinstance(row, dict)
        and isinstance(row.get("id"), str) and _is_chat_row(row)
    ), key=lambda row: row["id"])) if isinstance(raw_rows, list) else ()
    if not rows:
        return _models_cache["value"] or _FALLBACK_ROWS
    _models_cache.update(at=now, value=rows)
    return rows


def available_models(*, force: bool = False) -> tuple[str, ...]:
    """Просто id-шки чат-моделей шлюза — для валидации выбора модели
    (используется и здесь, и в tenders/views.py)."""
    return tuple(row["id"] for row in _fetch_chat_rows(force=force))


def model_catalog(*, force: bool = False) -> tuple[dict, ...]:
    """То же самое, но для селектора в лаборатории каскада: id, читаемое
    название, тариф (если занесён в RATES_RUB_PER_M — иначе None, а не
    выдуманное число) и реальный контекст/макс. ответ из ответа шлюза (если
    шлюз его прислал). Отсортировано от дешёвых к дорогим моделям
    (смешанная оценка 0.35×вход + 0.65×выход — вес ближе к тому, как
    вызываются наши шаги, где выход часто крупнее входа), без цены — в
    конец, по алфавиту."""
    catalog = []
    for row in _fetch_chat_rows(force=force):
        rate = RATES_RUB_PER_M.get(row["id"])
        label = MODEL_LABELS.get(row["id"], row["id"])
        display = label
        if rate:
            display += f" — {rate[0]:g}/{rate[1]:g} ₽"
        max_out = row.get("max_output_tokens")
        if isinstance(max_out, int) and max_out >= 1000:
            display += f" · до {max_out // 1000}K ответ"
        catalog.append({
            "id": row["id"],
            "label": label,
            "display": display,
            "in_rub": rate[0] if rate else None,
            "out_rub": rate[1] if rate else None,
            "blended_rub": rate[0] * 0.35 + rate[1] * 0.65 if rate else None,
            "max_input_tokens": row.get("max_input_tokens"),
            "max_output_tokens": row.get("max_output_tokens"),
        })
    catalog.sort(key=lambda entry: (
        0 if entry["blended_rub"] is not None else 1,
        entry["blended_rub"] or 0,
        entry["label"],
    ))
    return tuple(catalog)


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
