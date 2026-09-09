"""Плюс/минус-слова: разбор пользовательских списков и проверка названия тендера."""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").lower().replace("ё", "е")).strip()


def parse_terms(raw: str) -> list[list[str]]:
    """Список записей, разделённых запятой или переводом строки.

    Каждая запись — список частей: 'живых+цветов' -> ['живых', 'цветов'] (нужны все).
    Обычное слово -> список из одного элемента.
    """
    entries: list[list[str]] = []
    for chunk in re.split(r"[\n,]+", raw or ""):
        parts = [p for p in (_norm(p) for p in chunk.split("+")) if p]
        if parts:
            entries.append(parts)
    return entries


def match_title(title: str, include: list[list[str]], exclude: list[list[str]]) -> tuple[bool, list[str]]:
    """(проходит ли тендер, какие плюс-слова сработали).

    Скрываем, если сработало любое минус-слово. Иначе — проходит, если сработало
    хотя бы одно плюс-слово; пустой список плюс-слов пропускает всё.
    """
    text = _norm(title)
    if any(all(part in text for part in entry) for entry in exclude):
        return False, []
    if not include:
        return True, []
    hits = ["+".join(entry) for entry in include if all(part in text for part in entry)]
    return bool(hits), hits
