"""Неблокирующая пометка входящих, которые стоит проверить по профилю."""
from __future__ import annotations

import logging
import threading
from decimal import Decimal

from django.utils import timezone

from tenders.jev import decide_matrix

from .models import FilterSettings, Tender

logger = logging.getLogger(__name__)

PROFILE = "Полиграфия, сувенирная и наградная продукция, текстиль с нанесением логотипа."
LOW = 0.35
HIGH = 0.65
BATCH_SIZE = 20


def _state(tender: Tender) -> str:
    return f"Профиль компании: {PROFILE}\nТендер: {tender.title or tender.object_info}"


def _signal(value: float) -> str:
    if value <= LOW:
        return Tender.PROFILE_SIGNAL_NOT_PROFILE
    if value < HIGH:
        return Tender.PROFILE_SIGNAL_DOUBT
    return Tender.PROFILE_SIGNAL_CLEAR


def triage_tenders(ids: list[int]) -> int:
    """Проверяет только ещё не оценённые новые входящие и сохраняет сигнал, не решение."""
    if not FilterSettings.load().profile_triage_enabled:
        return 0
    tenders = list(Tender.objects.filter(
        pk__in=ids, status=Tender.NEW, review=Tender.UNREVIEWED, profile_checked_at__isnull=True,
    ).only("pk", "title", "object_info"))
    checked = 0
    for offset in range(0, len(tenders), BATCH_SIZE):
        batch = tenders[offset:offset + BATCH_SIZE]
        questions = {
            f"t{tender.pk}": {"type": "noul", "instructions": (
                "Соответствует ли тендер профилю компании? Высокая вероятность — обычно стоит рассматривать; "
                "низкая — явно чужая тема; середина — не уверен."
            )}
            for tender in batch
        }
        try:
            answers, _usage = decide_matrix("\n\n".join(_state(tender) for tender in batch), questions, timeout=30)
        except Exception:
            logger.exception("Jev profile triage failed")
            return checked
        now = timezone.now()
        for tender in batch:
            try:
                confidence = float(answers[f"t{tender.pk}"]["noul"])
            except (KeyError, TypeError, ValueError):
                continue
            Tender.objects.filter(pk=tender.pk, profile_checked_at__isnull=True).update(
                profile_signal=_signal(confidence), profile_confidence=Decimal(str(round(confidence, 3))), profile_checked_at=now,
            )
            checked += 1
    return checked


def start_profile_triage_in_background(ids: list[int]) -> None:
    if not ids or not FilterSettings.load().profile_triage_enabled:
        return

    def job():
        triage_tenders(ids)

    threading.Thread(target=job, name="tender-profile-triage", daemon=True).start()
