"""Оркестрация выгрузки закупок: тянет страницы через gosplan.iter_purchases,
сохраняет FoundTender, пишет журнал PullRun.

Фильтр по цене и категориям задаётся на стороне API; плюс/минус-слова
применяются позже, при показе списка (см. filtering.py).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone

from . import gosplan
from .models import FilterSettings, FoundTender, Organization, PullRun

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# служебные слова в названии организации -> в аббревиатуру / выкинуть
_ORG_ABBR = [
    ("федеральное государственное бюджетное образовательное учреждение высшего образования", "ФГБОУ ВО"),
    ("федеральное государственное бюджетное учреждение", "ФГБУ"),
    ("федеральное государственное казенное учреждение", "ФГКУ"),
    ("государственное бюджетное учреждение здравоохранения", "ГБУЗ"),
    ("государственное бюджетное профессиональное образовательное учреждение", "ГБПОУ"),
    ("государственное бюджетное общеобразовательное учреждение", "ГБОУ"),
    ("государственное бюджетное учреждение", "ГБУ"),
    ("государственное автономное учреждение", "ГАУ"),
    ("муниципальное бюджетное учреждение", "МБУ"),
    ("муниципальное казенное учреждение", "МКУ"),
    ("муниципальное автономное учреждение", "МАУ"),
    ("администрация", "Администрация"),
]

# ОКПД2-группы, смежные с профилем ПСОДИН. Заказчики часто ставят код небрежно —
# берём широкие группы, лучше поймать лишнее, чем упустить нужное.
# (code, человекочитаемая метка) — метки показываются галочками в настройках.
CATEGORY_GROUPS = [
    ("18.1", "Полиграфия и печать"),
    ("58.19", "Печатная продукция (открытки, календари, бланки)"),
    ("17.23", "Канцелярия бумажная"),
    ("32.99", "Прочие изделия (ручки, зонты, брелоки, флешки)"),
    ("32.13", "Значки, бижутерия"),
    ("32.40", "Игры и игрушки"),
    ("22.29", "Пластик (бейджи, магниты, папки)"),
    ("23.13", "Стекло (бокалы, кружки)"),
    ("23.41", "Керамика (кружки, посуда)"),
    ("25.99", "Металлоизделия (фляги, сувениры)"),
    ("13.92", "Готовый текстиль (бельё, шторы, флаги)"),
    ("13.99", "Прочий текстиль"),
    ("14.1", "Одежда (футболки, поло, рубашки)"),
    ("14.19", "Аксессуары одежды (кепки, шарфы, перчатки)"),
    ("14.39", "Трикотаж (джемперы, свитшоты)"),
    ("15.12", "Сумки, чемоданы"),
]
DEFAULT_OKPD2 = [code for code, _ in CATEGORY_GROUPS]

# API принимает не больше 5 кодов classifier за запрос — режем на батчи.
def _chunk(items, size=5):
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def effective_okpd2(settings) -> list[str]:
    return list(settings.okpd2_codes) if settings.okpd2_codes else DEFAULT_OKPD2


def effective_laws(settings) -> list[str]:
    return [law for law in (settings.laws or ["fz44"]) if law in {"fz44", "fz223"}]


# Совместимость со старым кодом/тестами.
TARGETED_OKPD2 = DEFAULT_OKPD2
TARGETED_OKPD2_BATCHES = _chunk(DEFAULT_OKPD2)


def _parse_dt(value):
    """API отдаёт время в UTC без пометки зоны. Возвращаем aware datetime (UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_price(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _record_to_fields(record: dict, pulled_at, law: str) -> dict:
    # 44-ФЗ: customers[] / responsible / collecting_finished_at
    # 223-ФЗ: customer (строка) / placer / submission_close_at
    customers = record.get("customers") or ([record["customer"]] if record.get("customer") else [])
    close = record.get("collecting_finished_at") or record.get("submission_close_at")
    number = (record.get("purchase_number") or "").strip()
    return {
        "object_info": record.get("object_info") or "",
        "title": gosplan.clean_title(record.get("object_info") or ""),
        "max_price": _parse_price(record.get("max_price")),
        "currency_code": record.get("currency_code") or "",
        "customer_inn": customers[0] if customers else "",
        "region": record.get("region"),
        "stage": record.get("stage"),
        "purchase_type": record.get("purchase_type") or "",
        "okpd2": record.get("okpd2") or [],
        "published_at": _parse_dt(record.get("published_at")),
        "collecting_finished_at": _parse_dt(close),
        "eis_url": gosplan.eis_url(number, record.get("purchase_type") or "") if law == "fz44"
        else f"https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber={number}",
        "raw": record,
        "last_pulled_at": pulled_at,
    }


def build_params(*, days: int, stage: int | None, min_price, regions=None, law: str = "fz44") -> dict:
    now_msk = timezone.now().astimezone(MSK)
    since = (now_msk - timedelta(days=days)).replace(microsecond=0)
    params = {
        "sort": "published_at_desc",
        "published_after": since.isoformat(),
        "published_before": now_msk.replace(microsecond=0).isoformat(),
    }
    if stage is not None and law == "fz44":
        params["stage"] = stage
    if min_price is not None:
        params["max_price_ge"] = float(min_price)
    if regions:
        params["region"] = [int(r) for r in regions]
    return params


def run_pull(
    *,
    days: int | None = None,
    stage: int | None = 1,
    min_price=None,
    classifiers=None,
    classifier_batches=None,
    laws=None,
    regions=None,
    max_requests: int = 20,
) -> PullRun:
    settings = FilterSettings.load()
    if days is None:
        days = settings.window_days
    if min_price is None:
        min_price = settings.min_price
    if regions is None:
        regions = settings.regions
    laws = laws or effective_laws(settings)

    if classifier_batches:
        batches = [list(b) for b in classifier_batches]
    elif classifiers:
        batches = _chunk(classifiers)
    else:
        batches = _chunk(effective_okpd2(settings))

    run = PullRun.objects.create(
        started_at=timezone.now(),
        params={"days": days, "min_price": float(min_price or 0), "laws": laws, "regions": list(regions or []),
                "okpd2": [c for b in batches for c in b]},
    )
    stats = {"requests": 0, "records": 0}

    def _on_request(_request_no, got):
        stats["requests"] += 1
        stats["records"] += got

    created = updated = 0
    seen: set[str] = set()
    bases = {law: build_params(days=days, stage=stage, min_price=min_price, regions=regions, law=law) for law in laws}
    # чередуем законы внутри каждого батча — при нехватке лимита оба закона получают поровну
    jobs = [(law, batch) for batch in batches for law in laws]
    try:
        for step, (law, batch) in enumerate(jobs):
            remaining = max_requests - stats["requests"]
            if remaining <= 0:
                break
            if step and stats["requests"]:
                time.sleep(gosplan.THROTTLE_SECONDS)
            params = {**bases[law], "classifier": batch}
            for record in gosplan.iter_purchases(
                params=params, law=law, max_requests=remaining, on_request=_on_request
            ):
                number = (record.get("purchase_number") or "").strip()
                if not number or (law, number) in seen:
                    continue
                seen.add((law, number))
                _, is_created = FoundTender.objects.update_or_create(
                    law=law,
                    purchase_number=number,
                    defaults=_record_to_fields(record, run.started_at, law),
                )
                created += int(is_created)
                updated += int(not is_created)
        run.ok = True
    except gosplan.GosplanError as exc:
        run.error = str(exc)
        run.ok = False

    run.finished_at = timezone.now()
    run.requests_made = stats["requests"]
    run.records_received = stats["records"]
    run.created_count = created
    run.updated_count = updated
    run.duration_seconds = round((run.finished_at - run.started_at).total_seconds(), 1)
    run.save()

    return run


def notification_for(tender, *, force: bool = False) -> dict | None:
    """Извещение по тендеру: из кэша, иначе один запрос к API. Возвращает сырой payload.

    Побочно кладёт имя/город заказчика в кэш Organization — список это подхватит без
    отдельных запросов.
    """
    if tender.law != "fz44":
        return None  # у 223-ФЗ нет разобранного извещения — только файлы
    if tender.notification_raw and not force:
        return tender.notification_raw
    try:
        payload = gosplan.fetch_notification(tender.purchase_number)
    except gosplan.GosplanError:
        return None
    tender.notification_raw = payload
    tender.notification_checked_at = timezone.now()
    tender.save(update_fields=["notification_raw", "notification_checked_at"])

    resp_org = (
        (payload.get("source") or {}).get("purchaseResponsibleInfo") or {}
    ).get("responsibleOrgInfo") or {}
    inn = (resp_org.get("INN") or tender.customer_inn or "").strip()
    if inn and resp_org.get("fullName"):
        org, _ = Organization.objects.get_or_create(inn=inn)
        org.name = _short_org_name(resp_org.get("fullName", ""))
        org.save(update_fields=["name", "checked_at"])
    return payload


_EXTRAS_TTL = timedelta(hours=6)
_EXTRAS_ACTIVE_WINDOW = timedelta(days=45)  # после закрытия приёма новые разъяснения/жалобы ещё возможны


def extras_for(tender, *, force: bool = False) -> tuple[list, list]:
    """(разъяснения, жалобы) для карточки. Только 44-ФЗ.

    Первый заход — всегда запрос (2 обращения к API). Повторный — обновляем, только
    пока закупка «живая» (приём заявок не закрыт или закрыт недавно) и кэш устарел.
    Сбой любого из запросов не роняет страницу — отдаём что было.
    """
    if tender.law != "fz44":
        return [], []

    now = timezone.now()
    checked = tender.extras_checked_at
    if checked is None or force:
        stale = True
    elif now - checked <= _EXTRAS_TTL:
        stale = False
    else:
        deadline = tender.collecting_finished_at
        stale = deadline is None or now - deadline <= _EXTRAS_ACTIVE_WINDOW

    if not stale:
        return tender.clarifications_raw or [], tender.complaints_raw or []

    clar = tender.clarifications_raw or []
    comp = tender.complaints_raw or []
    got_any = False
    try:
        clar = gosplan.fetch_clarifications(tender.purchase_number)
        got_any = True
    except gosplan.GosplanError:
        pass
    try:
        comp = gosplan.fetch_complaints(tender.purchase_number)
        got_any = True
    except gosplan.GosplanError:
        pass

    if got_any:
        tender.clarifications_raw = clar
        tender.complaints_raw = comp
        tender.extras_checked_at = now
        tender.save(update_fields=["clarifications_raw", "complaints_raw", "extras_checked_at"])
    return clar, comp


def push_to_estimate(tender, user):
    """Создать просчёт в «Расчёте тендеров» из позиций извещения. Возвращает id просчёта."""
    from decimal import Decimal as _D

    from tenders.models import TenderEstimate, TenderLine

    from .notification import parse_notification
    from .stats import price_stats_for

    card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    cust = (card or {}).get("customer", {})
    # tenders показывает "№ {tender_number} — {name}", поэтому name = только заказчик
    customer = (cust.get("short_name") or "").split(",")[0].strip() \
        or _short_org_name(cust.get("name", "")) \
        or (Organization.objects.filter(inn=tender.customer_inn).values_list("name", flat=True).first() or "") \
        or (f"ИНН {tender.customer_inn}" if tender.customer_inn else "заказчик не распознан")

    stats = price_stats_for(tender, card)
    reduction = _D(f"{stats['suggested_reduction']}.00") if stats else _D("30.00")
    snapshot = {"is_incomplete": True}
    if stats:
        snapshot["price_stats"] = {
            "median": stats["median"],
            "range_lo": stats["range_lo"],
            "range_hi": stats["range_hi"],
            "count": stats["count"],
            "categories": stats["categories"],
        }

    estimate = TenderEstimate.objects.create(
        owner=user,
        tender_number=tender.purchase_number[:100],
        name=customer[:300],
        reduction_percent=reduction,
        summary_snapshot=snapshot,
    )

    lines = []
    for i, item in enumerate((card or {}).get("items", [])):
        chars = [c for c in (item.get("characteristics") or []) if c.get("name") or c.get("value")]
        requirements = {
            "requirements": [
                {"label": c.get("name") or "Характеристика", "value": c.get("value", ""), "source": "из извещения"}
                for c in chars
            ],
            "missing": [],
            "questions": [],
            "source_name": item.get("name", ""),
        }
        code = "; ".join(c for c in [item.get("code"), item.get("code_name")] if c)
        lines.append(TenderLine(
            estimate=estimate, sort_order=i,
            name=(item.get("name") or "Позиция")[:500],
            quantity=_parse_price(item.get("quantity")) or _D("0"),
            nmck_unit=_parse_price(item.get("price")) or _D("0"),
            comment=code[:500],
            requirements=requirements if chars else {},
        ))
    if not lines:
        lines.append(TenderLine(
            estimate=estimate, sort_order=0, name=(tender.title or tender.object_info)[:500],
            quantity=_D("0"), nmck_unit=_parse_price(tender.max_price) or _D("0"),
            comment="Позиции не разобраны — см. файлы извещения", requirements={},
        ))
    TenderLine.objects.bulk_create(lines)

    tender.status = FoundTender.PUSHED
    tender.pushed_estimate_id = estimate.pk
    tender.save(update_fields=["status", "pushed_estimate_id"])
    return estimate.pk


def _short_org_name(full_name: str) -> str:
    text = " ".join((full_name or "").split())
    low = text.lower()
    for phrase, abbr in _ORG_ABBR:
        if phrase in low:
            rest = text[low.index(phrase) + len(phrase):].strip(' "«»')
            return f"{abbr} {rest}".strip() if rest else abbr
    return text


def _org_fields(source: dict, law: str) -> dict:
    """Нормализуем ответ реестра организаций (у 44 и 223 разная форма)."""
    source = source or {}
    if law == "fz223":
        main = source.get("mainInfo") or {}
        contact = source.get("contactInfo") or {}
        return {
            "name": _short_org_name(main.get("fullName", "")),
            "short_name": main.get("shortName", ""),
            "city": "",
            "region_name": (main.get("region") or "").title(),
            "address": main.get("postalAddress") or main.get("legalAddress") or "",
            "email": contact.get("contactEmail", ""),
            "website": contact.get("website", ""),
        }
    address = source.get("factualAddress") or {}
    contact = source.get("responsibleInfo") or {}
    person = contact.get("contactPersonInfo") or {}
    return {
        "name": _short_org_name(source.get("fullName", "")),
        "short_name": (source.get("shortName") or "").split(",")[0].strip(),
        "city": (address.get("city") or {}).get("fullName", ""),
        "region_name": (address.get("region") or {}).get("fullName", ""),
        "address": source.get("factAddress") or source.get("postalAddress") or "",
        "email": source.get("email", ""),
        "website": "",
    }


def enrich_one_org(inn: str, law: str) -> Organization | None:
    inn = (inn or "").strip()
    if not inn:
        return None
    try:
        source = gosplan.fetch_organization(inn, law)
    except gosplan.GosplanError:
        return None
    org, _ = Organization.objects.update_or_create(inn=inn, defaults=_org_fields(source, law))
    return org


def enrich_organizations(limit: int = 20) -> int:
    """Подтянуть карточки заказчиков по ИНН, которых ещё нет в кэше. Один запрос на ИНН."""
    known = set(Organization.objects.values_list("inn", flat=True))
    todo, seen = [], set()
    for law, inn in FoundTender.objects.exclude(customer_inn="").values_list("law", "customer_inn"):
        inn = (inn or "").strip()
        if inn and inn not in known and inn not in seen:
            seen.add(inn)
            todo.append((law, inn))
        if len(todo) >= limit:
            break

    saved = 0
    for index, (law, inn) in enumerate(todo):
        if index:
            time.sleep(gosplan.THROTTLE_SECONDS)
        try:
            source = gosplan.fetch_organization(inn, law)
        except gosplan.RateLimitError:
            break
        except gosplan.GosplanError:
            continue
        Organization.objects.update_or_create(inn=inn, defaults=_org_fields(source, law))
        saved += 1
    return saved
