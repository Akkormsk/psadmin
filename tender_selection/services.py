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
from django.db.models import Q

import os

from . import gosplan
from .documents import DocumentError, extract_preview, fetch_document
from .eis_docs import EisDocsError, fetch_document_via_eis
from .models import ContractStat, DocumentPreview, FilterSettings, Organization, PullRun, Tender

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


def _risk_eligible(tender) -> bool:
    # Плюс-слова и минимальную цену не проверяем: тендер уже вручную переведён в «Оценку».
    return (
        tender.law == "fz44"
        # риск не считаем, пока тендер не дошёл до «Проверки» — на «Входящих»
        # ещё не решили, что он вообще стоит внимания
        and tender.review != Tender.UNREVIEWED
        and (tender.collecting_finished_at is None or tender.collecting_finished_at >= timezone.now())
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


def _store_preview(url, name, data: bytes) -> str:
    result = extract_preview(data, name)
    DocumentPreview.objects.update_or_create(url=url, defaults={
        "filename": name, "kind": result.get("kind", ""),
        "html": result.get("html", ""), "error": result.get("error", ""),
    })
    return result.get("html", "")


def document_html(tender, url, name, **fetch_kwargs) -> str:
    """Разобранный документ извещения: из кэша предпросмотра, иначе с ЕИС — и сразу в кэш,
    чтобы следующий просмотр или оценка рисков не ходили в ЕИС повторно.
    Скачанный, но нечитаемый файл (архив, битый .doc) тоже в кэше — с пустым HTML,
    повторно за ним не ходим. DocumentError — документ сейчас не получить."""
    cached = DocumentPreview.objects.filter(url=url).first()
    if cached:
        return cached.html
    return _store_preview(url, name, _fetch_doc_bytes(tender, url, name, **fetch_kwargs))


def documents_html(tender, documents: list[dict], **fetch_kwargs) -> dict[str, str]:
    """{url: HTML} для нескольких документов: кэш, остальное с ЕИС параллельно.
    В потоках только сеть — база читается и пишется в вызывающем потоке."""
    from concurrent.futures import ThreadPoolExecutor

    htmls = dict(
        DocumentPreview.objects.filter(url__in=[doc["url"] for doc in documents]).values_list("url", "html")
    )
    missing = [doc for doc in documents if doc["url"] not in htmls]

    def fetch_one(doc):
        try:
            return doc, _fetch_doc_bytes(tender, doc["url"], doc["name"], **fetch_kwargs)
        except DocumentError:
            return doc, None

    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            fetched = list(pool.map(fetch_one, missing))
        for doc, data in fetched:
            if data is not None:
                htmls[doc["url"]] = _store_preview(doc["url"], doc["name"], data)
    return htmls


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


DOCUMENT_PREFETCH_PAUSE_SECONDS = 10


def retry_pending_documents(*, limit: int = 8, pause: float = DOCUMENT_PREFETCH_PAUSE_SECONDS) -> tuple[int, int]:
    """Фоновая докачка документов, нужных оценке рисков (проект контракта, ТЗ/описание),
    у тендеров с ещё открытым приёмом заявок — ближайший срок подачи первым. К моменту
    перевода в «Оценку» документы уже в кэше, и оценка не зависит от того, отвечает ли
    ЕИС прямо сейчас. Пауза между документами — ЕИС сбрасывает соединения при частых
    запросах с одного IP.

    Возвращает (сколько документов пробовали, сколько удалось)."""
    from .notification import parse_notification
    from .risk_assessment import select_documents

    tenders = (
        Tender.objects.filter(law="fz44", collecting_finished_at__gte=timezone.now())
        .exclude(status=Tender.DISMISSED).exclude(notification_raw={})
        .order_by("collecting_finished_at")
    )
    attempted = succeeded = 0
    for tender in tenders.iterator():
        docs = [
            doc for doc in select_documents(parse_notification(tender.notification_raw).get("documents", []))
            if doc.get("url") and not DocumentPreview.objects.filter(url=doc["url"]).exists()
        ]
        for doc in docs:
            if attempted >= limit:
                return attempted, succeeded
            if attempted:
                time.sleep(pause)
            attempted += 1
            try:
                document_html(tender, doc["url"], doc["name"])
                succeeded += 1
            except DocumentError:
                continue  # сетевой сбой — не кэшируем, попробуем в следующий тик
    return attempted, succeeded


NOTIFICATION_RETRY_COOLDOWN = timedelta(hours=2)


def retry_pending_notifications(*, limit: int = 10, recent: int = 300) -> tuple[int, int]:
    """Фоновая догрузка извещений для свежих 44-ФЗ тендеров, у которых ещё не было ни
    одной попытки. Без этого шага notification_for() вызывается только по клику
    «Открыть тендер» — новый тендер после выгрузки так и остаётся без извещения (товары,
    документы) неопределённо долго, а бейдж «⚠ нет данных» в списке (notification_missing,
    views.py) видит это как «попытка была и провалилась», хотя попытки не было вовсе.
    Идёт мелкими порциями по тому же паттерну, что retry_pending_documents.

    Возвращает (сколько тендеров пробовали, сколько удалось)."""
    now = timezone.now()
    never_checked = Q(notification_checked_at__isnull=True)
    # Бесплатный ГосПлан часто отвечает 429 — неудачную попытку повторяем, пока приём заявок открыт.
    failed_earlier = Q(
        notification_raw={},
        notification_checked_at__lt=now - NOTIFICATION_RETRY_COOLDOWN,
        collecting_finished_at__gte=now,
    )
    attempted = succeeded = 0
    tenders = (
        Tender.objects.filter(never_checked | failed_earlier, law="fz44")
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
    now = timezone.now()
    attempted = succeeded = 0
    tenders = Tender.objects.filter(
        law="fz44", risk_checked_at__isnull=True,
    ).exclude(review=Tender.UNREVIEWED).order_by("first_seen_at")[:300]
    for tender in tenders:
        if attempted >= limit:
            break
        if tender.risk_assessment or not _risk_eligible(tender):
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


def purge_stale() -> dict:
    """Фоновая уборка «Входящих» — навсегда удаляет:
    - просроченные «Входящие» (срок подачи истёк более недели назад, тендер
      так и не был переведён «в работу») — они больше никому не нужны.

    Архив не очищается: скрытые карточки можно восстановить."""

    from .filtering import match_title, parse_terms
    from .models import IncomingTrace

    now = timezone.now()
    settings = FilterSettings.load()
    expired = Tender.objects.filter(
        status=Tender.NEW, review=Tender.UNREVIEWED,
        collecting_finished_at__lt=now - timedelta(days=settings.incoming_ttl_days),
    )
    include, exclude = parse_terms(settings.include_words), parse_terms(settings.exclude_words)
    traces = []
    for tender in expired.only("law", "purchase_number", "title", "object_info", "okpd2", "max_price", "opened_at"):
        passes, hits = match_title(tender.title or tender.object_info, include, exclude)
        traces.append(IncomingTrace(
            law=tender.law, purchase_number=tender.purchase_number, title=tender.title or tender.object_info,
            okpd2=tender.okpd2, max_price=tender.max_price, plus_hits=hits,
            filtered_out=not passes, was_opened=tender.opened_at is not None,
        ))
    # След — для аудита плюс/минус-слов: без него «не наши» тендеры исчезают бесследно.
    with transaction.atomic():
        IncomingTrace.objects.bulk_create(traces)
        expired_incoming, _ = expired.delete()
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


def _classify(facts) -> dict:
    from .risk_policy import classify_risk

    settings = FilterSettings.load()
    return classify_risk(
        facts, warning_days=settings.risk_warning_days, critical_days=settings.risk_critical_days,
        levels=settings.risk_factor_levels,
    )


def _documents_sufficient(assessment) -> bool:
    return str(((assessment or {}).get("risk_facts") or {}).get("documents_sufficient")).lower() == "true"


def _keeps_previous_assessment(tender, new_data) -> bool:
    """Повторная оценка, у которой ЕИС не отдал документы, не должна затирать полную."""
    if _documents_sufficient(tender.risk_assessment) and not _documents_sufficient(new_data):
        logger.warning("risk: %s — новая оценка без документов, оставлена прежняя полная", tender.purchase_number)
        return True
    return False


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
    htmls = documents_html(tender, documents, timeout=5, archive_timeout=5, direct_timeout=5)
    context, used_names = build_context(card, documents, htmls)
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

        data.update(_classify(data.get("risk_facts")))
        data["degraded"] = True
        if _keeps_previous_assessment(tender, data):
            return tender.risk_assessment
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
    policy = _classify(data.get("risk_facts"))
    data.update(policy)
    if _keeps_previous_assessment(tender, data):
        return tender.risk_assessment
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
    from tenders.models import TenderSettings

    reduction = (
        _D(f"{stats['suggested_reduction']}.00") if stats
        else TenderSettings.objects.get_or_create(pk=1)[0].default_reduction_percent
    )
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


# «Итог опубликован» ждёт контракта с ИНН победителя: это дни, чаще проверять незачем.
CONTRACT_RECHECK = timedelta(hours=6)


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
        Q(status=TenderEstimate.PENDING, outcome_checked_at__isnull=True)
        | Q(status=TenderEstimate.PUBLISHED, outcome_checked_at__lt=timezone.now() - CONTRACT_RECHECK),
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
        if estimate.status == TenderEstimate.PUBLISHED:
            succeeded += _reconcile_published_with_contract(estimate, outcome)
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


def _reconcile_published_with_contract(estimate, outcome: dict) -> bool:
    """Протокол вышел, но нашу заявку не опознали — решаем по ИНН победителя в контракте.
    Цену и снижение оставляем из протокола: они уже посчитаны от НМЦК закупки."""
    from tenders.models import TenderEstimate

    if not outcome.get("auto_status"):
        estimate.outcome_checked_at = timezone.now()
        estimate.save(update_fields=["outcome_checked_at"])
        return False
    apply_tender_outcome(
        estimate, status=outcome["auto_status"],
        price=estimate.actual_price if estimate.actual_price is not None else outcome.get("price"),
        reduction_percent=(
            estimate.actual_reduction_percent if estimate.actual_reduction_percent is not None
            else outcome.get("reduction_percent")
        ),
        source=TenderEstimate.OUTCOME_AUTO,
    )
    return True


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


PROTOCOL_RECHECK = timedelta(minutes=30)
# Уже завершённые (итог внесён вручную) — дозаполняем протоколом для статистики, но не чаще раза в сутки.
PROTOCOL_BACKFILL_RECHECK = timedelta(days=1)
PROTOCOL_PAUSE_SECONDS = 20


def reduction_percent_from(nmck, price):
    try:
        return ((Decimal(str(nmck)) - price) / Decimal(str(nmck)) * 100).quantize(Decimal("0.01"))
    except (InvalidOperation, ZeroDivisionError, TypeError):
        return None


def _decide_status_from_protocol(estimate) -> None:
    """Опознали свою заявку — «Выигран»/«Проигран», нет — «Итог опубликован».
    Решения, принятые вручную, и «Не участвовали» не трогаем."""
    from tenders.models import TenderEstimate

    from . import protocols

    decidable = estimate.status in (TenderEstimate.PENDING, TenderEstimate.PUBLISHED) or (
        estimate.status in (TenderEstimate.WON, TenderEstimate.LOST)
        and estimate.outcome_source == TenderEstimate.OUTCOME_AUTO
    )
    if not decidable or not estimate.protocol:
        return
    ours = protocols.find_ours(estimate.protocol, bid_number=estimate.bid_number, bid_price=estimate.bid_price)
    if ours:
        status = TenderEstimate.WON if ours.get("rank") == 1 and not ours.get("rejected") else TenderEstimate.LOST
    else:
        status = TenderEstimate.PUBLISHED
    apply_tender_outcome(
        estimate, status=status, price=estimate.actual_price,
        reduction_percent=estimate.actual_reduction_percent, source=TenderEstimate.OUTCOME_AUTO,
    )


def check_protocol(estimate) -> bool:
    """Забрать итоговый протокол из ЕИС и применить к расчёту. False — протокола ещё нет.
    ProtocolError (сеть/ЕИС) пробрасывается: вызывающий решает, продолжать ли."""
    from . import protocols

    estimate.protocol_checked_at = timezone.now()
    protocol = protocols.fetch_protocol(estimate.tender.eis_url if estimate.tender_id else "")
    if not protocol:
        estimate.save(update_fields=["protocol_checked_at"])
        return False
    estimate.protocol = protocol
    update_fields = ["protocol", "protocol_checked_at"]
    win = protocols.winner(protocol)
    if win and win.get("price"):
        price = Decimal(win["price"])
        nmck = protocol.get("nmck") or (estimate.tender.max_price if estimate.tender_id else None)
        estimate.actual_price = price
        estimate.actual_reduction_percent = reduction_percent_from(nmck, price)
        update_fields += ["actual_price", "actual_reduction_percent"]
    estimate.save(update_fields=update_fields)
    _decide_status_from_protocol(estimate)
    return True


def set_our_bid(estimate, *, bid_number: str, bid_price) -> None:
    estimate.bid_number = (bid_number or "").strip()[:40]
    estimate.bid_price = bid_price
    estimate.save(update_fields=["bid_number", "bid_price"])
    _decide_status_from_protocol(estimate)


def retry_pending_protocols(*, limit: int = 3, pause: float = PROTOCOL_PAUSE_SECONDS) -> tuple[int, int]:
    """Фоновая проверка протоколов: «Торги» после окончания подачи заявок — каждые
    полчаса; уже завершённые без протокола — раз в сутки (для статистики снижения).
    На первом же сбое ЕИС останавливаемся до следующего тика — частые запросы с
    одного IP ЕИС наказывает сбросом соединений для всего сервера."""
    from tenders.models import TenderEstimate

    from .protocols import ProtocolError

    now = timezone.now()
    base = TenderEstimate.objects.filter(protocol={}, tender__law="fz44").select_related("tender")
    bidding = base.filter(
        Q(protocol_checked_at__isnull=True) | Q(protocol_checked_at__lt=now - PROTOCOL_RECHECK),
        status=TenderEstimate.PENDING, tender__collecting_finished_at__lt=now,
    ).order_by("tender__collecting_finished_at")
    finished = base.filter(
        Q(protocol_checked_at__isnull=True) | Q(protocol_checked_at__lt=now - PROTOCOL_BACKFILL_RECHECK),
        status__in=(TenderEstimate.WON, TenderEstimate.LOST, TenderEstimate.NOT_PARTICIPATED),
    ).order_by("-updated_at")
    estimates = (list(bidding[:limit]) + list(finished[:limit]))[:limit]

    attempted = succeeded = 0
    for estimate in estimates:
        if attempted:
            time.sleep(pause)
        attempted += 1
        try:
            succeeded += check_protocol(estimate)
        except ProtocolError as exc:
            logger.warning("protocols: ЕИС не ответил по %s (%s) — продолжим в следующий тик", estimate.tender_number, exc)
            break
    return attempted, succeeded
