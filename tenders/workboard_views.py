from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from tender_selection.models import Tender

from .models import WorkBoard, WorkBoardCard, WorkBoardColumn
from .workboards import add_column, create_board, create_task_card, create_tender_card, move_card


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
    available_tenders = Tender.objects.none()
    if selected:
        columns = list(
            WorkBoardColumn.objects.filter(board=selected).prefetch_related(
                Prefetch(
                    "cards",
                    queryset=WorkBoardCard.objects.select_related("task", "tender").order_by("position", "pk"),
                    to_attr="visible_cards",
                )
            )
        )
        if request.user.is_superuser:
            available_tenders = Tender.objects.filter(
                outcome_status=Tender.OUTCOME_WON,
                workboard_card__isnull=True,
            ).order_by("-outcome_checked_at", "-pk")

    return render(request, "tenders/workboards.html", {
        "boards": boards,
        "selected_board": selected,
        "columns": columns,
        "available_tenders": available_tenders,
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
def workboard_tender_add(request, board_id):
    board = _board_for_user(request.user, board_id)
    if not request.user.is_superuser:
        raise PermissionDenied
    tender = get_object_or_404(Tender, pk=request.POST.get("tender_id"))
    try:
        create_tender_card(board, tender)
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return _board_redirect(board)


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
    return _board_redirect(board)
