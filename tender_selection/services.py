"""Оркестрация выгрузки закупок: тянет страницы через gosplan.iter_purchases,
сохраняет Tender, пишет журнал PullRun.

Фильтр по цене и категориям задаётся на стороне API; плюс/минус-слова
применяются позже, при показе списка (см. filtering.py).
"""
from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.db import transaction

import os

from . import gosplan
from .documents import DocumentError, fetch_document
from .eis_docs import EisDocsError, fetch_document_via_eis
from .filtering import match_title, parse_terms
from .models import ContractStat, FilterSettings, Organization, PullRun, Tender

logger = logging.getLogger(__name__)

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


def tender_anchor_for(law: str, purchase_number: str) -> Tender:
    """Вернуть единый Tender для закупки независимо от её текущей стадии."""
    return Tender.objects.get_or_create(law=law, purchase_number=purchase_number)[0]


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


def _risk_eligible(tender, settings, include, exclude) -> bool:
    return (
        tender.law == "fz44"
        # риск не считаем, пока тендер не дошёл до «Проверки» — на «Входящих»
        # ещё не решили, что он вообще стоит внимания
        and tender.review != Tender.UNREVIEWED
        and (not settings.min_price or tender.max_price is None or tender.max_price >= settings.min_price)
        and (tender.collecting_finished_at is None or tender.collecting_finished_at >= timezone.now())
        and match_title(tender.title or tender.object_info, include, exclude)[0]
    )


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
                with transaction.atomic():
                    tender, is_created = Tender.objects.update_or_create(
                        law=law,
                        purchase_number=number,
                        defaults={**_record_to_fields(record, run.started_at, law), "source": Tender.EIS},
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


def _fetch_doc_bytes(tender, url, name, *, timeout=None, archive_timeout=None, direct_timeout=15):
    """Официальный канал ЕИС первым (сеть с сервера есть), прямая ссылка — подстраховка
    на случай проблем с токеном/лимитом. Общее для просмотра, захода внутрь архива и
    оценки рисков — каждый раз нужен весь файл заново, кэшируется только итог разбора.

    По умолчанию — обычные таймауты (интерактивный просмотр документа, где важнее
    дождаться реального ответа). Автооценка риска зовёт с timeout/archive_timeout
    покороче — там важнее быстро понять, что ЕИС недоступен, и уйти в аварийный режим."""
    eis_kwargs = {}
    if timeout is not None:
        eis_kwargs["timeout"] = timeout
    if archive_timeout is not None:
        eis_kwargs["archive_timeout"] = archive_timeout
    try:
        return fetch_document_via_eis(tender.purchase_number, name, **eis_kwargs)
    except EisDocsError as eis_exc:
        try:
            return fetch_document(url, timeout=direct_timeout)
        except DocumentError as direct_exc:
            raise DocumentError(f"{eis_exc} Прямая ссылка тоже не сработала: {direct_exc}") from direct_exc


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
        # Помечаем попытку даже на неудаче — иначе «нет данных» в списке (см.
        # notification_missing в views.py) не отличить от «карточку ещё никто не
        # открывал»: тендер только что выгружен и извещение для него попросту
        # никогда не запрашивалось, это не сбой API.
        tender.notification_checked_at = timezone.now()
        tender.save(update_fields=["notification_checked_at"])
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


def retry_pending_documents(*, limit: int = 5, recent: int = 50) -> tuple[int, int]:
    """Фоновая попытка докачать документы извещений, ещё не попавшие в кэш предпросмотра.

    Сеть с прод-сервера до zakupki.gov.ru нестабильна (похоже на плавающую блокировку
    ТСПУ — то отвечает, то нет), поэтому вместо однократной попытки по клику пробуем
    периодически, тихо, небольшими порциями. Каждый успех сразу доступен в карточке
    (клик «Просмотр» видит уже готовый кэш) — без этого фон бесполезен.

    Возвращает (сколько документов пробовали, сколько удалось). Смотрим только среди
    недавно выгруженных тендеров 44-ФЗ — старые уже не актуальны.
    """
    from .documents import DocumentError, extract_preview, fetch_document
    from .eis_docs import EisDocsError, fetch_document_via_eis
    from .models import DocumentPreview
    from .notification import parse_notification

    attempted = 0
    succeeded = 0
    tenders = (
        Tender.objects.filter(law="fz44")
        .exclude(notification_raw={})
        .order_by("-last_pulled_at")[:recent]
    )
    for tender in tenders:
        if attempted >= limit:
            break
        card = parse_notification(tender.notification_raw)
        for doc in card.get("documents", []):
            if attempted >= limit:
                break
            url, name = doc.get("url", ""), doc.get("name", "")
            if not url or DocumentPreview.objects.filter(url=url).exists():
                continue
            attempted += 1
            try:
                data = fetch_document_via_eis(tender.purchase_number, name)
            except EisDocsError:
                try:
                    data = fetch_document(url, timeout=15)
                except DocumentError:
                    continue  # сетевой сбой — не кэшируем, попробуем в следующий тик
            result = extract_preview(data, name)
            DocumentPreview.objects.update_or_create(url=url, defaults={
                "filename": name, "kind": result.get("kind", ""),
                "html": result.get("html", ""), "error": result.get("error", ""),
            })
            succeeded += 1
    return attempted, succeeded


def retry_pending_notifications(*, limit: int = 10, recent: int = 300) -> tuple[int, int]:
    """Фоновая догрузка извещений для свежих 44-ФЗ тендеров, у которых ещё не было ни
    одной попытки. Без этого шага notification_for() вызывается только по клику
    «Открыть тендер» — новый тендер после выгрузки так и остаётся без извещения (товары,
    документы) неопределённо долго, а бейдж «⚠ нет данных» в списке (notification_missing,
    views.py) видит это как «попытка была и провалилась», хотя попытки не было вовсе.
    Идёт мелкими порциями по тому же паттерну, что retry_pending_documents.

    Возвращает (сколько тендеров пробовали, сколько удалось)."""
    attempted = succeeded = 0
    tenders = (
        Tender.objects.filter(law="fz44", notification_checked_at__isnull=True)
        .order_by("-last_pulled_at")[:recent]
    )
    for tender in tenders:
        if attempted >= limit:
            break
        attempted += 1
        if notification_for(tender):
            succeeded += 1
    return attempted, succeeded


def retry_pending_risks(*, limit: int = 3) -> tuple[int, int]:
    """Досчитать риск для тендеров, уже отправленных «На оценку рисков»
    (review != unreviewed), но ещё не оценённых — как раз это и есть
    автозапуск на стадии «Проверка». Раньше здесь же было ограничение
    «тендер найден не позже 2 дней назад» (для другой задачи — досчитать
    после сбоя загрузки извещения) — оно тихо исключало любой тендер,
    который кто-то review'нул позже второго дня, а по-настоящему бывает
    почти всегда. Само по себе review != unreviewed уже достаточно редкий
    и осознанный фильтр — возрастное ограничение было лишним."""
    settings = FilterSettings.load()
    include = parse_terms(settings.include_words)
    exclude = parse_terms(settings.exclude_words)
    now = timezone.now()
    attempted = succeeded = 0
    tenders = Tender.objects.filter(
        law="fz44", risk_checked_at__isnull=True,
    ).exclude(review=Tender.UNREVIEWED).order_by("first_seen_at")[:300]
    for tender in tenders:
        if attempted >= limit:
            break
        if tender.risk_assessment or not _risk_eligible(tender, settings, include, exclude):
            continue
        if (not tender.notification_raw and tender.notification_checked_at and
                tender.notification_checked_at > now - timedelta(minutes=30)):
            continue
        attempted += 1
        try:
            if notification_for(tender, force=bool(tender.notification_checked_at and not tender.notification_raw)):
                succeeded += bool(risk_assessment_for(tender))
        except Exception:
            logger.exception("Risk assessment retry failed for tender %s", tender.purchase_number)
    return attempted, succeeded


_EXPIRED_INCOMING_TTL = timedelta(days=7)
def purge_stale() -> dict:
    """Фоновая уборка «Входящих» — навсегда удаляет:
    - просроченные «Входящие» (срок подачи истёк более недели назад, тендер
      так и не был переведён «в работу») — они больше никому не нужны.

    Архив не очищается: скрытые карточки можно восстановить."""

    now = timezone.now()
    expired_incoming, _ = Tender.objects.filter(
        status=Tender.NEW, review=Tender.UNREVIEWED,
        collecting_finished_at__lt=now - _EXPIRED_INCOMING_TTL,
    ).delete()
    return {
        "expired_incoming": expired_incoming, "archived_found": 0,
        "archived_estimates": 0,
    }


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


def risk_assessment_for(tender, *, force: bool = False) -> dict | None:
    """Оценка рисков тендера по вложенным документам (см. risk_assessment.py):
    срок исполнения, обеспечение, ст.96 44-ФЗ, штрафы, нацрежим, образцы + свободный текст.

    Из кэша, иначе один запрос к ИИ-шлюзу (с одним внутренним повтором при неполном
    ответе — см. risk_assessment.assess). Только 44-ФЗ и только если извещение уже
    загружено (нужен список документов) — иначе сначала notification_for(tender).
    Сбой не роняет карточку: отмечаем попытку и сохраняем причину в risk_error.
    """
    if tender.law != "fz44":
        return None
    if tender.risk_assessment and "risk_factors" in tender.risk_assessment and not force:
        return tender.risk_assessment
    if not tender.notification_raw:
        return None

    from .notification import parse_notification
    from .risk_assessment import RiskAssessmentError, assess, build_context, select_documents

    card = parse_notification(tender.notification_raw)
    documents = select_documents(card.get("documents") or [])
    if not documents:
        tender.risk_checked_at = timezone.now()
        tender.risk_error = "В извещении нет подходящих документов (проект контракта/ТЗ/описание)."
        tender.save(update_fields=["risk_checked_at", "risk_error"])
        return None

    # Короткий таймаут (5с на попытку — ЕИС либо отвечает сразу, либо не отвечает вовсе):
    # автооценке важнее быстро понять, что документы недоступны, и уйти в аварийный
    # режим, чем ждать десятки секунд по умолчанию (как в интерактивном просмотре).
    context, used_names = build_context(
        tender, card, documents,
        fetch=lambda url, name: _fetch_doc_bytes(tender, url, name, timeout=5, archive_timeout=5, direct_timeout=5),
    )
    if not used_names:
        # Документы не прочитались (типично — локальная сеть не видит ЕИС, только
        # прод) — аварийный режим: контекст всё равно не пуст (build_context кладёт
        # туда сводку извещения всегда), поэтому пробуем оценку по одним только
        # структурным данным извещения, явно помечая её как менее надёжную —
        # не подменяем молча настоящую оценку по документам.
        try:
            result = assess(context)
        except RiskAssessmentError as exc:
            tender.risk_checked_at = timezone.now()
            tender.risk_error = f"Документы недоступны, аварийная оценка тоже не удалась: {exc}"
            tender.save(update_fields=["risk_checked_at", "risk_error"])
            return None
        data = dict(result["data"])
        from .risk_policy import classify_risk

        data.update(classify_risk(
            data.get("risk_facts"),
            warning_days=FilterSettings.load().risk_warning_days,
            critical_days=FilterSettings.load().risk_critical_days,
        ))
        data["degraded"] = True
        tender.risk_assessment = data
        tender.risk_assessment_docs = []
        tender.risk_checked_at = timezone.now()
        tender.risk_error = ""
        tender.save(update_fields=["risk_assessment", "risk_assessment_docs", "risk_checked_at", "risk_error"])
        return tender.risk_assessment

    try:
        result = assess(context)
    except RiskAssessmentError as exc:
        tender.risk_checked_at = timezone.now()
        tender.risk_error = str(exc)
        tender.save(update_fields=["risk_checked_at", "risk_error"])
        return None

    from .risk_policy import classify_risk

    data = dict(result["data"])
    settings = FilterSettings.load()
    policy = classify_risk(
        data.get("risk_facts"),
        warning_days=settings.risk_warning_days,
        critical_days=settings.risk_critical_days,
    )
    data.update(policy)
    tender.risk_assessment = data
    tender.risk_assessment_docs = used_names
    tender.risk_checked_at = timezone.now()
    tender.risk_error = ""
    tender.save(update_fields=["risk_assessment", "risk_assessment_docs", "risk_checked_at", "risk_error"])
    return tender.risk_assessment


def start_risk_assessment_in_background(tender_id: int) -> None:
    """«Включить автооценку риска при переносе тендера в статус оценки» в буквальном
    смысле: срабатывает СРАЗУ в момент перехода (вызывается из set_review), не через
    фоновый тик по расписанию. Сам вызов может идти десятки секунд (сеть до ЕИС) —
    поэтому в отдельном потоке, чтобы не задерживать ответ на клик «На оценку рисков».

    Разовый поток, не персистентный пул: постоянный ThreadPoolExecutor (см. такой же
    у ассистента маршрутов, tenders/views.py) переиспользует соединение с БД между
    запусками — под Postgres это однажды привело к «the connection is closed» между
    тестами. Здесь поток живёт ровно один вызов и закрывается вместе с соединением.

    retry_pending_risks() в фоновом демоне остаётся как подстраховка (см. scheduler.py)
    на случай, если этот разовый запуск не удался или процесс перезапустился до того,
    как он успел закончить — не основной путь, а сеть безопасности."""
    import threading

    from django.db import close_old_connections

    def _job():
        close_old_connections()
        try:
            tender = Tender.objects.get(pk=tender_id)
            risk_assessment_for(tender)
        except Exception:
            logger.exception("Фоновая оценка риска не удалась для тендера %s", tender_id)
        finally:
            close_old_connections()

    threading.Thread(target=_job, daemon=True).start()


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
        tender=tender,
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

    tender.status = Tender.PUSHED
    tender.save(update_fields=["status"])
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
    for law, inn in Tender.objects.exclude(customer_inn="").values_list("law", "customer_inn"):
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


def fetch_tender_outcome(estimate) -> dict:
    """Забрать факт торгов по номеру закупки через реестр контрактов ГосПлан.

    Не решает само, выиграли мы или нет — это по умолчанию неизвестно без
    настроенного COMPANY_INN (свой ИНН нигде в проекте раньше не хранился).
    Если задан — сравнивает с суплаерами найденного контракта и возвращает
    auto_status; если нет — возвращает найденную цену/снижение, а решение
    «выиграли/проиграли» остаётся за администратором (см. enter_outcome).
    """
    from tenders.models import TenderEstimate

    try:
        rows = gosplan.fetch_contracts({"purchase_number": estimate.tender_number, "limit": 5})
    except gosplan.GosplanError:
        return {"found": False}
    if not rows:
        return {"found": False}
    row = rows[0]
    price = row.get("price")
    result: dict = {"found": True, "price": price, "suppliers": [str(s) for s in (row.get("suppliers") or [])]}

    nmck_total = (estimate.summary_snapshot or {}).get("nmck_total")
    if price is not None and nmck_total:
        try:
            reduction = (Decimal(str(nmck_total)) - Decimal(str(price))) / Decimal(str(nmck_total)) * 100
            result["reduction_percent"] = reduction.quantize(Decimal("0.01"))
        except (InvalidOperation, ZeroDivisionError):
            pass

    company_inn = os.getenv("COMPANY_INN", "").strip()
    if company_inn and price is not None:
        result["auto_status"] = TenderEstimate.WON if company_inn in result["suppliers"] else TenderEstimate.LOST
    return result


def retry_pending_outcomes(*, limit: int = 5) -> tuple[int, int]:
    """Фоновая попытка забрать факт торгов для просчётов «В ожидании», у которых
    итог ещё не внесён — тот же ГосПлан-запрос, что и ручная кнопка «Забрать итог
    автоматически» на странице тендера, просто без захода туда. Сама решает
    выиграли/проиграли только если настроен COMPANY_INN (см. fetch_tender_outcome) —
    иначе оставляет цену/снижение как есть, а решение по-прежнему за администратором.
    Идёт мелкими порциями по тому же паттерну, что retry_pending_documents/_risks."""
    from tenders.models import TenderEstimate

    attempted = succeeded = 0
    estimates = TenderEstimate.objects.filter(
        status=TenderEstimate.PENDING, outcome_checked_at__isnull=True,
    ).order_by("updated_at")[:50]
    for estimate in estimates:
        if attempted >= limit:
            break
        attempted += 1
        try:
            outcome = fetch_tender_outcome(estimate)
        except Exception:
            logger.exception("Outcome retry failed for estimate %s", estimate.tender_number)
            continue
        if not outcome.get("found"):
            continue
        if outcome.get("auto_status"):
            apply_tender_outcome(
                estimate, status=outcome["auto_status"], price=outcome.get("price"),
                reduction_percent=outcome.get("reduction_percent"), source=TenderEstimate.OUTCOME_AUTO,
            )
        else:
            estimate.actual_price = outcome.get("price")
            estimate.actual_reduction_percent = outcome.get("reduction_percent")
            estimate.outcome_checked_at = timezone.now()
            estimate.save(update_fields=["actual_price", "actual_reduction_percent", "outcome_checked_at"])
        succeeded += 1
    return attempted, succeeded


def apply_tender_outcome(estimate, *, status, price=None, reduction_percent=None, source) -> None:
    """Записать факт торгов на просчёт; при победе — отметить в ContractStat.is_ours,
    чтобы своя история наконец начала накапливаться (поле раньше нигде не писалось).

    «Результат» фиксирует итог торгов. Карточка остаётся на этой стадии,
    пока пользователь явно не скроет её в архив."""
    from tenders.models import TenderEstimate

    estimate.status = status
    if price is not None:
        estimate.actual_price = price
    if reduction_percent is not None:
        estimate.actual_reduction_percent = reduction_percent
    estimate.outcome_checked_at = timezone.now()
    estimate.outcome_source = source
    estimate.save(update_fields=[
        "status", "actual_price", "actual_reduction_percent", "outcome_checked_at", "outcome_source",
    ])

    if status == TenderEstimate.WON:
        ContractStat.objects.update_or_create(
            law="fz44", purchase_number=estimate.tender_number,
            defaults={
                "final_price": price, "discount_pct": reduction_percent,
                "is_ours": True, "contract_date": timezone.now().date(),
            },
        )
