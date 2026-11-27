from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch
from django.http import Http404, JsonResponse
import json
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from tender_selection.models import Tender

from .models import WorkBoard, WorkBoardCard, WorkBoardColumn
from .workboards import (add_column, create_board, create_task_card, create_tender_card, move_card,
                         task_progress, edit_task_work, create_task_from_plan)


def _visible_boards(user):
    boards = WorkBoard.objects.all() if user.is_superuser else WorkBoard.objects.filter(created_by=user)
    return boards.order_by("name", "pk")


def _board_for_user(user, board_id):
    board = get_object_or_404(WorkBoard, pk=board_id)
    if not user.is_superuser and board.created_by_id != user.id:
        raise PermissionDenied
    return board


def _error_text(error):
    if hasattr(error, "message_dict"):
        return " ".join(message for values in error.message_dict.values() for message in values)
    return " ".join(error.messages)


def _board_redirect(board):
    return redirect(f"{reverse('workboard_list')}?board={board.pk}")


@login_required
def workboard_list(request):
    boards = list(_visible_boards(request.user))
    selected = None
    requested_board = request.GET.get("board")
    if requested_board:
        try:
            selected = next(board for board in boards if str(board.pk) == requested_board)
        except StopIteration as error:
            raise Http404 from error
    elif boards:
        selected = boards[0]

    columns = []
    if selected:
        columns = list(
            WorkBoardColumn.objects.filter(board=selected).prefetch_related(
                Prefetch(
                    "cards",
                    queryset=WorkBoardCard.objects.select_related("task", "tender").prefetch_related("task__commercial_items__route_plans__steps__checkpoints").order_by("position", "pk"),
                    to_attr="visible_cards",
                )
            )
        )

    for column in columns:
        for card in column.visible_cards:
            if card.task_id:
                card.progress = task_progress(card.task)

    return render(request, "tenders/workboards.html", {
        "boards": boards,
        "selected_board": selected,
        "columns": columns,
    })


@login_required
@require_POST
def workboard_create(request):
    try:
        board = create_board(request.user, request.POST.get("name"))
    except ValidationError as error:
        messages.error(request, _error_text(error))
        return redirect("workboard_list")
    return _board_redirect(board)


@login_required
@require_POST
def workboard_column_create(request, board_id):
    board = _board_for_user(request.user, board_id)
    try:
        add_column(board, request.POST.get("name"))
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return _board_redirect(board)


@login_required
@require_POST
def workboard_task_create(request, board_id):
    board = _board_for_user(request.user, board_id)
    try:
        create_task_card(
            board,
            name=request.POST.get("name"),
            description=request.POST.get("description", ""),
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return _board_redirect(board)


@login_required
@require_POST
def workboard_tender_add(request, tender_id):
    if not request.user.is_superuser:
        raise PermissionDenied
    tender = get_object_or_404(Tender, pk=tender_id, outcome_status=Tender.OUTCOME_WON)
    board = get_object_or_404(_visible_boards(request.user), pk=request.POST.get("board_id"))
    try:
        card = create_tender_card(board, tender)
        messages.success(request, "Тендер передан в исполнение.")
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect(reverse('tender_selection:detail', args=[tender.pk]) + ('?workspace=1' if request.GET.get('workspace') == '1' else ''))


@login_required
@require_POST
def workboard_card_move(request, card_id):
    card = get_object_or_404(WorkBoardCard.objects.select_related("board"), pk=card_id)
    board = _board_for_user(request.user, card.board_id)
    column = get_object_or_404(WorkBoardColumn, pk=request.POST.get("column_id"), board=board)
    try:
        move_card(card, column)
    except ValidationError as error:
        messages.error(request, _error_text(error))
    if request.POST.get("from_task") == "1" and card.task_id:
        return redirect("workboard_task_detail", card_id=card.pk)
    return _board_redirect(board)


@login_required
def workboard_task_detail(request, card_id):
    card = get_object_or_404(WorkBoardCard.objects.select_related("task", "board", "column"), pk=card_id, task__isnull=False)
    _board_for_user(request.user, card.board_id)
    return render(request, "tenders/workboard_task.html", {
        "card": card, "progress": task_progress(card.task), "columns": card.board.columns.all(),
    })


@login_required
@require_POST
def workboard_task_edit(request, card_id):
    card = get_object_or_404(WorkBoardCard, pk=card_id, task__isnull=False)
    _board_for_user(request.user, card.board_id)
    try:
        edit_task_work(card, request.user, request.POST.get("action"), request.POST)
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect("workboard_task_detail", card_id=card.pk)


@login_required
@require_POST
def workboard_task_import(request, board_id):
    board = _board_for_user(request.user, board_id)
    try:
        if request.content_type == "application/json":
            raw = request.body
        elif request.FILES.get("plan_file"):
            raw = request.FILES["plan_file"].read(256001)
        else:
            raw = request.POST.get("plan", "")
        if len(raw) > 256000:
            raise ValidationError("Файл задачи слишком большой.")
        payload = json.loads(raw)
        card = create_task_from_plan(board, request.user, payload)
    except (ValueError, ValidationError) as error:
        message = _error_text(error) if isinstance(error, ValidationError) else "Некорректный JSON."
        if request.content_type == "application/json":
            return JsonResponse({"error": message}, status=400)
        messages.error(request, message)
        return _board_redirect(board)
    if request.content_type == "application/json":
        return JsonResponse({"card_id": card.pk, "url": reverse("workboard_task_detail", args=[card.pk])}, status=201)
    return redirect("workboard_task_detail", card_id=card.pk)
