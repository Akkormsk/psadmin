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
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .documents import MAX_BYTES, DocumentError, extract_preview, extract_zip_entry
from .filtering import match_title, parse_terms
from .models import ContractStat, DocumentPreview, FilterSettings, Organization, PullRun, Tender, TenderDismissalFeedback
from .notification import detail_document_candidate, parse_clarifications, parse_complaints, parse_notification
from .regions import REGION_NAMES, region_name
from .services import (
    CATEGORY_GROUPS, _fetch_doc_bytes, reduction_percent_from, apply_tender_outcome, check_protocol, effective_laws,
    effective_okpd2, enrich_one_org, extras_for, extras_need_refresh, fetch_tender_outcome, notification_for, push_to_estimate,
    archive_tender, archive_stage_for, risk_assessment_for, run_pull, set_our_bid, start_risk_assessment_in_background,
    start_extras_refresh_in_background,
)
from .profile_triage import start_profile_triage_in_background
from .protocols import ProtocolError, find_ours
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


def _dismiss_redirect(request):
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        return redirect(next_url)
    return redirect("tender_selection:list")


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


def _risk_badge(tender) -> dict:
    """Светофор по итоговому уровню оценки, а пока её нет — её состояние."""
    if tender.risk_error:
        return {"state": "error", "text": "риск: ошибка"}
    if not tender.risk_checked_at or "risk_factors" not in (tender.risk_assessment or {}):
        return {"state": "pending", "text": "риск: ожидает"}
    return {
        "low": {"state": "ok", "text": "риск: низкий"},
        "medium": {"state": "warn", "text": "риск: средний"},
        "high": {"state": "error", "text": "риск: высокий"},
    }.get((tender.risk_assessment or {}).get("risk_level"), {"state": "ok", "text": "риск: оценён"})


def _found_tender_card(tender):
    """Карточка «Входящие»/«Проверка» — тендер ещё не отправлен в расчёт.

    Решение «вперёд» (На оценку рисков / В расчёт) принимается только внутри
    самого тендера, не с карточки — рано жать кнопку, не открыв, что внутри.
    На карточке остаётся лишь маленький «×» скрыть (по ховеру).

    Оценка риска не показывается, пока тендер не прошёл «Входящие» — на
    этой стадии её ещё не считали (расчёт запускается при открытии карточки
    на стадии «Проверка»), нечего показывать раньше времени."""
    reviewed = tender.review != Tender.UNREVIEWED
    status_key = "error" if tender.risk_error else "assessed" if tender.risk_checked_at else "new"
    badges = [_risk_badge(tender)] if reviewed else []
    now = timezone.now()
    is_soon = bool(tender.collecting_finished_at and now <= tender.collecting_finished_at <= now + timedelta(days=1))
    org = getattr(tender, "org", None)
    customer = (org.name if org and org.name else "") or (f"ИНН {tender.customer_inn}" if tender.customer_inn else "")
    return {
        "kind": "found",
        "pk": tender.pk,
        "title": tender.title or tender.object_info,
        "customer": customer,
        "law_label": tender.get_law_display(),
        "purchase_number": tender.purchase_number,
        "max_price": tender.max_price,
        "deadline": tender.collecting_finished_at,
        "is_soon": is_soon,
        "status_label": "",
        "status_key": status_key,
        "badges": badges,
        "detail_url": f"{reverse('tender_selection:detail', args=[tender.pk])}?return_to=board",
        "dismiss_url": reverse("tender_selection:dismiss", args=[tender.pk]),
    }


def _estimate_card(estimate):
    """Карточка «Расчёт»/«Торги»/«Результат» — просчёт, откуда бы он ни пришёл
    (перенесён из подбора или создан вручную импортом в самих «Тендерах»).

    Бейджи накапливаются по мере продвижения, не заменяют друг друга и идут в
    порядке стадий: риск с «Оценки», ROI с «Расчёта», «Выигран»/«Проигран» с «Результата»."""
    summary = estimate.summary_snapshot or {}
    badges = []
    # Расчёт есть — «Оценка» пройдена, даже если тендер перенесён по старой схеме без отметки «в работу».
    if estimate.tender_id:
        badges.append(_risk_badge(estimate.tender))
    if summary.get("roi") is not None:
        from decimal import Decimal, InvalidOperation

        from tenders.services import roi_thresholds

        if summary.get("is_incomplete", True):
            # Себестоимость ещё не досчитана до конца — ROI не окончательный, не красим.
            roi_state = "pending"
        else:
            try:
                good, thin = roi_thresholds()
                roi_value = Decimal(str(summary["roi"]))
                roi_state = "ok" if roi_value >= good else "warn" if roi_value >= thin else "error"
            except InvalidOperation:
                roi_state = "pending"
        badges.append({"state": roi_state, "text": f"ROI {summary['roi']}%"})
    tender = estimate.tender
    outcome_status = tender.outcome_status if tender else Tender.OUTCOME_DRAFT
    if tender and tender.outcome_status == Tender.OUTCOME_WON:
        badges.append({"state": "ok", "text": "Выигран"})
    elif tender and tender.outcome_status == Tender.OUTCOME_LOST:
        badges.append({"state": "error", "text": "Проигран"})
    # Карточка ведёт на страницу ТЕНДЕРА (с растущими блоками по стадиям), а не
    # сразу в рабочее пространство расчёта — туда только через кнопку «Перейти
    # в расчёт» внутри блока «Расчёт» на самой странице тендера.
    detail_url = f"{reverse('tender_selection:detail', args=[estimate.tender_id])}?return_to=board" if estimate.tender_id else ""
    return {
        "kind": "estimate",
        "pk": estimate.pk,
        "title": (tender.title or tender.object_info) if tender else estimate.name,
        "customer": (
            (getattr(tender, "org", None).name if getattr(tender, "org", None) and tender.org.name else "")
            or (f"ИНН {tender.customer_inn}" if tender and tender.customer_inn else "")
        ),
        "purchase_number": tender.purchase_number if tender else estimate.tender_number,
        "tender_number": tender.purchase_number if tender else estimate.tender_number,
        "max_price": tender.max_price if tender else None,
        "deadline": tender.collecting_finished_at if tender else None,
        "is_soon": bool(
            tender and tender.collecting_finished_at
            and timezone.now() <= tender.collecting_finished_at <= timezone.now() + timedelta(days=1)
        ),
        "status": outcome_status,
        "status_label": tender.get_outcome_status_display() if tender else "",
        "status_key": outcome_status,
        "badges": badges,
        # Внесение итога живёт на странице самого тендера (tenders/home.html,
        # рядом с прогнозом снижения), не на карточке канбана — здесь только
        # уже накопленный результат в badges выше.
        "detail_url": detail_url,
        "dismiss_url": reverse("tender_selection:dismiss_estimate", args=[estimate.pk]),
        # Архивировать «в тихую» просчёт, по которому ещё не внесён итог торгов, —
        # частая случайная потеря данных; предупреждаем перед этим (см. kanban.html).
        "warn_before_dismiss": not (tender.outcome_checked_at if tender else False),
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

    def _deadline_order(dir_key, deadline_field, tie_breaker):
        if dirs[dir_key] == "asc":
            return (F(deadline_field).desc(nulls_last=True), F(tie_breaker).asc())
        return (F(deadline_field).asc(nulls_last=True), F(tie_breaker).desc())

    def _visible(base_qs, dir_key):
        qs = base_qs.order_by(*_deadline_order(dir_key, "collecting_finished_at", "first_seen_at"))
        rows, _hidden, _expired = _visible_found_tenders(
            qs, min_price=settings.min_price,
            include_words=settings.include_words, exclude_words=settings.exclude_words,
        )
        return rows

    # «Входящие» больше не колонка канбана — это отдельный список (tender_list);
    # сюда тендер попадает только после «В работу» (review != unreviewed).
    review_tenders = _visible(Tender.objects.filter(status=Tender.NEW).exclude(review=Tender.UNREVIEWED), "review")
    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in review_tenders if t.customer_inn})}
    for t in review_tenders:
        t.org = orgs.get(t.customer_inn)
    review = [
        _found_tender_card(t) for t in review_tenders
    ]

    from tenders.models import TenderEstimate

    live_estimates = TenderEstimate.objects.filter(
        tender__isnull=False,
    ).exclude(tender__status=Tender.DISMISSED).select_related("tender")
    estimate_orgs = {
        org.inn: org for org in Organization.objects.filter(
            inn__in=live_estimates.values_list("tender__customer_inn", flat=True),
        )
    }

    def estimate_card(estimate):
        estimate.tender.org = estimate_orgs.get(estimate.tender.customer_inn)
        return _estimate_card(estimate)

    calculation = [
        estimate_card(e) for e in
        live_estimates.filter(tender__outcome_status=Tender.OUTCOME_DRAFT).order_by(
            *_deadline_order("calculation", "tender__collecting_finished_at", "updated_at")
        )
    ]
    bidding = [
        estimate_card(e) for e in
        live_estimates.filter(tender__outcome_status=Tender.OUTCOME_PENDING).order_by(
            *_deadline_order("bidding", "tender__collecting_finished_at", "updated_at")
        )
    ]
    result = [
        estimate_card(e) for e in
        live_estimates.exclude(tender__outcome_status__in=(Tender.OUTCOME_DRAFT, Tender.OUTCOME_PENDING))
        .order_by(*_deadline_order("result", "tender__collecting_finished_at", "updated_at"))
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
    return render(request, "tender_selection/kanban.html", {
        "columns": columns, "archived_count": archived_count, "settings": settings,
        "nav_counts": {"incoming": _incoming_count(settings), "board": sum(len(c["cards"]) for c in columns)},
    })


def _deadline_urgency(deadline, now) -> tuple[int | None, str]:
    """(полных дней до окончания подачи, 'urgent' — сутки (последний день) | 'soon' — двое суток | '')."""
    if deadline is None or deadline < now:
        return None, ""
    days = (deadline - now).days
    return days, "urgent" if days <= 0 else "soon" if days == 1 else ""


def _incoming_count(settings) -> int:
    rows, _hidden, _expired = _visible_found_tenders(
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED), min_price=settings.min_price,
        include_words=settings.include_words, exclude_words=settings.exclude_words,
    )
    return len(rows)


def _board_count(settings) -> int:
    """Столько же карточек, сколько на доске «Торги» (см. kanban)."""
    from tenders.models import TenderEstimate

    review, _hidden, _expired = _visible_found_tenders(
        Tender.objects.filter(status=Tender.NEW).exclude(review=Tender.UNREVIEWED), min_price=settings.min_price,
        include_words=settings.include_words, exclude_words=settings.exclude_words,
    )
    estimates = TenderEstimate.objects.filter(tender__isnull=False).exclude(tender__status=Tender.DISMISSED).count()
    return len(review) + estimates


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
    show_all = request.GET.get("all") == "1"
    sort = request.GET.get("sort") if request.GET.get("sort") in SORTS else DEFAULT_SORT
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    query = request.GET.get("q", "").strip()
    now = timezone.now()

    queryset = Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED).defer(
        "raw", "clarifications_raw", "risk_assessment", "risk_assessment_docs",
    )
    if law_filter != "all":
        queryset = queryset.filter(law=law_filter)
    queryset = queryset.order_by(SORTS[sort], F("first_seen_at").desc())

    rows, hidden, expired = _visible_found_tenders(
        queryset, min_price=settings.min_price, include_words=settings.include_words,
        exclude_words=settings.exclude_words, show_all=show_all,
    )

    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in rows if t.customer_inn})}
    for tender in rows:
        tender.org = orgs.get(tender.customer_inn)
    if query:
        needle = query.casefold()
        rows = [t for t in rows if needle in " ".join([
            t.title, t.object_info, t.purchase_number, t.customer_inn, t.org.name if t.org else "",
        ]).casefold()]

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    for tender in page.object_list:
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        tender.days_left, tender.deadline_state = _deadline_urgency(tender.collecting_finished_at, now)
        tender.has_complaint = bool(tender.complaints_raw)
        # 223-ФЗ не имеет разобранного извещения по конструкции источника — это не
        # ошибка. У 44-ФЗ пустой notification_raw значит запрос ещё не удался — но
        # только если попытка вообще была (notification_checked_at): свежевыгруженный
        # тендер, который ещё никто не открывал и фон (retry_pending_notifications)
        # до него не добрался, — это не сбой, а «ещё не проверяли», значок не должен
        # гореть на буквально каждой новой карточке.
        tender.notification_missing = (
            tender.law == "fz44" and not tender.notification_raw and tender.notification_checked_at is not None
            and not tender.notification_error
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
        "query": query,
        "nav_counts": {
            "incoming": sum(
                not tender.filtered_out and (
                    tender.collecting_finished_at is None or tender.collecting_finished_at >= now
                ) for tender in rows
            ),
            "board": _board_count(settings),
        },
    })


@tender_viewer_required
def tender_detail(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    is_archived = tender.status == Tender.DISMISSED
    return_url = (
        reverse("tender_selection:archive") if is_archived or request.GET.get("from") == "archive"
        else f"{reverse('tender_selection:list')}?view=kanban" if request.GET.get("return_to") == "board"
        else reverse("tender_selection:list")
    )
    if tender.opened_at is None:  # для «жирного» непрочитанного в списке — только реальный заход, не фон
        tender.opened_at = timezone.now()
        tender.save(update_fields=["opened_at"])
    is_manual = tender.source == Tender.MANUAL
    payload = None if is_manual else notification_for(tender, force=request.GET.get("refresh") == "1")
    card = parse_notification(payload) if payload else None
    detail_document = detail_document_candidate(card.get("items", []), card.get("documents", [])) if card else None
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
        "calculation" if tender.outcome_status == Tender.OUTCOME_DRAFT else
        "bidding" if tender.outcome_status == Tender.OUTCOME_PENDING else "result"
    )

    org = Organization.objects.filter(inn=tender.customer_inn).first() if tender.customer_inn else None
    if tender.law != "fz44" and org is None and tender.customer_inn:
        org = enrich_one_org(tender.customer_inn, tender.law)  # для 223 карточки заказчика больше неоткуда взять

    if is_manual:
        clar_raw, comp_raw = [], []
    elif request.GET.get("workspace"):
        clar_raw, comp_raw = tender.clarifications_raw or [], tender.complaints_raw or []
        force_extras = request.GET.get("refresh") == "1"
        if extras_need_refresh(tender, force=force_extras):
            start_extras_refresh_in_background(tender.pk, force=force_extras)
    else:
        clar_raw, comp_raw = extras_for(tender, force=request.GET.get("refresh") == "1")

    # Прогноз снижения и оценка риска не показываются на «Входящих» — рано,
    # ещё не решили, что тендер вообще стоит смотреть; появляются вместе,
    # начиная с «Проверки» (review != unreviewed).
    stats_diag = {}
    stats = price_stats_for(tender, card, diag=stats_diag) if (is_archived or tender.review != Tender.UNREVIEWED) else None
    if stats:
        for row in stats["examples"]:
            row["region_label"] = region_name(row["region"]) if row["region"] else ""

    if is_manual:
        risk_needs_fetch = False
        risk = None
        risk_error = ""
    elif tender.review == Tender.UNREVIEWED and estimate is None:
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
        "detail_document": detail_document,
        "org": org,
        "region_label": region_name(tender.region) if tender.region else "",
        "fetch_failed": payload is None and tender.law == "fz44",
        "is_fz223": tender.law != "fz44",
        "pushed_estimate_id": estimate_id,
        "pipeline_estimate_id": estimate_id,
        "calc_verdict": calc_verdict,
        "lifecycle": lifecycle,
        "active_stage": active_stage,
        "is_archived": is_archived,
        "archive_stage_label": {key: label for key, label, _ in ARCHIVE_STAGES}.get(_archive_stage(tender, estimate), ""),
        "back_to_archive": is_archived or request.GET.get("from") == "archive",
        "return_url": return_url,
        "forecast_stat": ContractStat.objects.filter(law=tender.law, purchase_number=tender.purchase_number, own_funnel=True).first(),
        "estimate": estimate,
        "bid_reduction_percent": reduction_percent_from(tender.max_price, tender.bid_price),
        "protocol": _protocol_view(tender),
        "show_outcome": bool(
            (estimate and tender.outcome_status != Tender.OUTCOME_DRAFT)
            or (is_archived and (
                tender.protocol or tender.contract_price is not None
                or tender.contract_reduction_percent is not None or tender.outcome_checked_at
            ))
        ),
        "clarifications": parse_clarifications(clar_raw),
        "complaints": parse_complaints(comp_raw),
        "price_stats": stats,
        "price_stats_diag": stats_diag or None,
        "risk_needs_fetch": risk_needs_fetch,
        "risk": risk,
        "risk_error": risk_error,
        "risk_docs": [] if risk_needs_fetch else tender.risk_assessment_docs,
    })


def _protocol_view(tender):
    """Таблица участников итогового протокола; наша заявка отмечена, если опознана."""
    protocol = tender.protocol or {}
    if not protocol:
        return None
    nmck = protocol.get("nmck") or tender.max_price
    ours = find_ours(protocol, bid_number=tender.bid_number, bid_price=tender.bid_price)
    rows = []
    for participant in sorted(protocol.get("participants", []), key=lambda p: (p.get("rank") is None, p.get("rank") or 0)):
        price = Decimal(participant["price"]) if participant.get("price") else None
        rows.append({
            **participant,
            "price": price,
            "reduction": reduction_percent_from(nmck, price) if price is not None else None,
            "is_ours": participant is ours,
        })
    return {**protocol, "rows": rows, "ours": ours}


def _tender_lifecycle(tender, estimate):
    """Короткий ориентир на карточке: этапы, а не вторая навигация."""
    status = tender.outcome_status if estimate else ""
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
        settings.profile_triage_enabled = request.POST.get("profile_triage_enabled") == "1"
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
        "laws": [(code, label, code in chosen_laws) for code, label in Tender.LAW_CHOICES],
        "using_defaults": not settings.okpd2_codes,
    })


RISK_FACTOR_LABELS = (
    ("samples_required", "Требуются образцы или испытания"),
    ("samples_impossible", "Образцы нужны в нереальный срок"),
    ("national_confirmation", "Нацрежим: нужно подтвердить происхождение"),
    ("national_blocked", "Нацрежим: подтверждение недоступно"),
    ("delivery_requests", "Поставка по заявкам заказчика"),
    ("delivery_open_ended", "Поставка по заявкам без ясного срока или объёма"),
)
RISK_LEVEL_CHOICES = (("low", "низкий"), ("medium", "средний"), ("high", "высокий"))
EVALUATION_NUMBERS = (
    ("risk_warning_days", "Короткий срок", int, 1, 365),
    ("risk_critical_days", "Критический срок", int, 1, 365),
    ("roi_good_percent", "ROI зелёной зоны", Decimal, 0, 1000),
    ("roi_thin_percent", "ROI жёлтой зоны", Decimal, 0, 1000),
    ("vat_rate", "НДС", Decimal, 0, 50),
    ("default_reduction_percent", "Снижение по умолчанию", Decimal, 0, 100),
    ("stats_target_count", "Похожих закупок для прогноза", int, 1, 100),
    ("stats_min_samples", "Минимум закупок для прогноза", int, 1, 100),
    ("reduction_hint_min", "Подсказка снижения не меньше", int, 0, 100),
    ("reduction_hint_max", "Подсказка снижения не больше", int, 0, 100),
    ("incoming_ttl_days", "Хранить просроченные «Входящие»", int, 1, 365),
)
_TENDER_SETTINGS_FIELDS = ("roi_good_percent", "roi_thin_percent", "vat_rate", "default_reduction_percent")


def _parse_evaluation_settings(post) -> tuple[dict, list[str]]:
    """Значения формы «Настройки оценки» и список ошибок; пустой список — можно сохранять."""
    values, errors = {}, []
    for name, label, kind, low, high in EVALUATION_NUMBERS:
        raw = (post.get(name) or "").strip().replace(",", ".")
        try:
            value = kind(raw)
        except (ValueError, InvalidOperation):
            errors.append(f"{label}: нужно число.")
            continue
        if not low <= value <= high:
            errors.append(f"{label}: допустимо от {low} до {high}.")
        values[name] = value
    levels = {}
    for key, label in RISK_FACTOR_LABELS:
        level = post.get(f"level_{key}", "")
        if level not in dict(RISK_LEVEL_CHOICES):
            errors.append(f"{label}: выберите уровень риска.")
        levels[key] = level
    values["risk_factor_levels"] = levels
    if errors:
        return values, errors
    if values["risk_critical_days"] >= values["risk_warning_days"]:
        errors.append("Критический срок должен быть меньше короткого.")
    if values["roi_thin_percent"] >= values["roi_good_percent"]:
        errors.append("Граница жёлтой зоны ROI должна быть ниже зелёной.")
    if values["stats_min_samples"] > values["stats_target_count"]:
        errors.append("Минимум закупок для прогноза не может быть больше их целевого числа.")
    if values["reduction_hint_min"] >= values["reduction_hint_max"]:
        errors.append("Нижняя граница подсказки снижения должна быть меньше верхней.")
    return values, errors


@superuser_required
def evaluation_settings(request):
    """Все числа, по которым оцениваются тендеры: пороги сроков и уровни факторов
    риска, светофор ROI (из него же целевая и минимальная цена на торгах), НДС и
    снижение по умолчанию для расчёта, прогноз снижения, срок хранения «Входящих»."""
    from tenders.models import TenderSettings

    from .risk_policy import DEFAULT_FACTOR_LEVELS

    filters = FilterSettings.load()
    tender_settings = TenderSettings.objects.get_or_create(pk=1)[0]
    form = {
        name: getattr(tender_settings if name in _TENDER_SETTINGS_FIELDS else filters, name)
        for name, *_ in EVALUATION_NUMBERS
    }
    levels = {**DEFAULT_FACTOR_LEVELS, **(filters.risk_factor_levels or {})}
    if request.method == "POST":
        values, errors = _parse_evaluation_settings(request.POST)
        if not errors:
            for name, value in values.items():
                setattr(tender_settings if name in _TENDER_SETTINGS_FIELDS else filters, name, value)
            filters.save()
            tender_settings.save()
            messages.success(request, "Настройки оценки сохранены.")
            return redirect("tender_selection:evaluation_settings")
        for error in errors:
            messages.error(request, error)
        form = {name: request.POST.get(name, "") for name in form}
        levels = values["risk_factor_levels"]
    return render(request, "tender_selection/evaluation_settings.html", {
        "form": form,
        "factor_levels": [(key, label, levels.get(key, "")) for key, label in RISK_FACTOR_LABELS],
        "level_choices": RISK_LEVEL_CHOICES,
    })


@superuser_required
def word_audit_page(request):
    """Статистика плюс/минус-слов (без ИИ) и последний AI-аудит с галочками для применения."""
    from . import word_audit
    from .models import WordAudit

    return render(request, "tender_selection/word_audit.html", {
        "stats": word_audit.word_stats(),
        "audit": WordAudit.objects.first(),
        "settings": FilterSettings.load(),
    })


@superuser_required
@require_POST
def word_audit_run(request):
    from . import word_audit
    from .ai_gateway import AIGatewayError

    try:
        audit = word_audit.run_audit(request.user)
    except AIGatewayError as exc:
        messages.error(request, f"Аудит не выполнен: {exc}")
    else:
        spent = f" Потрачено {audit.spend_rub} ₽." if audit.spend_rub is not None else ""
        messages.success(request, f"Аудит готов.{spent}")
    return redirect("tender_selection:word_audit")


@superuser_required
@require_POST
def word_audit_apply(request):
    from . import word_audit

    chosen = {kind: [w for w in request.POST.getlist(kind) if w.strip()] for kind in word_audit.SUGGESTION_KINDS}
    if not any(chosen.values()):
        messages.info(request, "Ничего не выбрано.")
        return redirect("tender_selection:word_audit")
    word_audit.apply_words(**chosen)
    messages.success(request, "Фильтр обновлён: " + ", ".join(
        f"{label} {len(chosen[kind])}" for kind, label in (
            ("add_plus", "+плюс"), ("add_minus", "+минус"), ("remove_plus", "−плюс"), ("remove_minus", "−минус"),
        ) if chosen[kind]
    ) + ".")
    return redirect("tender_selection:word_audit")


@superuser_required
@require_POST
def profile_triage_run(request):
    settings = FilterSettings.load()
    if not settings.profile_triage_enabled:
        messages.info(request, "Сначала включите Jev-проверку в настройках отбора.")
        return redirect("tender_selection:list")
    rows, _hidden, _expired = _visible_found_tenders(
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED, profile_checked_at__isnull=True),
        min_price=settings.min_price, include_words=settings.include_words, exclude_words=settings.exclude_words,
    )
    ids = [tender.pk for tender in rows[:100]]
    start_profile_triage_in_background(ids)
    messages.success(request, f"Jev начал проверку {len(ids)} входящих. Отметки появятся после обновления страницы.")
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


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
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
        return redirect("tender_selection:detail", pk=tender.pk)
    try:
        push_to_estimate(tender, request.user)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось создать просчёт: {exc}")
        return redirect("tender_selection:detail", pk=pk)
    messages.success(request, "Просчёт создан. Позиции подставлены из извещения.")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect("tender_selection:detail", pk=tender.pk)


@superuser_required
@require_POST
def enter_outcome(request, pk):
    """Обязательный шаг канбана для карточек «Торги» — забрать факт торгов
    (автоматически через ГосПлан) или подтвердить его вручную, если по номеру
    закупки контракт ещё не найден или свой ИНН не настроен (COMPANY_INN)."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    tender = estimate.tender
    manual_status = request.POST.get("status")
    manual_reduction = request.POST.get("actual_reduction_percent", "").strip()
    try:
        reduction_percent = Decimal(manual_reduction) if manual_reduction else None
        if reduction_percent is not None and not Decimal("0") <= reduction_percent <= Decimal("100"):
            raise InvalidOperation
    except InvalidOperation:
        messages.error(request, "Фактическое снижение должно быть числом от 0 до 100.")
        return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")

    lifecycle_changed = manual_status in (Tender.OUTCOME_WON, Tender.OUTCOME_LOST, Tender.OUTCOME_NOT_PARTICIPATED)
    if lifecycle_changed:
        apply_tender_outcome(
            estimate, status=manual_status, reduction_percent=reduction_percent,
            source=Tender.OUTCOME_MANUAL,
        )
        messages.success(request, f"Итог внесён вручную: {tender.get_outcome_status_display()}.")
    else:
        try:
            protocol_found = check_protocol(tender)
        except ProtocolError as exc:
            protocol_found = False
            messages.warning(request, f"ЕИС сейчас не ответил ({exc}) — проверю протокол позже автоматически.")
        if protocol_found:
            tender.refresh_from_db()
            messages.success(request, f"Итоговый протокол загружен из ЕИС: {tender.get_outcome_status_display()}.")
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
            return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
        outcome = fetch_tender_outcome(tender)
        if not outcome.get("found"):
            messages.warning(request, "Контракт по этому номеру закупки в реестре пока не найден — попробуйте позже или внесите итог вручную.")
        elif outcome.get("auto_status"):
            apply_tender_outcome(
                estimate, status=outcome["auto_status"], price=outcome.get("price"),
                reduction_percent=outcome.get("reduction_percent"), source=Tender.OUTCOME_AUTO,
                reg_num=outcome.get("reg_num"), exe_start=outcome.get("exe_start"), exe_end=outcome.get("exe_end"),
            )
            tender.refresh_from_db()
            lifecycle_changed = True
            messages.success(request, f"Итог найден автоматически: {tender.get_outcome_status_display()}.")
        else:
            tender.contract_price = outcome.get("price")
            tender.contract_reduction_percent = outcome.get("reduction_percent")
            tender.outcome_checked_at = timezone.now()
            tender.save(update_fields=["contract_price", "contract_reduction_percent", "outcome_checked_at"])
            messages.info(request, "Цена контракта найдена, но выиграли мы или нет — решите сами кнопками ниже (свой ИНН не настроен).")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        if lifecycle_changed:
            return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
        return JsonResponse({"lifecycle_changed": False, "refresh_detail": True})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def save_bid(request, pk):
    """Номер и/или сумма нашей заявки — по ним находим себя в протоколе."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    raw_price = request.POST.get("bid_price", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        bid_price = Decimal(raw_price) if raw_price else None
        if bid_price is not None and bid_price <= 0:
            raise InvalidOperation
    except InvalidOperation:
        messages.error(request, "Сумма заявки должна быть положительным числом.")
        return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
    set_our_bid(estimate, bid_number=request.POST.get("bid_number", ""), bid_price=bid_price)
    messages.success(request, "Данные нашей заявки сохранены.")
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")


@superuser_required
@require_POST
def dismiss(request, pk):
    tender = get_object_or_404(Tender, pk=pk)
    reason = request.POST.get("reason")
    if tender.review == Tender.UNREVIEWED and reason in dict(TenderDismissalFeedback.REASON_CHOICES):
        TenderDismissalFeedback.objects.create(tender=tender, reason=reason)
    _clear_tender_document_previews(tender)
    archive_tender(tender)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return _dismiss_redirect(request)


@superuser_required
@require_POST
def dismiss_estimate(request, pk):
    """Архивировать карточку Tender со стадии расчёта, торгов или результата."""
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    tender = get_object_or_404(Tender, pk=estimate.tender_id)
    _clear_tender_document_previews(tender)
    archive_tender(tender)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return _dismiss_redirect(request)


@superuser_required
@require_POST
def restore(request, pk):
    tender = get_object_or_404(Tender, pk=pk, status=Tender.DISMISSED)
    # Со стадий расчёта и дальше — обратно на доску, а не во «Входящие».
    tender.status = Tender.PUSHED if tender.estimates.exists() else Tender.NEW
    tender.archived_at = None
    tender.archived_from_stage = ""
    tender.save(update_fields=["status", "archived_at", "archived_from_stage"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect(f"{reverse('tender_selection:list')}?view=kanban")


@superuser_required
@require_POST
def restore_estimate(request, pk):
    from tenders.models import TenderEstimate

    estimate = get_object_or_404(TenderEstimate, pk=pk)
    return restore(request, estimate.tender_id)


@superuser_required
@require_POST
def toggle_forecast(request, pk):
    tender = get_object_or_404(Tender, pk=pk, status=Tender.DISMISSED)
    stat = ContractStat.objects.filter(law=tender.law, purchase_number=tender.purchase_number, own_funnel=True).first()
    if stat is None:
        messages.warning(request, "Результат торгов ещё не попал в статистику прогноза.")
    else:
        stat.forecast_included = not stat.forecast_included
        stat.save(update_fields=["forecast_included"])
        messages.success(request, "Результат добавлен в прогноз." if stat.forecast_included else "Результат исключён из прогноза.")
    return redirect(f"{reverse('tender_selection:detail', args=[pk])}?from=archive")


# Причину скрытия не спрашиваем: её задаёт стадия, на которой тендер ушёл в архив.
ARCHIVE_STAGES = (
    ("incoming", "Входящие", "не наш профиль"),
    ("evaluation", "Оценка", "риски"),
    ("calculation", "Расчёт", "нерентабельно"),
    ("bidding", "Торги", "без итога"),
    ("published", "Итог без нас", "наша заявка не отмечена"),
    ("lost", "Проиграли", ""),
    # Пока нет «Исполнения», выигранные тоже уходят сюда.
    ("won", "Выиграли", ""),
)
ARCHIVE_SORTS = {
    "archived": F("archived_at").desc(nulls_last=True),
    "price_hi": SORTS["price_hi"],
    "price_lo": SORTS["price_lo"],
    "published": SORTS["new"],
}


def _archive_stage(tender, estimate) -> str:
    if tender.archived_from_stage:
        return tender.archived_from_stage
    return archive_stage_for(tender)


@superuser_required
def archive(request):
    """Архив скрытых тендеров: поиск, фильтр по стадии скрытия (она же причина),
    закону и итогу торгов, с возможностью восстановления."""
    from tenders.models import TenderEstimate

    query = request.GET.get("q", "").strip()
    stage_filter = request.GET.get("stage") if request.GET.get("stage") in dict((k, 1) for k, *_ in ARCHIVE_STAGES) else "all"
    law_filter = request.GET.get("law") if request.GET.get("law") in ("fz44", "fz223") else "all"
    sort = request.GET.get("sort") if request.GET.get("sort") in ARCHIVE_SORTS else "archived"

    selection_settings = FilterSettings.load()
    tenders = Tender.objects.filter(status=Tender.DISMISSED)
    if law_filter != "all":
        tenders = tenders.filter(law=law_filter)
    tenders = list(tenders.order_by(ARCHIVE_SORTS[sort], "-pk"))
    orgs = {o.inn: o for o in Organization.objects.filter(inn__in={t.customer_inn for t in tenders if t.customer_inn})}
    latest_estimate = {}
    for estimate in TenderEstimate.objects.filter(tender__in=tenders).order_by("updated_at"):
        latest_estimate[estimate.tender_id] = estimate

    needle = query.casefold()
    rows = []
    for tender in tenders:
        tender.org = orgs.get(tender.customer_inn)
        haystack = " ".join([
            tender.title, tender.object_info, tender.purchase_number, tender.customer_inn,
            tender.org.name if tender.org else "",
        ]).casefold()
        if needle and needle not in haystack:
            continue
        estimate = latest_estimate.get(tender.pk)
        rows.append({"tender": tender, "estimate": estimate, "stage": _archive_stage(tender, estimate)})

    stage_counts = {key: 0 for key, *_ in ARCHIVE_STAGES}
    for row in rows:
        stage_counts[row["stage"]] += 1
    stages = [{"key": "all", "label": "Все", "hint": "", "count": len(rows)}] + [
        {"key": key, "label": label, "hint": hint, "count": stage_counts[key]} for key, label, hint in ARCHIVE_STAGES
    ]
    if stage_filter != "all":
        rows = [row for row in rows if row["stage"] == stage_filter]

    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    stage_labels = {key: label for key, label, _ in ARCHIVE_STAGES}
    for row in page.object_list:
        tender, estimate = row["tender"], row["estimate"]
        tender.region_label = region_name(tender.region) if tender.region else ""
        tender.law_label = LAW_LABELS.get(tender.law, tender.law)
        row["stage_label"] = stage_labels[row["stage"]]
        protocol = tender.protocol or {}
        row["ours"] = find_ours(protocol, bid_number=tender.bid_number, bid_price=tender.bid_price) if protocol else None
        row["participants"] = len(protocol.get("participants", []))

    return render(request, "tender_selection/archive.html", {
        "page_obj": page,
        "stages": stages,
        "stage_filter": stage_filter,
        "law_filter": law_filter,
        "sort": sort,
        "query": query,
        "nav_counts": {"incoming": _incoming_count(selection_settings), "board": _board_count(selection_settings)},
    })


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
        return JsonResponse({"lifecycle_changed": True, "tender_id": tender.pk})
    return redirect(request.META.get("HTTP_REFERER") or "tender_selection:list")
