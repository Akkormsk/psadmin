from datetime import timedelta
from decimal import Decimal, InvalidOperation
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
from .models import DocumentPreview, FilterSettings, Organization, PullRun, Tender
from .notification import parse_clarifications, parse_complaints, parse_notification
from .regions import REGION_NAMES, region_name
from .services import (
    CATEGORY_GROUPS, _fetch_doc_bytes, apply_tender_outcome, effective_laws, effective_okpd2,
    enrich_one_org, extras_for, fetch_tender_outcome, notification_for, push_to_estimate,
    risk_assessment_for, run_pull, start_risk_assessment_in_background,
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
LAW_LABELS = dict(Tender.LAW_CHOICES)


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
    if tender.status != Tender.PUSHED:
        return False
    from tenders.models import TenderEstimate

    return tender.estimates.filter(owner=user).exists()


def tender_viewer_required(view):
    """Весь lifecycle тендера доступен только администратору."""
    return superuser_required(view)


def _found_tender_card(tender):
    """Карточка «Входящие»/«Проверка» — тендер ещё не отправлен в расчёт.

    Решение «вперёд» (На оценку рисков / В расчёт) принимается только внутри
    самого тендера, не с карточки — рано жать кнопку, не открыв, что внутри.
    На карточке остаётся лишь маленький «×» скрыть (по ховеру).

    Оценка риска не показывается, пока тендер не прошёл «Входящие» — на
    этой стадии её ещё не считали (расчёт запускается при открытии карточки
    на стадии «Проверка»), нечего показывать раньше времени."""
    reviewed = tender.review != Tender.UNREVIEWED
    if tender.risk_error:
        risk_state, status_label, status_key = "error", "Ошибка оценки", "error"
    elif tender.risk_checked_at:
        risk_state, status_label, status_key = "ok", "Оценена", "assessed"
    else:
        risk_state, status_label, status_key = "pending", "Не оценена", "new"
    badges = []
    if reviewed:
        # Как только оценка реально посчитана, вместо мета-статуса ("оценена/не
        # оценена") показываем светофор по её итоговому уровню — критерии живут
        # целиком в самом промпте (risk_assessment.py), тут только раскраска.
        risk_level = (tender.risk_assessment or {}).get("risk_level") if risk_state == "ok" else None
        risk_badge = {
            "low": {"state": "ok", "text": "риск: низкий"},
            "medium": {"state": "warn", "text": "риск: средний"},
            "high": {"state": "error", "text": "риск: высокий"},
        }.get(risk_level)
        if risk_badge:
            badges.append(risk_badge)
        else:
            badges.append({"state": risk_state, "text": {"ok": "риск: оценён", "error": "риск: ошибка", "pending": "риск: ожидает"}[risk_state]})
    now = timezone.now()
    is_soon = bool(tender.collecting_finished_at and now <= tender.collecting_finished_at <= now + timedelta(days=1))
    return {
        "kind": "found",
        "pk": tender.pk,
        "title": tender.title or tender.object_info,
        "law_label": tender.get_law_display(),
        "purchase_number": tender.purchase_number,
        "max_price": tender.max_price,
        "deadline": tender.collecting_finished_at,
        "is_soon": is_soon,
        "status_label": "",
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
        from decimal import Decimal, InvalidOperation

        from tenders.services import roi_thresholds

        try:
            good, thin = roi_thresholds()
            roi_value = Decimal(str(summary["roi"]))
            roi_state = "ok" if roi_value >= good else "warn" if roi_value >= thin else "error"
        except InvalidOperation:
            roi_state = "pending"
        badges.append({"state": roi_state, "text": f"ROI {summary['roi']}%"})
    if estimate.outcome_checked_at:
        source_label = "авто" if estimate.outcome_source == estimate.OUTCOME_AUTO else "вручную"
        if estimate.actual_reduction_percent is not None:
            badges.append({"state": "pending", "text": f"факт: снижение {estimate.actual_reduction_percent}% ({source_label})"})
        else:
            badges.append({"state": "pending", "text": f"итог внесён ({source_label})"})
    # Карточка ведёт на страницу ТЕНДЕРА (с растущими блоками по стадиям), а не
    # сразу в рабочее пространство расчёта — туда только через кнопку «Перейти
    # в расчёт» внутри блока «Расчёт» на самой странице тендера.
    detail_url = reverse("tender_selection:detail", args=[estimate.tender_id]) if estimate.tender_id else ""
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
        "detail_url": detail_url,
        "dismiss_url": reverse("tender_selection:dismiss_estimate", args=[estimate.pk]),
        # Архивировать «в тихую» просчёт, по которому ещё не внесён итог торгов, —
        # частая случайная потеря данных; предупреждаем перед этим (см. kanban.html).
        "warn_before_dismiss": not estimate.outcome_checked_at,
    }


_KANBAN_COLUMN_KEYS = ("review", "calculation", "bidding", "result")


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
    чтение Tender (ещё не в расчёте) и TenderEstimate (расчёт, из любого
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

    # «Входящие» больше не колонка канбана — это отдельный список (tender_list);
    # сюда тендер попадает только после «В работу» (review != unreviewed).
    review = [
        _found_tender_card(t) for t in
        _visible(Tender.objects.filter(status=Tender.NEW).exclude(review=Tender.UNREVIEWED), "review")
    ]

    from tenders.models import TenderEstimate

    live_estimates = TenderEstimate.objects.filter(
        tender__isnull=False,
    ).exclude(tender__status=Tender.DISMISSED)
    calculation = [
        _estimate_card(e) for e in
        live_estimates.filter(status=TenderEstimate.DRAFT).order_by(*_order("calculation", "updated_at"))
    ]
    bidding = [
        _estimate_card(e) for e in
        live_estimates.filter(status=TenderEstimate.PENDING).order_by(*_order("bidding", "updated_at"))
    ]
    result = [
        _estimate_card(e) for e in
        live_estimates.exclude(status__in=(TenderEstimate.DRAFT, TenderEstimate.PENDING))
        .order_by(*_order("result", "updated_at"))
    ]

    columns = [
        {"key": "review", "label": "Оценка", "cards": review},
        {"key": "calculation", "label": "Расчёт", "cards": calculation},
        {"key": "bidding", "label": "Торги", "cards": bidding},
        {"key": "result", "label": "Результат", "cards": result},
    ]
    for column in columns:
        column["dir"] = dirs[column["key"]]
        column["toggle_qs"] = _kanban_toggle_qs(dirs, column["key"])
    archived_count = (
        Tender.objects.filter(status=Tender.DISMISSED).count()
    )
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


@superuser_required
def tender_list(request):
    """«Входящие» — только первичный отбор входящего потока: пока тендер не
    переведён «В работу» (review=unreviewed). Жизненным циклом уже отобранных
    занимается канбан (kanban()), не этот экран — сюда попавшие «в работу»
    не возвращаются, и статус тут показывать нечего."""
    view = request.GET.get("view")
    if view is None:
        # Голый заход без ?view= — открываем ту вкладку, что смотрели в
        # прошлый раз в этой сессии, а не всегда канбан («Торги»).
        view = request.session.get("ts_last_view", "kanban")
    request.session["ts_last_view"] = "list" if view == "list" else "kanban"
    if view != "list":
        return kanban(request)

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

    queryset = Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED)
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

    counts = dict(
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED)
        .values_list("law").annotate(n=Count("law"))
    )
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
    })


@tender_viewer_required
def tender_detail(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    if tender.opened_at is None:  # для «жирного» непрочитанного в списке — только реальный заход, не фон
        tender.opened_at = timezone.now()
        tender.save(update_fields=["opened_at"])
    is_manual = tender.source == Tender.MANUAL
    payload = None if is_manual else notification_for(tender, force=request.GET.get("refresh") == "1")
    card = parse_notification(payload) if payload else None
    estimate_id = tender.estimates.order_by("-updated_at").values_list("pk", flat=True).first()

    # Компактная сводка расчёта прямо на странице тендера (см. концепцию: блок
    # с данными остаётся на каждом пройденном этапе) — цена/прибыль/ROI с учётом
    # прогноза + кнопка в сам расчёт. Тот же verdict_for, что и на странице
    # расчёта — одни и те же цифры, не пересчитываем по-своему.
    calc_verdict = None
    estimate = None
    if estimate_id:
        from tenders.models import TenderEstimate
        from tenders.services import verdict_for

        estimate = TenderEstimate.objects.filter(pk=estimate_id).first()
        if estimate:
            calc_verdict = verdict_for(estimate, tender)

    lifecycle = _tender_lifecycle(tender, estimate)
    active_stage = "review" if estimate is None else (
        "calculation" if estimate.status == TenderEstimate.DRAFT else
        "bidding" if estimate.status == TenderEstimate.PENDING else "result"
    )

    org = Organization.objects.filter(inn=tender.customer_inn).first() if tender.customer_inn else None
    if tender.law != "fz44" and org is None and tender.customer_inn:
        org = enrich_one_org(tender.customer_inn, tender.law)  # для 223 карточки заказчика больше неоткуда взять

    clar_raw, comp_raw = ([], []) if is_manual else extras_for(tender, force=request.GET.get("refresh") == "1")

    # Прогноз снижения и оценка риска не показываются на «Входящих» — рано,
    # ещё не решили, что тендер вообще стоит смотреть; появляются вместе,
    # начиная с «Проверки» (review != unreviewed).
    stats = price_stats_for(tender, card) if card and tender.review != Tender.UNREVIEWED else None
    if stats:
        for row in stats["examples"]:
            row["region_label"] = region_name(row["region"]) if row["region"] else ""

    if is_manual:
        risk_needs_fetch = False
        risk = None
        risk_error = ""
    elif tender.review == Tender.UNREVIEWED:
        # «Входящие» — ещё рано на настоящую (платную) оценку, но бесплатную
        # предварительную сводку по уже разобранному извещению показываем
        # всегда: та же форма таблицы, без документов и без ИИ (см.
        # risk_assessment.preliminary_summary). Кнопки «Оценить риски» тут
        # нет — оценка стартует автоматически по «В работу».
        from .risk_assessment import preliminary_summary

        risk_needs_fetch = False
        risk = preliminary_summary(card) if card else None
        risk_error = ""
    else:
        # Оценка рисков читает документы закупки — может занимать до ~30-40с
        # (сеть до ЕИС). Чтобы это не блокировало открытие карточки, первый
        # расчёт уходит в фон (см. risk_status ниже, дергается JS-ом со
        # спиннером). Уже посчитанное (успех или ошибка — risk_checked_at не
        # пуст) отдаём сразу, без лишнего похода в шлюз.
        risk_needs_fetch = card is not None and (
            tender.risk_checked_at is None or "risk_factors" not in (tender.risk_assessment or {})
        )
        risk = None if risk_needs_fetch else (tender.risk_assessment or None)
        risk_error = "" if risk_needs_fetch else tender.risk_error

    return render(request, "tender_selection/detail.html", {
        "tender": tender,
        "is_manual": is_manual,
        "display_purchase_number": (tender.raw or {}).get("display_number") or tender.purchase_number,
        "card": card,
        "org": org,
        "region_label": region_name(tender.region) if tender.region else "",
        "fetch_failed": payload is None and tender.law == "fz44",
        "is_fz223": tender.law != "fz44",
        "pushed_estimate_id": estimate_id,
        "pipeline_estimate_id": estimate_id,
        "calc_verdict": calc_verdict,
        "lifecycle": lifecycle,
        "active_stage": active_stage,
        "estimate": estimate,
        "clarifications": parse_clarifications(clar_raw),
        "complaints": parse_complaints(comp_raw),
        "price_stats": stats,
        "risk_needs_fetch": risk_needs_fetch,
        "risk": risk,
        "risk_error": risk_error,
        "risk_docs": [] if risk_needs_fetch else tender.risk_assessment_docs,
    })


def _tender_lifecycle(tender, estimate):
    """Короткий ориентир на карточке: этапы, а не вторая навигация."""
    status = estimate.status if estimate else ""
    current = "incoming" if tender.review == Tender.UNREVIEWED else "evaluation"
    if estimate:
        current = "calculation" if status == "draft" else "bidding" if status == "pending" else "result"

    order = ("incoming", "evaluation", "calculation", "bidding", "result")
    labels = {"incoming": "Входящие", "evaluation": "Оценка", "calculation": "Расчёт", "bidding": "Торги", "result": "Результат"}
    current_index = order.index(current)
    return [
        {"label": labels[key], "state": "done" if index < current_index else "current" if index == current_index else "future"}
        for index, key in enumerate(order)
    ]


@tender_viewer_required
def risk_status(request, pk):
    """AJAX-эндпоинт для блока «Оценка рисков» на карточке — считает (или берёт из
    кэша) и отдаёт готовый HTML-фрагмент. Чтение документов и запрос к ИИ-шлюзу могут
    занять десятки секунд, поэтому вызывается из JS отдельно от рендера страницы."""
    tender = get_object_or_404(Tender, pk=pk)
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


def _clear_tender_document_previews(tender):
    card = parse_notification(tender.notification_raw) if tender.notification_raw else None
    urls = [doc.get("url") for doc in (card or {}).get("documents", []) if doc.get("url")]
    if urls:
        DocumentPreview.objects.filter(url__in=urls).delete()




def _result_json(name, result):
    return JsonResponse({"name": name, "kind": result.get("kind", ""),
                         "html": result.get("html", ""), "error": result.get("error", ""),
                         "zip_entries": result.get("zip_entries", [])})


@tender_viewer_required
def doc_preview(request, pk, idx):
    tender = get_object_or_404(Tender, pk=pk)
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
    tender = get_object_or_404(Tender, pk=pk)
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
    tender = get_object_or_404(Tender, pk=pk)
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
        settings.risk_warning_days = int(request.POST.get("risk_warning_days") or 14)
        settings.risk_critical_days = int(request.POST.get("risk_critical_days") or 7)
        if settings.risk_critical_days >= settings.risk_warning_days:
            messages.error(request, "Критический срок должен быть меньше предупреждающего.")
            return redirect("tender_selection:settings")
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
        "laws": [(code, label, code in chosen_laws) for code, label in Tender.LAW_CHOICES],
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
    tender = get_object_or_404(Tender, pk=pk)
    if tender.status == Tender.PUSHED and tender.estimates.exists():
        return redirect("tender_selection:detail", pk=tender.pk)
    try:
        push_to_estimate(tender, request.user)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось создать просчёт: {exc}")
        return redirect("tender_selection:detail", pk=pk)
    messages.success(request, "Просчёт создан. Позиции подставлены из извещения.")
    return redirect("tender_selection:detail", pk=tender.pk)


@superuser_required
@require_POST
def enter_outcome(request, pk):
    """Обязательный шаг канбана для карточек «Торги» — забрать факт торгов
    (автоматически через ГосПлан) или подтвердить его вручную, если по номеру
    закупки контракт ещё не найден или свой ИНН не настроен (COMPANY_INN)."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    manual_status = request.POST.get("status")
    manual_reduction = request.POST.get("actual_reduction_percent", "").strip()
    try:
        reduction_percent = Decimal(manual_reduction) if manual_reduction else None
        if reduction_percent is not None and not Decimal("0") <= reduction_percent <= Decimal("100"):
            raise InvalidOperation
    except InvalidOperation:
        messages.error(request, "Фактическое снижение должно быть числом от 0 до 100.")
        return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")

    if manual_status in (TenderEstimate.WON, TenderEstimate.LOST, TenderEstimate.NOT_PARTICIPATED):
        apply_tender_outcome(
            estimate, status=manual_status, reduction_percent=reduction_percent,
            source=TenderEstimate.OUTCOME_MANUAL,
        )
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
    tender = get_object_or_404(Tender, pk=pk)
    _clear_tender_document_previews(tender)
    tender.status = Tender.DISMISSED
    tender.archived_at = timezone.now()
    tender.save(update_fields=["status", "archived_at"])
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def dismiss_estimate(request, pk):
    """Архивировать карточку Tender со стадии расчёта, торгов или результата."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    tender = get_object_or_404(Tender, pk=estimate.tender_id)
    _clear_tender_document_previews(tender)
    tender.status = Tender.DISMISSED
    tender.archived_at = timezone.now()
    tender.save(update_fields=["status", "archived_at"])
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def restore(request, pk):
    tender = get_object_or_404(Tender, pk=pk, status=Tender.DISMISSED)
    tender.status = Tender.NEW
    tender.archived_at = None
    tender.save(update_fields=["status", "archived_at"])
    return redirect("tender_selection:list")


@superuser_required
@require_POST
def restore_estimate(request, pk):
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    return restore(request, estimate.tender_id)


@superuser_required
def archive(request):
    """Единый архив скрытых карточек с возможностью восстановления."""
    found = [
        _found_tender_card(t) for t in
        Tender.objects.filter(status=Tender.DISMISSED).order_by("-archived_at")
    ]
    return render(request, "tender_selection/archive.html", {"found": found, "estimates": []})


@superuser_required
@require_POST
def set_review(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    value = request.POST.get("review", "")
    if value in dict(Tender.REVIEW_CHOICES):
        was_unreviewed = tender.review == Tender.UNREVIEWED
        tender.review = value
        tender.save(update_fields=["review"])
        if was_unreviewed and value != Tender.UNREVIEWED and not tender.risk_checked_at:
            start_risk_assessment_in_background(tender.pk)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"review": tender.review, "label": tender.get_review_display()})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
