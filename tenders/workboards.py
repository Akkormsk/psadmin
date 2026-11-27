from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
import uuid
import hashlib
import json

from .models import (Task, WorkBoard, WorkBoardCard, WorkBoardColumn, TenderCommercialItem,
                     ComponentRoutePlan, ComponentOperationStep, WorkCheckpoint, WorkCheckpointEvent)


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
    # Serialize attempts even when different destination boards are selected.
    tender = type(tender).objects.select_for_update().get(pk=tender.pk)
    if WorkBoardCard.objects.filter(tender=tender).exists():
        raise ValidationError("Тендер уже передан в исполнение.")
    queue = WorkBoardColumn.objects.select_for_update().get(board=board, kind=WorkBoardColumn.Kind.QUEUE)
    return WorkBoardCard.objects.create(
        board=board, column=queue, tender=tender, position=_next_card_position(queue),
    )


@transaction.atomic
def move_card(card, column):
    locked = WorkBoardCard.objects.select_for_update().get(pk=card.pk)
    if column.board_id != locked.board_id:
        raise ValidationError("Нельзя перенести карточку на чужую доску через выбор колонки.")
    if locked.task_id and column.kind == WorkBoardColumn.Kind.DONE:
        progress = task_progress(locked.task)
        if progress["total"] and progress["done"] != progress["total"]:
            raise ValidationError("Сначала завершите обязательные критерии всех подзадач.")
    locked.column = column
    locked.position = _next_card_position(column)
    locked.save(update_fields=["column", "position", "updated_at"])
    return locked


def task_progress(task):
    items = []
    for item in task.commercial_items.order_by("position", "pk"):
        if item.status != "active":
            continue
        plans = list(item.route_plans.all())
        # Task MVP has one position-level route; never choose an ambiguous plan.
        plan = plans[0] if len(plans) == 1 else None
        steps = []
        blocked = False
        for step in plan.steps.all() if plan else []:
            checks = list(step.checkpoints.all())
            mandatory = [c for c in checks if c.required]
            done = bool(mandatory) and all(c.completed_at for c in mandatory)
            steps.append({"step": step, "checks": checks, "done": done and not blocked,
                          "blocked": blocked, "done_count": sum(bool(c.completed_at) for c in mandatory),
                          "total": len(mandatory)})
            blocked = blocked or not done
        items.append({"item": item, "steps": steps, "done": bool(steps) and all(s["done"] for s in steps),
                      "done_count": sum(s["done_count"] for s in steps), "total": sum(s["total"] for s in steps)})
    done = sum(i["done"] for i in items)
    return {"items": items, "done": done, "total": len(items), "percent": round(100 * done / len(items)) if items else None}


def _lock_task_card(card, actor):
    locked = WorkBoardCard.objects.select_for_update().get(pk=card.pk)
    from django.core.exceptions import PermissionDenied
    if not actor.is_superuser and locked.board.created_by_id != actor.pk:
        raise PermissionDenied
    if not locked.task_id:
        raise ValidationError("Редактирование ручного маршрута доступно только для задачи.")
    return locked


def _task_item(card, item_id):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(TenderCommercialItem, pk=item_id, task_id=card.task_id, status="active")


def _task_step(card, step_id):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(ComponentOperationStep, pk=step_id, route_plan__commercial_item__task_id=card.task_id)


def _task_check(card, checkpoint_id):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        WorkCheckpoint,
        pk=checkpoint_id,
        step__route_plan__commercial_item__task_id=card.task_id,
    )


def _save_order(rows):
    if not rows:
        return
    model = type(rows[0])
    offset = max(row.position for row in rows) + len(rows) + 1
    for index, row in enumerate(rows):
        row.position = offset + index
    model.objects.bulk_update(rows, ["position"])
    for index, row in enumerate(rows):
        row.position = index
    model.objects.bulk_update(rows, ["position"])


def _move_in_order(queryset, target_id, direction):
    if direction not in ("up", "down"):
        raise ValidationError("Неизвестное направление перемещения.")
    rows = list(queryset.select_for_update().order_by("position", "pk"))
    try:
        index = next(index for index, row in enumerate(rows) if row.pk == int(target_id))
    except (StopIteration, TypeError, ValueError) as error:
        raise ValidationError("Элемент не найден в этой задаче.") from error
    destination = index - 1 if direction == "up" else index + 1
    if destination < 0 or destination >= len(rows):
        return
    rows[index], rows[destination] = rows[destination], rows[index]
    _save_order(rows)


def _delete_checkpoints(checkpoints):
    WorkCheckpointEvent.objects.filter(checkpoint__in=checkpoints).delete()
    checkpoints.delete()


def _reopen_if_needed(card):
    if card.column.kind != WorkBoardColumn.Kind.DONE:
        return
    progress = task_progress(card.task)
    if progress["done"] != progress["total"]:
        target = card.board.columns.filter(kind=WorkBoardColumn.Kind.STANDARD).first()
        target = target or card.board.columns.get(kind=WorkBoardColumn.Kind.QUEUE)
        card.column = target
        card.position = _next_card_position(target)
        card.save(update_fields=["column", "position", "updated_at"])


@transaction.atomic
def edit_task_work(card, actor, action, data):
    """Shared server executor for task card mutations, scoped to the authorized card."""
    card = _lock_task_card(card, actor)
    if action in ("edit_task", "add_item", "edit_item", "add_step", "add_check"):
        name = _clean_name(data.get("name"), label="название", max_length=300)
    if action == "edit_task":
        card.task.name = name
        card.task.description = str(data.get("description") or "").strip()
        card.task.save(update_fields=["name", "description"])
    elif action == "add_item":
        last = card.task.commercial_items.filter(status="active").aggregate(value=Max("position"))["value"]
        TenderCommercialItem.objects.create(
            task=card.task,
            source_key="task:" + uuid.uuid4().hex,
            display_name=name,
            description=str(data.get("description") or "").strip(),
            position=0 if last is None else last + 1,
        )
    elif action == "edit_item":
        item = _task_item(card, data.get("item_id"))
        item.display_name = name
        item.description = str(data.get("description") or "").strip()
        item.save(update_fields=["display_name", "description", "updated_at"])
    elif action == "add_step":
        item = _task_item(card, data.get("item_id"))
        first = _clean_name(data.get("criterion"), label="обязательный критерий", max_length=300)
        plan, _ = ComponentRoutePlan.objects.get_or_create(commercial_item=item, component=None,
                                                           scope=ComponentRoutePlan.Scope.SHARED)
        last = plan.steps.aggregate(value=Max("position"))["value"]
        step = ComponentOperationStep.objects.create(route_plan=plan, manual_name=name, position=0 if last is None else last + 1)
        WorkCheckpoint.objects.create(step=step, name=first, position=0)
    elif action == "add_check":
        step = _task_step(card, data.get("step_id"))
        last = step.checkpoints.aggregate(value=Max("position"))["value"]
        WorkCheckpoint.objects.create(step=step, name=name, position=0 if last is None else last + 1,
                                      required=data.get("required") in (True, "1", "on"))
    elif action == "move_item":
        _move_in_order(
            card.task.commercial_items.filter(status="active"),
            data.get("item_id"),
            data.get("direction"),
        )
    elif action == "move_step":
        step = _task_step(card, data.get("step_id"))
        _move_in_order(step.route_plan.steps.all(), step.pk, data.get("direction"))
    elif action == "move_check":
        checkpoint = _task_check(card, data.get("checkpoint_id"))
        _move_in_order(checkpoint.step.checkpoints.all(), checkpoint.pk, data.get("direction"))
    elif action == "delete_item":
        item = _task_item(card, data.get("item_id"))
        siblings = card.task.commercial_items.filter(status="active").exclude(pk=item.pk)
        _delete_checkpoints(WorkCheckpoint.objects.filter(step__route_plan__commercial_item=item))
        item.delete()
        _save_order(list(siblings.select_for_update().order_by("position", "pk")))
    elif action == "delete_step":
        step = _task_step(card, data.get("step_id"))
        siblings = step.route_plan.steps.exclude(pk=step.pk)
        _delete_checkpoints(step.checkpoints.all())
        step.delete()
        _save_order(list(siblings.select_for_update().order_by("position", "pk")))
    elif action == "delete_check":
        checkpoint = _task_check(card, data.get("checkpoint_id"))
        siblings = checkpoint.step.checkpoints.exclude(pk=checkpoint.pk)
        _delete_checkpoints(WorkCheckpoint.objects.filter(pk=checkpoint.pk))
        _save_order(list(siblings.select_for_update().order_by("position", "pk")))
    elif action == "toggle_check":
        checkpoint = _task_check(card, data.get("checkpoint_id"))
        completed = data.get("completed") in (True, "1", "on")
        if completed == bool(checkpoint.completed_at):
            return card
        step = checkpoint.step
        if completed:
            for previous in step.route_plan.steps.filter(position__lt=step.position).prefetch_related("checkpoints"):
                required = [c for c in previous.checkpoints.all() if c.required]
                if not required or not all(c.completed_at for c in required):
                    raise ValidationError("Сначала завершите предыдущий этап.")
        elif checkpoint.required and WorkCheckpoint.objects.filter(step__route_plan=step.route_plan,
                        step__position__gt=step.position, completed_at__isnull=False).exists():
            raise ValidationError("Сначала снимите отметки последующих этапов, чтобы сохранить историю.")
        checkpoint.completed_at = timezone.now() if completed else None
        checkpoint.completed_by = actor if completed else None
        checkpoint.save(update_fields=["completed_at", "completed_by"])
        WorkCheckpointEvent.objects.create(checkpoint=checkpoint, actor=actor, completed=completed)
    else:
        raise ValidationError("Неизвестное действие.")
    _reopen_if_needed(card)
    return card


@transaction.atomic
def create_task_from_plan(board, actor, payload):
    """Generic structured import, no project-specific seed or alternate data store."""
    from django.core.exceptions import PermissionDenied
    if not actor.is_superuser and board.created_by_id != actor.pk:
        raise PermissionDenied
    if not isinstance(payload, dict):
        raise ValidationError("Ожидается объект задачи.")
    name = _clean_name(payload.get("name"), label="название задачи", max_length=300)
    items = payload.get("items", [])
    if not isinstance(items, list) or len(items) > 100:
        raise ValidationError("Ожидается список до 100 подзадач.")
    board = WorkBoard.objects.select_for_update().get(pk=board.pk)
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    previous = WorkBoardCard.objects.filter(board=board, import_key=key).first()
    if previous:
        return previous
    card = create_task_card(board, name=name, description=payload.get("description", ""))
    card.import_key = key
    card.save(update_fields=["import_key", "updated_at"])
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Некорректная подзадача.")
        edit_task_work(card, actor, "add_item", item)
        position = card.task.commercial_items.latest("pk")
        steps = item.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 100:
            raise ValidationError("Ожидается список до 100 этапов.")
        for step in steps:
            if not isinstance(step, dict):
                raise ValidationError("Некорректный этап.")
            checks = step.get("checks", [])
            if not isinstance(checks, list) or not checks or len(checks) > 100 or not all(isinstance(c, str) for c in checks):
                raise ValidationError("У этапа должен быть список обязательных критериев.")
            edit_task_work(card, actor, "add_step", {"item_id": position.pk, "name": step.get("name"), "criterion": checks[0]})
            route_step = position.route_plans.get().steps.latest("position")
            for criterion in checks[1:]:
                edit_task_work(card, actor, "add_check", {"step_id": route_step.pk, "name": criterion, "required": True})
    return card


@transaction.atomic
def delete_card(card, actor):
    """Explicit deletion of a whole Task; removing Tender only detaches it."""
    from django.core.exceptions import PermissionDenied
    from django.db.models.deletion import ProtectedError

    locked = WorkBoardCard.objects.select_for_update().get(pk=card.pk)
    if not actor.is_superuser and locked.board.created_by_id != actor.pk:
        raise PermissionDenied
    try:
        if locked.task_id:
            # PROTECT is intentional for ordinary route edits. Only explicit
            # whole-task deletion removes its own checkpoints and their events.
            checkpoints = WorkCheckpoint.objects.filter(
                step__route_plan__commercial_item__task_id=locked.task_id,
                step__route_plan__commercial_item__tender__isnull=True,
                step__route_plan__commercial_item__order__isnull=True,
            )
            WorkCheckpointEvent.objects.filter(checkpoint__in=checkpoints).delete()
            checkpoints.delete()
            locked.task.delete()
        else:
            locked.delete()
    except ProtectedError as error:
        raise ValidationError("Карточка связана с другими данными и не может быть удалена.") from error
