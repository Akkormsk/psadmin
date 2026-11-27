from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from .models import Task, WorkBoard, WorkBoardCard, WorkBoardColumn


def _clean_name(value, *, label, max_length):
    name = str(value or "").strip()
    if not name:
        raise ValidationError(f"Укажите {label}.")
    if len(name) > max_length:
        raise ValidationError(f"{label.capitalize()} слишком длинное.")
    return name


@transaction.atomic
def create_board(user, name):
    board = WorkBoard.objects.create(
        created_by=user,
        name=_clean_name(name, label="название доски", max_length=WorkBoard._meta.get_field("name").max_length),
    )
    WorkBoardColumn.objects.bulk_create([
        WorkBoardColumn(
            board=board, name=WorkBoardColumn.DEFAULT_QUEUE_NAME,
            kind=WorkBoardColumn.Kind.QUEUE, position=0,
        ),
        WorkBoardColumn(
            board=board, name=WorkBoardColumn.DEFAULT_DONE_NAME,
            kind=WorkBoardColumn.Kind.DONE, position=1,
        ),
    ])
    return board


@transaction.atomic
def add_column(board, name):
    done = WorkBoardColumn.objects.select_for_update().get(board=board, kind=WorkBoardColumn.Kind.DONE)
    position = done.position
    done.position = position + 1
    done.save(update_fields=["position"])
    return WorkBoardColumn.objects.create(
        board=board,
        name=_clean_name(name, label="название колонки", max_length=WorkBoardColumn._meta.get_field("name").max_length),
        kind=WorkBoardColumn.Kind.STANDARD,
        position=position,
    )


def _next_card_position(column):
    maximum = column.cards.aggregate(value=Max("position"))["value"]
    return 0 if maximum is None else maximum + 1


@transaction.atomic
def create_task_card(board, *, name, description=""):
    queue = WorkBoardColumn.objects.select_for_update().get(board=board, kind=WorkBoardColumn.Kind.QUEUE)
    task = Task.objects.create(
        name=_clean_name(name, label="название задачи", max_length=Task._meta.get_field("name").max_length),
        description=str(description or "").strip(),
    )
    return WorkBoardCard.objects.create(
        board=board, column=queue, task=task, position=_next_card_position(queue),
    )


@transaction.atomic
def create_tender_card(board, tender):
    queue = WorkBoardColumn.objects.select_for_update().get(board=board, kind=WorkBoardColumn.Kind.QUEUE)
    return WorkBoardCard.objects.create(
        board=board, column=queue, tender=tender, position=_next_card_position(queue),
    )


@transaction.atomic
def move_card(card, column):
    locked = WorkBoardCard.objects.select_for_update().get(pk=card.pk)
    if column.board_id != locked.board_id:
        raise ValidationError("Нельзя перенести карточку на чужую доску через выбор колонки.")
    locked.column = column
    locked.position = _next_card_position(column)
    locked.save(update_fields=["column", "position", "updated_at"])
    return locked
