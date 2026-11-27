import base64
import json
import os

from django.db import transaction

from tenders.models import WorkBoard, WorkBoardCard
from tenders.workboards import create_task_from_plan


action = os.environ.get("WORKBOARD_DATA_ACTION", "").strip()
if action not in {"inspect", "import"}:
    raise RuntimeError("Unsupported workboard data action")


def card_summary(card):
    items = card.task.commercial_items.filter(status="active") if card.task_id else []
    return {
        "id": card.pk,
        "type": "task" if card.task_id else "tender",
        "name": card.task.name if card.task_id else card.tender.purchase_number,
        "column": card.column.name,
        "items": items.count() if card.task_id else None,
    }


boards = list(WorkBoard.objects.select_related("created_by").order_by("pk"))
print(json.dumps({
    "action": action,
    "boards": [
        {
            "id": board.pk,
            "name": board.name,
            "owner": board.created_by.get_username(),
            "cards": [
                card_summary(card)
                for card in WorkBoardCard.objects.filter(board=board)
                .select_related("task", "tender", "column").order_by("column__position", "position", "pk")
            ],
        }
        for board in boards
    ],
}, ensure_ascii=False))

if action == "import":
    encoded = os.environ.get("WORKBOARD_DATA_PAYLOAD_B64", "")
    payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    board = WorkBoard.objects.select_related("created_by").get(pk=payload["board_id"])
    projects = payload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise RuntimeError("Projects are required")
    names = [str(project.get("name", "")).strip() for project in projects]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise RuntimeError("Project names must be present and unique")
    existing = {
        name.casefold(): card_id
        for name, card_id in WorkBoardCard.objects.filter(board=board, task__isnull=False)
        .values_list("task__name", "pk")
    }
    created = []
    skipped = []
    with transaction.atomic():
        for project in projects:
            key = project["name"].strip().casefold()
            if key in existing:
                skipped.append({"name": project["name"], "card_id": existing[key]})
                continue
            card = create_task_from_plan(board, board.created_by, project)
            created.append({"name": project["name"], "card_id": card.pk})
        actual = WorkBoardCard.objects.filter(
            board=board,
            task__name__in=names,
            task__isnull=False,
        ).distinct()
        if actual.count() != len(names):
            raise RuntimeError("Not all requested project cards are present")
        verification = []
        for card in actual.select_related("task").order_by("position", "pk"):
            items = card.task.commercial_items.filter(status="active")
            verification.append({
                "card_id": card.pk,
                "name": card.task.name,
                "items": items.count(),
                "steps": sum(item.route_plans.get().steps.count() for item in items),
                "checks": sum(
                    step.checkpoints.count()
                    for item in items
                    for step in item.route_plans.get().steps.all()
                ),
            })
    print(json.dumps({
        "board_id": board.pk,
        "created": created,
        "skipped": skipped,
        "verification": verification,
    }, ensure_ascii=False))
