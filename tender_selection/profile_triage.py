"""Неблокирующая, объяснимая проверка входящих по коммерческому профилю."""
from __future__ import annotations

import logging
import os
import threading
import json
from decimal import Decimal

from django.utils import timezone

from .ai_gateway import AIGatewayError, chat_json
from .filtering import match_title, parse_terms
from .models import FilterSettings, Tender

logger = logging.getLogger(__name__)

PROFILE = """Компания поставляет полиграфию, сувенирную и наградную продукцию,
текстиль и промо-товары с нанесением логотипа. Не относятся к профилю медицина
и стерилизация, электромонтаж и маркировка электрооборудования, строительство,
поставка оборудования, программное обеспечение и хозяйственные товары — даже
если в названии случайно встретились слова «пакет», «нанесение» или «печать»."""
MODEL = os.getenv("INCOMING_PROFILE_MODEL", "gemini/gemini-3.1-flash-lite")
BATCH_SIZE = 10
_VERDICTS = {"fit": Tender.PROFILE_SIGNAL_CLEAR, "review": Tender.PROFILE_SIGNAL_DOUBT, "not_fit": Tender.PROFILE_SIGNAL_NOT_PROFILE}


def _state(tender: Tender, settings: FilterSettings) -> dict:
    title = tender.title or tender.object_info
    _passes, hits = match_title(title, parse_terms(settings.include_words), parse_terms(settings.exclude_words))
    return {
        "id": tender.pk,
        "title": title,
        "object_info": tender.object_info,
        "okpd2": tender.okpd2,
        "matched_terms": hits,
    }


def _answers(batch: list[Tender], settings: FilterSettings) -> list[dict]:
    prompt = (
        f"Коммерческий профиль:\n{PROFILE}\n\n"
        "Проверь каждую закупку по её фактическому предмету. Совпадение одного слова не является основанием "
        "считать закупку подходящей. Верни только JSON: "
        '{"items":[{"id":123,"verdict":"fit|review|not_fit","confidence":0.0,'
        '"reason":"короткая причина простыми словами"}]}. '
        "not_fit ставь только при явном чужом предмете; review — если из названия нельзя решить.\n\n"
        "Закупки:\n" + json.dumps([_state(tender, settings) for tender in batch], ensure_ascii=False)
    )
    response = chat_json(
        "Ты аккуратно классифицируешь закупки. Не придумывай товары, которых нет в тексте.",
        prompt, model=MODEL, max_tokens=1200, timeout=45,
    )
    items = response.get("data", {}).get("items") if isinstance(response.get("data"), dict) else None
    return items if isinstance(items, list) else []


def _valid_answer(item: object, allowed_ids: set[int]):
    if not isinstance(item, dict):
        return None
    try:
        tender_id = int(item.get("id"))
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        return None
    verdict = str(item.get("verdict") or "")
    if tender_id not in allowed_ids or verdict not in _VERDICTS or not 0 <= confidence <= 1:
        return None
    reason = str(item.get("reason") or "").strip()[:500]
    if not reason:
        return None
    return tender_id, _VERDICTS[verdict], confidence, reason


def triage_tenders(ids: list[int]) -> int:
    """Проверяет только ещё не оценённые новые входящие и сохраняет сигнал, не решение."""
    if not FilterSettings.load().profile_triage_enabled:
        return 0
    tenders = list(Tender.objects.filter(
        pk__in=ids, status=Tender.NEW, review=Tender.UNREVIEWED, profile_checked_at__isnull=True,
    ).only("pk", "title", "object_info", "okpd2"))
    checked = 0
    for offset in range(0, len(tenders), BATCH_SIZE):
        batch = tenders[offset:offset + BATCH_SIZE]
        try:
            answers = _answers(batch, FilterSettings.load())
        except AIGatewayError:
            logger.exception("Gemini profile triage failed")
            return checked
        now = timezone.now()
        for item in answers:
            answer = _valid_answer(item, {tender.pk for tender in batch})
            if answer is None:
                continue
            tender_id, signal, confidence, reason = answer
            updated = Tender.objects.filter(pk=tender_id, profile_checked_at__isnull=True).update(
                profile_signal=signal, profile_confidence=Decimal(str(round(confidence, 3))),
                profile_reason=reason, profile_model=MODEL, profile_checked_at=now,
            )
            checked += updated
    return checked


def start_profile_triage_in_background(ids: list[int]) -> None:
    if not ids or not FilterSettings.load().profile_triage_enabled:
        return

    def job():
        triage_tenders(ids)

    threading.Thread(target=job, name="tender-profile-triage", daemon=True).start()
