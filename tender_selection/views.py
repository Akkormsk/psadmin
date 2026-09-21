from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .documents import MAX_BYTES, DocumentError, extract_preview, extract_zip_entry
from .filtering import match_title, parse_terms
from .models import DocumentPreview, FilterSettings, FoundTender, Organization, PullRun
from .notification import parse_clarifications, parse_complaints, parse_notification
from .regions import REGION_NAMES, region_name
from .services import (
    CATEGORY_GROUPS, _fetch_doc_bytes, apply_tender_outcome, effective_laws, effective_okpd2,
    enrich_one_org, extras_for, fetch_tender_outcome, notification_for, push_to_estimate,
    risk_assessment_for, run_pull,
)
from .stats import price_stats_for

SORTS = {
    "new": F("published_at").desc(nulls_last=True),
    "old": F("published_at").asc(nulls_last=True),
    "deadline": F("collecting_finished_at").asc(nulls_last=True),
    "deadline_far": F("collecting_finished_at").desc(nulls_last=True),
    "price_hi": F("max_price").desc(nulls_last=True),
    "price_lo": F("max_price").asc(nulls_last=True),
}
DEFAULT_SORT = "deadline"
LAW_LABELS = dict(FoundTender.LAW_CHOICES)


def superuser_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapped)


def _tender_viewable_by(user, tender):
    """Суперюзер видит любой тендер. Менеджер — только тот, что стал ЕГО просчётом
    (та же проверка владения, что и у самого просчёта — см. _estimate_for_user в
    tenders/views.py): попасть можно по ссылке «Открыть карточку →» со страницы
    своего просчёта, а не подбором номера в адресной строке и не через каталог."""
    if user.is_superuser:
        return True
    if tender.status != FoundTender.PUSHED or not tender.pushed_estimate_id:
        return False
    from tenders.models import TenderEstimate

    return TenderEstimate.objects.filter(pk=tender.pushed_estimate_id, owner=user).exists()


def tender_viewer_required(view):
    """Как superuser_required, но также пускает владельца просчёта, в который попал
    именно этот тендер (см. _tender_viewable_by) — карточка тендера и его документы
    доступны менеджеру со своего просчёта, каталог подбора (tender_list и все
    остальные view) по-прежнему только суперюзеру."""
    @wraps(view)
    def wrapped(request, pk, *args, **kwargs):
        tender = get_object_or_404(FoundTender, pk=pk)
        if not _tender_viewable_by(request.user, tender):
            raise Http404
        return view(request, pk, *args, **kwargs)

    return login_required(wrapped)


def _found_tender_card(tender):
    """Карточка «Входящие»/«Проверка» — тендер ещё не отправлен в расчёт.

    Решение «вперёд» (На оценку рисков / В расчёт) принимается только внутри
    самого тендера, не с карточки — рано жать кнопку, не открыв, что внутри.
    На карточке остаётся лишь маленький «×» скрыть (по ховеру).

    Оценка риска не показывается, пока тендер не прошёл «Входящие» — на
    этой стадии её ещё не считали (расчёт запускается при открытии карточки
    на стадии «Проверка»), нечего показывать раньше времени."""
    reviewed = tender.review != FoundTender.UNREVIEWED
    if tender.risk_error:
        risk_state, status_label, status_key = "error", "Ошибка оценки", "error"
    elif tender.risk_checked_at:
        risk_state, status_label, status_key = "ok", "Оценена", "assessed"
    else:
        risk_state, status_label, status_key = "pending", "Не оценена", "new"
    badges = []
    if reviewed:
        badges.append({"state": risk_state, "text": {"ok": "риск: оценён", "error": "риск: ошибка", "pending": "риск: ожидает"}[risk_state]})
    return {
        "kind": "found",
        "pk": tender.pk,
        "title": tender.title or tender.object_info,
        "law_label": tender.get_law_display(),
        "purchase_number": tender.purchase_number,
        "max_price": tender.max_price,
        "deadline": tender.collecting_finished_at,
        "status_label": status_label if reviewed else "",
        "status_key": status_key,
        "badges": badges,
        "detail_url": reverse("tender_selection:detail", args=[tender.pk]),
        "dismiss_url": reverse("tender_selection:dismiss", args=[tender.pk]),
    }


def _estimate_card(estimate):
    """Карточка «Расчёт»/«Торги»/«Результат» — просчёт, откуда бы он ни пришёл
    (перенесён из подбора или создан вручную импортом в самих «Тендерах»).

    Бейджи накапливаются по мере продвижения, не заменяют друг друга: ROI
    появляется на «Расчёте» и остаётся видимым дальше; факт торгов появляется
    только после «Внести итог» на «Торгах» и тоже остаётся на «Результате»."""
    summary = estimate.summary_snapshot or {}
    badges = []
    if summary.get("roi") is not None:
        badges.append({"state": "ok", "text": f"ROI {summary['roi']}%"})
    if estimate.outcome_checked_at:
        source_label = "авто" if estimate.outcome_source == estimate.OUTCOME_AUTO else "вручную"
        if estimate.actual_reduction_percent is not None:
            badges.append({"state": "ok", "text": f"факт: снижение {estimate.actual_reduction_percent}% ({source_label})"})
        else:
            badges.append({"state": "pending", "text": f"итог внесён ({source_label})"})
    return {
        "kind": "estimate",
        "pk": estimate.pk,
        "title": estimate.name,
        "tender_number": estimate.tender_number,
        "status": estimate.status,
        "status_label": estimate.get_status_display(),
        "status_key": estimate.status,
        "badges": badges,
        # Внесение итога живёт на странице самого тендера (tenders/home.html,
        # рядом с прогнозом снижения), не на карточке канбана — здесь только
        # уже накопленный результат в badges выше.
        "detail_url": reverse("tender_estimate", args=[estimate.pk]),
    }


_KANBAN_COLUMN_KEYS = ("incoming", "review", "calculation", "bidding", "result")


def _kanban_column_dirs(request):
    """Направление сортировки каждого столбца канбана — независимо друг от
    друга, из query-параметров ``dir_<key>``. По умолчанию — новые сверху."""
    return {key: ("asc" if request.GET.get(f"dir_{key}") == "asc" else "desc") for key in _KANBAN_COLUMN_KEYS}


def _kanban_toggle_qs(dirs, key):
    """Ссылка-стрелка одного столбца: та же сортировка у остальных, у этого — наоборот."""
    from urllib.parse import urlencode
    flipped = dict(dirs, **{key: "asc" if dirs[key] == "desc" else "desc"})
    return urlencode({f"dir_{k}": v for k, v in flipped.items() if v == "asc"})


def kanban(request):
    """Единая доска жизненного цикла тендера — не новая сущность, а объединённое
    чтение FoundTender (ещё не в расчёте) и TenderEstimate (расчёт, из любого
    источника: перенос из подбора или ручной импорт) в одном списке карточек."""
    dirs = _kanban_column_dirs(request)
    settings = FilterSettings.load()

    def _order(dir_key, *fields):
        return tuple(f if dirs[dir_key] == "asc" else f"-{f}" for f in fields)

    # Тот же порядок, что и по умолчанию в плоском списке (SORTS[DEFAULT_SORT] —
    # ближайший срок подачи сверху) — одно и то же выражение в обоих режимах,
    # чтобы «Входящие»/«Проверка» не расходились со списком последовательностью.
    def _found_order(dir_key):
        if dirs[dir_key] == "asc":
            return (F("collecting_finished_at").desc(nulls_last=True), F("first_seen_at").asc())
        return (SORTS[DEFAULT_SORT], F("first_seen_at").desc())

    def _visible(base_qs, dir_key):
        qs = base_qs.order_by(*_found_order(dir_key))
        rows, _hidden, _expired = _visible_found_tenders(
            qs, min_price=settings.min_price,
            include_words=settings.include_words, exclude_words=settings.exclude_words,
        )
        return rows

    incoming = [
        _found_tender_card(t) for t in
        _visible(FoundTender.objects.filter(status=FoundTender.NEW, review=FoundTender.UNREVIEWED), "incoming")
    ]
    review = [
        _found_tender_card(t) for t in
        _visible(FoundTender.objects.filter(status=FoundTender.NEW).exclude(review=FoundTender.UNREVIEWED), "review")
    ]

    from tenders.models import TenderEstimate

    calculation = [
        _estimate_card(e) for e in
        TenderEstimate.objects.filter(status=TenderEstimate.DRAFT).order_by(*_order("calculation", "updated_at"))
    ]
    bidding = [
        _estimate_card(e) for e in
        TenderEstimate.objects.filter(status=TenderEstimate.PENDING).order_by(*_order("bidding", "updated_at"))
    ]
    result = [
        _estimate_card(e) for e in
        TenderEstimate.objects.exclude(status__in=(TenderEstimate.DRAFT, TenderEstimate.PENDING))
        .order_by(*_order("result", "updated_at"))
    ]

    columns = [
        {"key": "incoming", "label": "Входящие", "cards": incoming},
        {"key": "review", "label": "Проверка", "cards": review},
        {"key": "calculation", "label": "Расчёт", "cards": calculation},
        {"key": "bidding", "label": "Торги", "cards": bidding},
        {"key": "result", "label": "Результат", "cards": result},
    ]
    for column in columns:
        column["dir"] = dirs[column["key"]]
        column["toggle_qs"] = _kanban_toggle_qs(dirs, column["key"])
    archived_count = FoundTender.objects.filter(status=FoundTender.DISMISSED).count()
    return render(request, "tender_selection/kanban.html", {"columns": columns, "archived_count": archived_count, "settings": settings})


def _visible_found_tenders(queryset, *, min_price, include_words, exclude_words, show_all=False):
    """Общие правила «что скрыто» у найденных тендеров — минимальная цена,
    истёкший срок подачи, плюс/минус-слова. Одна функция для канбана и
    плоского списка, чтобы они не расходились в том, что показывают."""
    now = timezone.now()
    if min_price:
        queryset = queryset.filter(Q(max_price__gte=min_price) | Q(max_price__isnull=True))
    expired = 0
    if not show_all:
        expired = queryset.filter(collecting_finished_at__lt=now).count()
        queryset = queryset.filter(Q(collecting_finished_at__gte=now) | Q(collecting_finished_at__isnull=True))
    include = parse_terms(include_words)
    exclude = parse_terms(exclude_words)
    rows, hidden = [], 0
    for tender in queryset:
        passes, hits = match_title(tender.title or tender.object_info, include, exclude)
        if passes or show_all:
            tender.match_hits = hits
            tender.filtered_out = not passes
            rows.append(tender)
        else:
            hidden += 1
    return rows, hidden, expired


_ESTIMATE_STAGE_STATUSES = {
    "calculation": ("draft",),
    "bidding": ("pending",),
    "result": ("not_participated", "lost", "won"),
}


@superuser_required
def tender_list(request):
    if request.GET.get("view") != "list":
        return kanban(request)

    stage = request.GET.get("stage") if request.GET.get("stage") in (*_ESTIMATE_STAGE_STATUSES, "all") else ""

    # «Расчёт»/«Торги»/«Результат» — это уже не FoundTender, а TenderEstimate
    # (см. kanban()) — не нужны ни сортировки, ни плюс/минус-слова ЕИС-триажа,
    # только сам список. При stage="" (по умолчанию, «Новые») это не строится
    # вовсе — быстрый путь остаётся быстрым.
    estimate_cards = None
    if stage:
        from tenders.models import TenderEstimate

        statuses = [s for statuses in (
            _ESTIMATE_STAGE_STATUSES.values() if stage == "all" else (_ESTIMATE_STAGE_STATUSES[stage],)
        ) for s in statuses]
        estimate_cards = [
            _estimate_card(estimate)
            for estimate in TenderEstimate.objects.filter(status__in=statuses).order_by("-updated_at")
        ]
        if stage != "all":
            return render(request, "tender_selection/list.html", {"stage": stage, "estimate_cards": estimate_cards})

    settings = FilterSettings.load()
    # плюс/минус-слова можно временно переопределить прямо на странице (?inc=/?exc=),
    # не трогая сохранённые настройки — для подбора формулировок
    inc_raw = request.GET.get("inc")
    exc_raw = request.GET.get("exc")
    words_overridden = inc_raw is not None or exc_raw is not None
    inc_value = inc_raw if inc_raw is not None else settings.include_words
    exc_value = exc_raw if exc_raw is not None else settings.exclude_words
    include = parse_terms(inc_value)
    exclude = parse_terms(exc_value)
    show_all = request.GET.get("all") == "1"
    sort = request.GET.get("sort") if request.GET.get("sort") in SORTS else DEFAULT_SORT
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    now = timezone.now()
    soon_cutoff = now + timedelta(days=1)

    queryset = FoundTender.objects.exclude(status=FoundTender.DISMISSED)
    if law_filter != "all":
        queryset = queryset.filter(law=law_filter)
    queryset = queryset.order_by(SORTS[sort], F("first_seen_at").desc())

    rows, hidden, expired = _visible_found_tenders(
        queryset, min_price=settings.min_price, include_words=inc_value, exclude_words=exc_value, show_all=show_all,
    )

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    orgs = {o.inn: o for o in Organization.objects.filter(
        inn__in=[t.customer_inn for t in page.object_list if t.customer_inn]
    )}
    for tender in page.object_list:
        tender.org = orgs.get(tender.customer_inn)
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        tender.is_soon = bool(tender.collecting_finished_at and now <= tender.collecting_finished_at <= soon_cutoff)
        tender.on_estimate = tender.status == FoundTender.PUSHED and tender.pushed_estimate_id
        tender.has_complaint = bool(tender.complaints_raw)
        # 223-ФЗ не имеет разобранного извещения по конструкции источника — это не
        # ошибка. У 44-ФЗ пустой notification_raw значит запрос ещё не удался — но
        # только если попытка вообще была (notification_checked_at): свежевыгруженный
        # тендер, который ещё никто не открывал и фон (retry_pending_notifications)
        # до него не добрался, — это не сбой, а «ещё не проверяли», значок не должен
        # гореть на буквально каждой новой карточке.
        tender.notification_missing = (
            tender.law == "fz44" and not tender.notification_raw and tender.notification_checked_at is not None
        )

    counts = dict(FoundTender.objects.exclude(status=FoundTender.DISMISSED).values_list("law").annotate(n=Count("law")))
    return render(request, "tender_selection/list.html", {
        "page_obj": page,
        "shown_count": len(rows),
        "hidden_count": hidden,
        "expired_count": expired,
        "show_all": show_all,
        "sort": sort,
        "law_filter": law_filter,
        "law_counts": {"all": sum(counts.values()), "fz44": counts.get("fz44", 0), "fz223": counts.get("fz223", 0)},
        "settings": settings,
        "last_run": PullRun.objects.first(),
        "inc_value": inc_value,
        "exc_value": exc_value,
        "words_overridden": words_overridden,
        "stage": stage,
        "estimate_cards": estimate_cards,
    })


@tender_viewer_required
def tender_detail(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    if tender.opened_at is None:  # для «жирного» непрочитанного в списке — только реальный заход, не фон
        tender.opened_at = timezone.now()
        tender.save(update_fields=["opened_at"])
    payload = notification_for(tender, force=request.GET.get("refresh") == "1")
    card = parse_notification(payload) if payload else None
    estimate_id = tender.pushed_estimate_id if tender.status == FoundTender.PUSHED else None

    org = Organization.objects.filter(inn=tender.customer_inn).first() if tender.customer_inn else None
    if tender.law != "fz44" and org is None and tender.customer_inn:
        org = enrich_one_org(tender.customer_inn, tender.law)  # для 223 карточки заказчика больше неоткуда взять

    clar_raw, comp_raw = extras_for(tender, force=request.GET.get("refresh") == "1")

    stats = price_stats_for(tender, card)
    if stats:
        for row in stats["examples"]:
            row["region_label"] = region_name(row["region"]) if row["region"] else ""

    # Оценка рисков читает документы закупки — может занимать до ~30-40с (сеть до ЕИС).
    # Чтобы это не блокировало открытие карточки, первый расчёт уходит в фон (см.
    # risk_status ниже, дергается JS-ом со спиннером). Уже посчитанное (успех или
    # ошибка — risk_checked_at не пуст) отдаём сразу, без лишнего похода в шлюз.
    risk_needs_fetch = card is not None and tender.risk_checked_at is None

    return render(request, "tender_selection/detail.html", {
        "tender": tender,
        "card": card,
        "org": org,
        "region_label": region_name(tender.region) if tender.region else "",
        "fetch_failed": payload is None and tender.law == "fz44",
        "is_fz223": tender.law != "fz44",
        "pushed_estimate_id": estimate_id,
        "clarifications": parse_clarifications(clar_raw),
        "complaints": parse_complaints(comp_raw),
        "price_stats": stats,
        "risk_needs_fetch": risk_needs_fetch,
        "risk": None if risk_needs_fetch else (tender.risk_assessment or None),
        "risk_error": "" if risk_needs_fetch else tender.risk_error,
        "risk_docs": [] if risk_needs_fetch else tender.risk_assessment_docs,
    })


@tender_viewer_required
def risk_status(request, pk):
    """AJAX-эндпоинт для блока «Оценка рисков» на карточке — считает (или берёт из
    кэша) и отдаёт готовый HTML-фрагмент. Чтение документов и запрос к ИИ-шлюзу могут
    занять десятки секунд, поэтому вызывается из JS отдельно от рендера страницы."""
    tender = get_object_or_404(FoundTender, pk=pk)
    risk = risk_assessment_for(tender, force=request.GET.get("refresh") == "1")
    html = render_to_string("tender_selection/_risk_block.html", {
        "risk": risk, "risk_error": tender.risk_error, "risk_docs": tender.risk_assessment_docs,
    }, request=request)
    return JsonResponse({"html": html})


def _doc_by_idx(tender, idx):
    """(doc-словарь | None, JsonResponse-ошибка | None)."""
    card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    docs = (card or {}).get("documents", [])
    if not 0 <= idx < len(docs):
        return None, JsonResponse({"error": "Документ не найден."}, status=404)
    return docs[idx], None




def _result_json(name, result):
    return JsonResponse({"name": name, "kind": result.get("kind", ""),
                         "html": result.get("html", ""), "error": result.get("error", ""),
                         "zip_entries": result.get("zip_entries", [])})


@tender_viewer_required
def doc_preview(request, pk, idx):
    tender = get_object_or_404(FoundTender, pk=pk)
    doc, err = _doc_by_idx(tender, idx)
    if err:
        return err
    url, name = doc.get("url", ""), doc.get("name", "")

    cached = DocumentPreview.objects.filter(url=url).first()
    if cached and request.GET.get("refresh") != "1":
        return _result_json(name, {"kind": cached.kind, "html": cached.html, "error": cached.error})

    try:
        data = _fetch_doc_bytes(tender, url, name)
    except DocumentError as exc:
        # сетевые сбои не кэшируем — на проде повтор может пройти
        return _result_json(name, {"error": str(exc)})

    result = extract_preview(data, name)
    DocumentPreview.objects.update_or_create(url=url, defaults={
        "filename": name, "kind": result.get("kind", ""),
        "html": result.get("html", ""), "error": result.get("error", ""),
    })
    return _result_json(name, result)


@tender_viewer_required
@require_POST
def doc_upload(request, pk, idx):
    """Ручной запасной путь: если у сервера вдруг снова не будет сети до ЕИС —
    пользователь скачивает файл сам и загружает сюда, дальше тот же разбор
    (extract_preview), что и при автоскачивании."""
    tender = get_object_or_404(FoundTender, pk=pk)
    doc, err = _doc_by_idx(tender, idx)
    if err:
        return err
    url, name = doc.get("url", ""), doc.get("name", "")

    uploaded = request.FILES.get("file")
    if not uploaded:
        return _result_json(name, {"error": "Файл не выбран."})
    if uploaded.size > MAX_BYTES:
        return _result_json(name, {"error": "Файл слишком большой для предпросмотра."})

    result = extract_preview(uploaded.read(), name)
    DocumentPreview.objects.update_or_create(url=url, defaults={
        "filename": name, "kind": result.get("kind", ""),
        "html": result.get("html", ""), "error": result.get("error", ""),
    })
    return _result_json(name, result)


@tender_viewer_required
def doc_zip_entry(request, pk, idx, entry):
    """Провал внутрь многофайлового архива: качаем документ заново (файл-то один
    и тот же — кэш предпросмотра держит только итог разбора КОНКРЕТНОГО вложенного
    файла, не сырые байты архива) и достаём из него нужный вложенный файл."""
    tender = get_object_or_404(FoundTender, pk=pk)
    doc, err = _doc_by_idx(tender, idx)
    if err:
        return err
    url, name = doc.get("url", ""), doc.get("name", "")

    try:
        data = _fetch_doc_bytes(tender, url, name)
    except DocumentError as exc:
        return _result_json(entry, {"error": str(exc)})

    inner = extract_zip_entry(data, entry)
    if inner is None:
        return _result_json(entry, {"error": "Файл не найден в архиве."})
    return _result_json(entry, extract_preview(inner, entry))


@superuser_required
def eis_diag(request):
    """Разовая диагностика: реально ли прод-сервер видит сеть ЕИС на уровне TCP/HTTPS,
    или заблокирован весь домен zakupki.gov.ru (а не только конкретная ссылка/метод).
    Ничего не сохраняет, только сетевые зонды с прод-машины."""
    import socket
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as _Req
    from urllib.request import urlopen as _urlopen

    results = []

    def probe_tcp(label, host, port=443, timeout=8):
        t0 = time.monotonic()
        try:
            conn = socket.create_connection((host, port), timeout=timeout)
            conn.close()
            results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000)})
        except Exception as exc:
            results.append({"probe": label, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                             "error": f"{type(exc).__name__}: {exc}"})

    def probe_http(label, url, timeout=10):
        t0 = time.monotonic()
        try:
            req = _Req(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with _urlopen(req, timeout=timeout) as resp:
                results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000),
                                 "status": resp.status})
        except HTTPError as exc:
            # HTTP-ошибка — значит соединение и TLS прошли, портал ответил (это НЕ таймаут)
            results.append({"probe": label, "ok": True, "ms": round((time.monotonic() - t0) * 1000),
                             "status": exc.code, "note": "сервер ответил (пусть и ошибкой) — сеть не блокирует"})
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            results.append({"probe": label, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                             "error": f"{type(exc).__name__}: {exc}"})

    # свой внешний IP — чтобы проверить снаружи (по базам geo/ASN), где он реально числится,
    # независимо от того, что написано в личном кабинете Timeweb
    my_ip = None
    try:
        req = _Req("https://api.ipify.org?format=json")
        with _urlopen(req, timeout=8) as resp:
            import json as _json
            my_ip = _json.loads(resp.read().decode("utf-8")).get("ip")
        results.append({"probe": "свой внешний IP", "ok": True, "ms": 0, "note": my_ip})
    except Exception as exc:
        results.append({"probe": "свой внешний IP", "ok": False, "ms": 0, "error": f"{type(exc).__name__}: {exc}"})

    # контроль: то, что точно работает (автосбор дёргает это же каждые 30 мин)
    probe_tcp("TCP v2test.gosplan.info (контроль, точно работает)", "v2test.gosplan.info")
    probe_tcp("TCP zakupki.gov.ru", "zakupki.gov.ru")
    probe_tcp("TCP int44.zakupki.gov.ru", "int44.zakupki.gov.ru")
    probe_http("GET https://zakupki.gov.ru/ (главная, не файл)", "https://zakupki.gov.ru/")

    # масштаб блокировки: только ЕИС или весь рунет с этого сервера?
    probe_tcp("TCP www.gosuslugi.ru (другой gov.ru)", "www.gosuslugi.ru")
    probe_tcp("TCP www.nalog.gov.ru (другой gov.ru)", "www.nalog.gov.ru")
    probe_tcp("TCP www.cbr.ru (ЦБ РФ, не gov.ru)", "www.cbr.ru")
    probe_tcp("TCP ya.ru (обычный рунет, контроль)", "ya.ru")

    return JsonResponse({"results": results, "my_ip": my_ip})


@superuser_required
def filter_settings(request):
    settings = FilterSettings.load()
    if request.method == "POST":
        settings.include_words = request.POST.get("include_words", "").strip()
        settings.exclude_words = request.POST.get("exclude_words", "").strip()
        settings.min_price = request.POST.get("min_price") or 0
        settings.window_days = request.POST.get("window_days") or 7
        settings.okpd2_codes = request.POST.getlist("okpd2")
        settings.regions = [r for r in request.POST.getlist("region") if r.isdigit()]
        settings.laws = [law for law in request.POST.getlist("law") if law in ("fz44", "fz223")] or ["fz44"]
        settings.save()
        messages.success(request, "Настройки подбора сохранены.")
        return redirect("tender_selection:list")

    chosen_codes = set(effective_okpd2(settings))
    chosen_regions = set(str(r) for r in (settings.regions or []))
    chosen_laws = set(effective_laws(settings))
    return render(request, "tender_selection/settings.html", {
        "settings": settings,
        "categories": [(code, label, code in chosen_codes) for code, label in CATEGORY_GROUPS],
        "regions": [(code, name, str(code) in chosen_regions) for code, name in sorted(REGION_NAMES.items(), key=lambda x: x[1])],
        "laws": [(code, label, code in chosen_laws) for code, label in FoundTender.LAW_CHOICES],
        "using_defaults": not settings.okpd2_codes,
    })


@superuser_required
@require_POST
def save_words(request):
    settings = FilterSettings.load()
    settings.include_words = request.POST.get("inc", "").strip()
    settings.exclude_words = request.POST.get("exc", "").strip()
    settings.save()
    messages.success(request, "Плюс/минус-слова сохранены.")
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def pull_now(request):
    run = run_pull(max_requests=16)
    if run.ok:
        messages.success(
            request,
            f"Выгрузка: запросов {run.requests_made}, записей {run.records_received}, "
            f"новых {run.created_count}, за {run.duration_seconds} сек.",
        )
    else:
        messages.error(request, f"Выгрузка не удалась: {run.error}")
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def push_estimate(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    if tender.status == FoundTender.PUSHED and tender.pushed_estimate_id:
        return redirect("tender_estimate", pk=tender.pushed_estimate_id)
    try:
        estimate_id = push_to_estimate(tender, request.user)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось создать просчёт: {exc}")
        return redirect("tender_selection:detail", pk=pk)
    messages.success(request, "Просчёт создан. Позиции подставлены из извещения.")
    return redirect("tender_estimate", pk=estimate_id)


@superuser_required
@require_POST
def enter_outcome(request, pk):
    """Обязательный шаг канбана для карточек «Торги» — забрать факт торгов
    (автоматически через ГосПлан) или подтвердить его вручную, если по номеру
    закупки контракт ещё не найден или свой ИНН не настроен (COMPANY_INN)."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    manual_status = request.POST.get("status")

    if manual_status in (TenderEstimate.WON, TenderEstimate.LOST, TenderEstimate.NOT_PARTICIPATED):
        apply_tender_outcome(estimate, status=manual_status, source=TenderEstimate.OUTCOME_MANUAL)
        messages.success(request, f"Итог внесён вручную: {estimate.get_status_display()}.")
    else:
        outcome = fetch_tender_outcome(estimate)
        if not outcome.get("found"):
            messages.warning(request, "Контракт по этому номеру закупки в реестре пока не найден — попробуйте позже или внесите итог вручную.")
        elif outcome.get("auto_status"):
            apply_tender_outcome(
                estimate, status=outcome["auto_status"], price=outcome.get("price"),
                reduction_percent=outcome.get("reduction_percent"), source=TenderEstimate.OUTCOME_AUTO,
            )
            messages.success(request, f"Итог найден автоматически: {estimate.get_status_display()}.")
        else:
            estimate.actual_price = outcome.get("price")
            estimate.actual_reduction_percent = outcome.get("reduction_percent")
            estimate.outcome_checked_at = timezone.now()
            estimate.save(update_fields=["actual_price", "actual_reduction_percent", "outcome_checked_at"])
            messages.info(request, "Цена контракта найдена, но выиграли мы или нет — решите сами кнопками ниже (свой ИНН не настроен).")
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def dismiss(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    tender.status = FoundTender.DISMISSED
    tender.save(update_fields=["status"])
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def set_review(request, pk):
    tender = get_object_or_404(FoundTender, pk=pk)
    value = request.POST.get("review", "")
    if value in dict(FoundTender.REVIEW_CHOICES):
        tender.review = value
        tender.save(update_fields=["review"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"review": tender.review, "label": tender.get_review_display()})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
