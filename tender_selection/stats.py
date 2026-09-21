"""Сбор статистики снижения цен по завершённым закупкам.

Идея: раз в сутки берём свежие контракты по нашим категориям (несколько запросов),
а по каждому новому — одну карточку закупки ради НМЦК, чтобы посчитать снижение.
Бюджет запросов ограничен; что не добрали — доберём следующим прогоном.

Сбор запускается ПОСЛЕ выгрузки тендеров в том же процессе (или отдельной командой),
поэтому параллельных обращений к API не бывает.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count
from django.utils import timezone

from . import gosplan
from .filtering import _norm
from .models import ContractStat, FilterSettings, StatsRun
from .services import CATEGORY_GROUPS, _chunk, effective_okpd2

# снижение вне этого коридора — почти всегда артефакт (многолотовая/совместная закупка,
# цена за единицу вместо суммы контракта) → в статистику по снижению не берём
_MIN_DISCOUNT = Decimal("0")
_MAX_DISCOUNT = Decimal("80")

_CATEGORY_PREFIXES = [code for code, _ in CATEGORY_GROUPS]


def category_for_codes(okpd2, ktru, prefixes=None) -> str:
    """Группа-категория по кодам: ОКПД2, а где пусто — ОКПД2-часть кода КТРУ."""
    prefixes = prefixes or _CATEGORY_PREFIXES
    candidates = [str(c) for c in (okpd2 or [])]
    for code in ktru or []:
        candidates.append(str(code).split("-", 1)[0])  # 17.23.13.196-00000001 -> 17.23.13.196
    best = ""
    for code in candidates:
        for prefix in prefixes:
            if code.startswith(prefix) and len(prefix) > len(best):
                best = prefix
    return best


def _price(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _discount(nmck, final):
    """Снижение в %, но только если оно правдоподобно (см. коридор). Иначе None."""
    if not (nmck and final and nmck > 0):
        return None
    pct = ((nmck - final) / nmck * Decimal(100)).quantize(Decimal("0.1"))
    return pct if _MIN_DISCOUNT <= pct <= _MAX_DISCOUNT else None


def collect_price_stats(
    *,
    since_days: int = 30,
    catalog_requests: int = 12,
    nmck_requests: int = 48,
    categories=None,
) -> StatsRun:
    """Один прогон сбора. Возвращает журнальную запись StatsRun.

    ``catalog_requests`` — потолок запросов на шаг 1 (свежие контракты по категориям),
    ``nmck_requests`` — на шаг 2 (добор начальной цены по одной карточке закупки).
    """
    run = StatsRun.objects.create(
        started_at=timezone.now(),
        params={"since_days": since_days, "catalog_requests": catalog_requests, "nmck_requests": nmck_requests},
    )
    prefixes = list(categories) if categories else effective_okpd2(FilterSettings.load())
    batches = _chunk(prefixes, 5)
    since = (timezone.now() - timedelta(days=since_days)).date().isoformat() + "T00:00:00+03:00"

    counters = {"requests": 0, "contracts": 0, "created": 0, "filled": 0}

    def _tick(_no, _got):
        counters["requests"] += 1

    # делим бюджет шага 1 поровну между батчами категорий — иначе последние не сканируются
    per_batch = max(1, -(-catalog_requests // len(batches))) if batches else 0

    step1_limited = False
    try:
        # 1) свежие контракты по нашим категориям
        for index, batch in enumerate(batches):
            if index:
                time.sleep(gosplan.THROTTLE_SECONDS)
            params = {"classifier": batch, "published_after": since, "sort": "published_at_desc"}
            try:
                contracts = list(gosplan.iter_contracts(params=params, max_requests=per_batch, on_request=_tick))
            except gosplan.RateLimitError:
                step1_limited = True
                break  # лимит — то, что успели, сохранено; продолжим следующим прогоном
            for rec in contracts:
                number = (rec.get("purchase_number") or "").strip()
                reg = (rec.get("reg_num") or "").strip() or number
                if not number or not reg:
                    continue
                counters["contracts"] += 1
                suppliers = rec.get("suppliers") or []
                _, created = ContractStat.objects.update_or_create(
                    law="fz44",
                    contract_reg_num=reg,
                    defaults={
                        "purchase_number": number,
                        "category": category_for_codes(rec.get("okpd2"), rec.get("ktru"), prefixes),
                        "okpd2": rec.get("okpd2") or [],
                        "ktru": rec.get("ktru") or [],
                        "region": rec.get("region"),
                        "subject": rec.get("subject") or "",
                        "customer_inn": (rec.get("customer") or "").strip(),
                        "final_price": _price(rec.get("price")),
                        "winner_inn": suppliers[0] if suppliers else "",
                        "contract_date": _date(rec.get("published_at")) or _date(rec.get("exe_start")),
                    },
                )
                counters["created"] += int(created)

        # несколько контрактов на один номер закупки = совместная/многолотовая —
        # сравнивать один контракт с общей НМЦК нельзя, снижение по ним не считаем
        shared = [
            row["purchase_number"]
            for row in ContractStat.objects.values("purchase_number").annotate(n=Count("id")).filter(n__gt=1)
        ]
        if shared:
            ContractStat.objects.filter(purchase_number__in=shared, shared_purchase=False).update(
                shared_purchase=True, discount_pct=None
            )

        # по совместным/многолотовым НМЦК добирать бессмысленно — закрываем без запроса
        ContractStat.objects.filter(law="fz44", shared_purchase=True, nmck_checked=False).update(nmck_checked=True)

        # 2) добрать НМЦК по одиночным контрактам без неё — по одной карточке закупки на запрос
        attempts = 0
        pending = list(
            ContractStat.objects.filter(law="fz44", nmck_checked=False, shared_purchase=False)
            .order_by("-contract_date")[: nmck_requests + 20]
        )
        rate_limit_hits = 0
        for stat in pending:
            if attempts >= nmck_requests:
                break
            time.sleep(gosplan.THROTTLE_SECONDS)
            try:
                payload = gosplan.fetch_purchase(stat.purchase_number)
            except gosplan.RateLimitError:
                rate_limit_hits += 1
                if rate_limit_hits >= 3:  # лимит держится — заканчиваем прогон, доберём позже
                    break
                time.sleep(gosplan.RATE_LIMIT_WAIT)
                continue
            except gosplan.GosplanError:
                attempts += 1
                counters["requests"] += 1
                continue
            rate_limit_hits = 0
            attempts += 1
            counters["requests"] += 1
            nmck = _price((payload or {}).get("max_price"))
            stat.nmck = nmck
            stat.nmck_checked = True
            stat.discount_pct = _discount(nmck, stat.final_price)
            stat.save(update_fields=["nmck", "nmck_checked", "discount_pct", "collected_at"])
            counters["filled"] += 1

        run.ok = True
        if step1_limited:
            run.error = "лимит запросов — часть контрактов доберётся следующим прогоном"
    except gosplan.GosplanError as exc:
        run.error = str(exc)
        run.ok = False

    run.finished_at = timezone.now()
    run.requests_made = counters["requests"]
    run.contracts_seen = counters["contracts"]
    run.created_count = counters["created"]
    run.filled_count = counters["filled"]
    run.duration_seconds = round((run.finished_at - run.started_at).total_seconds(), 1)
    run.save()
    return run


# --- Витрина: подсказка по снижению для карточки тендера ----------------------

_TARGET_COUNT = 10        # сколько похожих закупок стараемся набрать
_MIN_SAMPLES = 3          # меньше — раздел не показываем, это не статистика

# служебные слова из названий закупок — в ключевые не берём
_SUBJECT_STOP = {
    "поставка", "поставки", "поставку", "поставке", "оказание", "оказанию", "услуги",
    "услуг", "услуга", "выполнение", "выполнению", "работы", "работ", "изготовление",
    "изготовлению", "приобретение", "закупка", "закупку", "закупки", "товаров", "товара",
    "товары", "продукции", "продукция", "нужд", "обеспечение", "комплект", "комплекта",
    "электронный", "электронной", "аукцион", "аукциона", "форме", "запрос", "котировок",
    "участниками", "которого", "могут", "быть", "только", "субъекты", "малого", "среднего",
    "предпринимательства", "иных", "государственных", "муниципальных", "учреждения",
    "учреждений", "организации", "проведение", "нужды", "материалов", "материал",
    "продукта", "прочих", "прочие",
}


def tender_categories(tender, card=None) -> set[str]:
    """Группы-категории тендера: из его кодов ОКПД2 + кодов позиций извещения."""
    cats = set()
    for code in _codes_of(tender, card):
        group = category_for_codes([code], [])
        if group:
            cats.add(group)
    return cats


def _codes_of(tender, card=None) -> set[str]:
    codes = {str(c) for c in (tender.okpd2 or [])}
    for item in (card or {}).get("items", []):
        if item.get("code"):
            codes.add(str(item["code"]).split("-", 1)[0])
    return codes


def _keywords(text: str) -> set[str]:
    """Значимые слова из названия/предмета — по первым 8 буквам, без служебных."""
    cleaned = _norm(text)
    for ch in ",.()«»\"'/:;":
        cleaned = cleaned.replace(ch, " ")
    out = set()
    for word in cleaned.split():
        word = word.strip("-–")
        if len(word) >= 5 and not word.isdigit() and word not in _SUBJECT_STOP:
            out.add(word[:8])
    return out


def _tender_keywords(tender, card=None) -> set[str]:
    kws = _keywords(tender.title or tender.object_info or "")
    for item in (card or {}).get("items", []):
        if item.get("name"):
            kws |= _keywords(item["name"])
    return kws


def _estimate_keywords(estimate) -> set[str]:
    """Слова из названия расчёта и его товарных позиций — точнее, чем общее
    название закупки: по своим прошлым тендерам видно, что именно покупали
    (не «поставка полиграфии», а «визитки», «буклеты» и т.д.)."""
    kws = _keywords(estimate.name or "")
    for line in estimate.lines.all():
        if line.name:
            kws |= _keywords(line.name)
    return kws


def _region_of_estimate(estimate):
    if not estimate.tender_id:
        return None
    found = getattr(estimate.tender, "found_tender", None)
    return found.region if found else None


def price_stats_for(tender, card=None) -> dict | None:
    """Сводка по снижению цен на похожих закупках — для раздела карточки.

    Похожесть = совпадение значимых слов в названии/товарных позициях, без
    баллов и весов. Сначала берём свои прошлые тендеры (знаем не только
    общее название закупки, но и реальные товарные позиции) — если не
    набралось ``_TARGET_COUNT`` — добираем из открытой истории похожих
    закупок. При равном совпадении слов вперёд идёт тот же регион, затем —
    более свежая запись.
    """
    if tender.law != "fz44":
        return None
    t_kws = _tender_keywords(tender, card)
    if not t_kws:
        return None

    from tenders.models import TenderEstimate

    own_pool = (
        TenderEstimate.objects.exclude(actual_reduction_percent=None)
        .select_related("tender", "tender__found_tender")
        .prefetch_related("lines")
    )
    own_scored = [
        (len(t_kws & _estimate_keywords(est)), est) for est in own_pool
    ]
    own_scored = [(overlap, est) for overlap, est in own_scored if overlap]
    own_scored.sort(key=lambda pair: (
        -pair[0],
        0 if (tender.region and _region_of_estimate(pair[1]) == tender.region) else 1,
        -(pair[1].outcome_checked_at.toordinal() if pair[1].outcome_checked_at else 0),
    ))

    chosen = []
    for _, est in own_scored[:_TARGET_COUNT]:
        nmck = (est.summary_snapshot or {}).get("nmck_total")
        chosen.append({
            "source": "own",
            "subject": est.name,
            "region": _region_of_estimate(est),
            "nmck": Decimal(str(nmck)) if nmck else None,
            "final_price": est.actual_price,
            "discount_pct": est.actual_reduction_percent,
            "contract_date": est.outcome_checked_at.date() if est.outcome_checked_at else None,
        })
    own_count = len(chosen)

    remaining = _TARGET_COUNT - own_count
    if remaining > 0:
        cats = tender_categories(tender, card)
        market_pool = ContractStat.objects.filter(law="fz44", shared_purchase=False, discount_pct__isnull=False)
        if cats:
            market_pool = market_pool.filter(category__in=cats)
        market_scored = [
            (len(t_kws & _keywords(row.subject or "")), row)
            for row in market_pool.order_by("-contract_date")[:500]
        ]
        market_scored = [(overlap, row) for overlap, row in market_scored if overlap]
        market_scored.sort(key=lambda pair: (
            -pair[0],
            0 if (tender.region and pair[1].region == tender.region) else 1,
            -(pair[1].contract_date.toordinal() if pair[1].contract_date else 0),
        ))
        for _, row in market_scored[:remaining]:
            chosen.append({
                "source": "market",
                "subject": row.subject,
                "region": row.region,
                "nmck": row.nmck,
                "final_price": row.final_price,
                "discount_pct": row.discount_pct,
                "contract_date": row.contract_date,
            })
    market_count = len(chosen) - own_count

    if len(chosen) < _MIN_SAMPLES:
        return None

    discounts = sorted(float(row["discount_pct"]) for row in chosen)
    median = statistics.median(discounts)

    return {
        "count": len(chosen),
        "own_count": own_count,
        "market_count": market_count,
        "median": round(median),
        "range_lo": round(discounts[0]),
        "range_hi": round(discounts[-1]),
        "suggested_reduction": max(5, min(60, round(median))),
        "same_region": sum(1 for row in chosen if tender.region and row["region"] == tender.region),
        "categories": sorted(tender_categories(tender, card)),
        "examples": chosen,
    }
