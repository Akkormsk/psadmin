"""Аудит плюс/минус-слов «Входящих».

Статистика слов и последствия любого предложенного слова считаются здесь, без ИИ.
ИИ (только по кнопке) читает названия взятых, отклонённых, проигнорированных и
скрытых фильтром тендеров и предлагает слова — решает и применяет пользователь.
"""
from __future__ import annotations

import os
import re
from decimal import Decimal

from .ai_gateway import chat_json
from .filtering import _norm, match_title, parse_terms
from .models import FilterSettings, IncomingTrace, Tender, WordAudit

MODEL = os.getenv("WORD_AUDIT_MODEL", "openai/gpt-4.1-mini")
SAMPLE_LIMITS = {"taken": 120, "dismissed": 120, "ignored": 150, "hidden": 300}
TITLE_CHARS = 160
SUGGESTION_KINDS = ("add_plus", "add_minus", "remove_plus", "remove_minus")


def _entry_text(entry: list[str]) -> str:
    return "+".join(entry)


def _contains(text: str, entry: list[str]) -> bool:
    return all(part in text for part in entry)


def _corpus() -> dict[str, list[str]]:
    """Названия по судьбе тендера. «incoming» — живые «Входящие» и следы удалённых
    (а также архивные, но нетронутые): по ним видно, что слова пропускают и что
    скрывают прямо сейчас.

    Архивный тендер без реального просмотра (``opened_at`` пусто) — это тендер,
    который прошёл фильтр, но истёк срок до того, как до него дошли руки (см.
    ``services.purge_stale``): это НЕ сигнал «не наш профиль», а то же самое
    «показан, но не взят», что и раньше было видно по ``IncomingTrace``.
    Ручной отказ (кнопка «не наш профиль» на карточке) — только когда карточку
    реально открывали."""
    groups = {"taken": [], "dismissed": [], "incoming": [], "ignored": []}
    for tender in Tender.objects.only("title", "object_info", "review", "status", "opened_at"):
        title = tender.title or tender.object_info or ""
        if tender.review != Tender.UNREVIEWED:
            groups["taken"].append(title)
        elif tender.status == Tender.DISMISSED and tender.opened_at is None:
            groups["incoming"].append(title)
            groups["ignored"].append(title)
        elif tender.status == Tender.DISMISSED:
            groups["dismissed"].append(title)
        else:
            groups["incoming"].append(title)
    for trace in IncomingTrace.objects.only("title", "filtered_out").order_by("-purged_at"):
        groups["incoming"].append(trace.title)
        if not trace.filtered_out:
            groups["ignored"].append(trace.title)
    return groups


def _filters():
    settings = FilterSettings.load()
    return settings, parse_terms(settings.include_words), parse_terms(settings.exclude_words)


def word_stats() -> dict:
    """Что каждое плюс-слово пропустило и что с этими тендерами стало; что скрыло каждое минус-слово."""
    _, include, exclude = _filters()
    groups = _corpus()
    normalized = {key: [_norm(t) for t in titles] for key, titles in groups.items()}

    def passes(text):
        return match_title(text, include, exclude)[0]

    plus = []
    for entry in include:
        row = {"word": _entry_text(entry)}
        for key in ("taken", "dismissed", "ignored"):
            row[key] = sum(_contains(t, entry) for t in normalized[key])
        row["passed"] = sum(
            _contains(t, entry) and passes(t)
            for key in ("taken", "dismissed", "incoming") for t in normalized[key]
        )
        plus.append(row)
    minus = []
    for entry in exclude:
        minus.append({
            "word": _entry_text(entry),
            "hidden": sum(_contains(t, entry) for t in normalized["incoming"]),
            "hits_taken": sum(_contains(t, entry) for t in normalized["taken"]),
        })
    plus.sort(key=lambda r: (-r["passed"], r["word"]))
    minus.sort(key=lambda r: (-r["hidden"], r["word"]))
    return {"plus": plus, "minus": minus, "counts": {key: len(titles) for key, titles in groups.items()}}


def term_effects(term: str, *, minus: bool) -> dict:
    """Что изменится, если добавить слово: плюс — сколько скрытых сейчас откроет;
    минус — сколько проходящих скроет и сколько взятых в работу задевает."""
    _, include, exclude = _filters()
    entries = parse_terms(term)
    if not entries:
        return {}
    entry = entries[0]
    groups = _corpus()
    incoming = [_norm(t) for t in groups["incoming"]]
    taken = [_norm(t) for t in groups["taken"]]
    if minus:
        return {
            "hides_passing": sum(_contains(t, entry) and match_title(t, include, exclude)[0] for t in incoming),
            "hits_taken": sum(_contains(t, entry) for t in taken),
        }
    return {
        "opens_hidden": sum(
            not match_title(t, include, exclude)[0] and match_title(t, include + [entry], exclude)[0] for t in incoming
        ),
        "in_taken": sum(_contains(t, entry) for t in taken),
    }


SYSTEM_PROMPT = (
    "Ты помогаешь производственной компании (полиграфия, сувенирная продукция, текстиль с нанесением, "
    "наградная продукция) настроить фильтр госзакупок по названию. Фильтр: плюс-слова — тендер показывается, "
    "если в названии есть хотя бы одно; минус-слова — скрывается, если есть любое (минус сильнее плюса). "
    "Слово — это подстрока в нижнем регистре, обычно основа без окончания (\"футболк\", \"кружк\"); "
    "знак + внутри записи значит «все части сразу» (\"живых+цветов\"). "
    "Отвечай ТОЛЬКО валидным JSON без markdown, по-русски."
)

_SCHEMA = """{
  "add_plus": [{"word": "основа слова", "topic": "опционально: тематика, если это часть пачки слов по одной идее — иначе пустая строка", "why": "1 предложение", "examples": ["название из списка СКРЫТЫ ФИЛЬТРОМ, которое оно откроет"]}],
  "add_minus": [{"word": "...", "why": "...", "examples": ["название из ПОКАЗАНЫ НО НЕ ВЗЯТЫ или СКРЫТЫ ВРУЧНУЮ"]}],
  "remove_plus": [{"word": "текущее плюс-слово", "why": "почему бесполезно или вредно"}],
  "remove_minus": [{"word": "текущее минус-слово", "why": "какие нужные тендеры оно скрывает", "examples": ["..."]}]
}"""


def _section(title: str, titles: list[str], limit: int) -> tuple[str, int]:
    seen, lines = set(), []
    for text in titles:
        text = re.sub(r"\s+", " ", text or "").strip()[:TITLE_CHARS]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            lines.append(f"- {text}")
        if len(lines) >= limit:
            break
    return f"{title} ({len(lines)}):\n" + ("\n".join(lines) or "- нет"), len(lines)


def _prompt(settings, groups) -> tuple[str, dict]:
    _, include, exclude = _filters()
    hidden = [t for t in groups["incoming"] if not match_title(_norm(t), include, exclude)[0]]
    parts, counts = [], {}
    for key, title, titles in (
        ("taken", "ВЗЯТЫ В РАБОТУ (наш профиль)", groups["taken"]),
        ("dismissed", "СКРЫТЫ ВРУЧНУЮ ИЗ ВХОДЯЩИХ (не наш профиль)", groups["dismissed"]),
        ("ignored", "ПОКАЗАНЫ НО НЕ ВЗЯТЫ (скорее не наш профиль)", groups["ignored"]),
        ("hidden", "СКРЫТЫ ФИЛЬТРОМ (ищи среди них пропущенные нужные)", hidden),
    ):
        text, counts[key] = _section(title, titles, SAMPLE_LIMITS[key])
        parts.append(text)
    header = (
        f"Текущие плюс-слова:\n{settings.include_words or '— нет'}\n\n"
        f"Текущие минус-слова:\n{settings.exclude_words or '— нет'}\n\n"
    )
    return (
        header + "\n\n".join(parts)
        + f"\n\nПредложи изменения фильтра. Верни JSON строго по схеме:\n{_SCHEMA}\n"
        "Не предлагай слова, которые уже есть. Минус-слово не должно задевать тендеры из ВЗЯТЫ В РАБОТУ.\n"
        "Отдельно поищи среди СКРЫТЫ ФИЛЬТРОМ целую пропущенную тематику (не единичное слово, а направление "
        "товаров/услуг в нашем профиле, которое сейчас не открывает ни одно плюс-слово) — предложи для неё "
        "несколько add_plus-слов с одинаковым непустым \"topic\"."
    ), counts


def _clean(result: dict) -> dict:
    """Оставляем только осмысленные предложения и дописываем посчитанные бэкендом последствия."""
    settings, include, exclude = _filters()
    current = {"plus": {_entry_text(e) for e in include}, "minus": {_entry_text(e) for e in exclude}}
    cleaned = {}
    for kind in SUGGESTION_KINDS:
        rows = []
        for item in result.get(kind) or []:
            if not isinstance(item, dict):
                continue
            entries = parse_terms(str(item.get("word") or ""))
            if not entries:
                continue
            word = _entry_text(entries[0])
            target = "plus" if kind.endswith("plus") else "minus"
            if (word in current[target]) == kind.startswith("add"):
                continue
            row = {"word": word, "why": str(item.get("why") or ""), "examples": [str(x) for x in (item.get("examples") or [])][:5]}
            if kind == "add_plus":
                row["topic"] = str(item.get("topic") or "").strip()[:60]
            if kind.startswith("add"):
                row["effects"] = term_effects(word, minus=target == "minus")
            rows.append(row)
        cleaned[kind] = rows
    return cleaned


def run_audit(user) -> WordAudit:
    """Один запрос к ИИ. AIGatewayError пробрасывается — показать пользователю."""
    settings, _, _ = _filters()
    prompt, counts = _prompt(settings, _corpus())
    answer = chat_json(SYSTEM_PROMPT, prompt, model=MODEL, max_tokens=3000, timeout=150)
    spend = None
    try:
        from tenders.gateway_budget import spend_rub

        value = spend_rub(answer.get("usage") or {}, MODEL)
        spend = Decimal(str(round(value, 2))) if value is not None else None
    except Exception:
        spend = None
    return WordAudit.objects.create(
        created_by=user, model=MODEL, input_counts=counts, result=_clean(answer.get("data") or {}), spend_rub=spend,
    )


def _merge(raw: str, add: list[str], remove: list[str]) -> str:
    kept = []
    remove_keys = {_entry_text(e[0]) for e in (parse_terms(w) for w in remove) if e}
    for chunk in re.split(r"[\n,]+", raw or ""):
        entries = parse_terms(chunk)
        if entries and _entry_text(entries[0]) not in remove_keys:
            kept.append(chunk.strip())
    existing = {_entry_text(parse_terms(c)[0]) for c in kept}
    for word in add:
        entries = parse_terms(word)
        if entries and _entry_text(entries[0]) not in existing:
            existing.add(_entry_text(entries[0]))
            kept.append(word.strip())
    return "\n".join(kept)


def apply_words(*, add_plus, add_minus, remove_plus, remove_minus) -> None:
    settings = FilterSettings.load()
    settings.include_words = _merge(settings.include_words, add_plus, remove_plus)
    settings.exclude_words = _merge(settings.exclude_words, add_minus, remove_minus)
    settings.save(update_fields=["include_words", "exclude_words"])
    _drop_applied(add_plus=add_plus, add_minus=add_minus, remove_plus=remove_plus, remove_minus=remove_minus)


def _drop_applied(**chosen: list[str]) -> None:
    """Принятые предложения убираются из последнего аудита — иначе страница
    показывает их снова и позволяет применить повторно."""
    audit = WordAudit.objects.first()
    if not audit:
        return
    result = audit.result or {}
    changed = False
    for kind, words in chosen.items():
        wanted = {w.strip().lower() for w in words}
        if not wanted or not result.get(kind):
            continue
        kept = [item for item in result[kind] if str(item.get("word", "")).strip().lower() not in wanted]
        if len(kept) != len(result[kind]):
            result[kind] = kept
            changed = True

    if changed:
        audit.result = result
        audit.save(update_fields=["result"])
