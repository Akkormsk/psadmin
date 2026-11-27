import base64
import json
import os

from django.db import transaction

from tenders.models import WorkBoard, WorkBoardCard
from tenders.workboards import create_task_from_plan, edit_task_work


action = os.environ.get("WORKBOARD_DATA_ACTION", "").strip()
if action not in {"inspect", "import"}:
    raise RuntimeError("Unsupported workboard data action")


def card_summary(card):
    summary = {
        "id": card.pk,
        "type": "task" if card.task_id else "tender",
        "name": card.task.name if card.task_id else card.tender.purchase_number,
        "column": card.column.name,
    }
    if card.task_id:
        summary["description"] = card.task.description
        summary["items"] = []
        for item in card.task.commercial_items.filter(status="active").order_by("position", "pk"):
            plans = list(item.route_plans.all())
            summary["items"].append({
                "name": item.display_name,
                "description": item.description,
                "steps": [
                    {
                        "name": step.display_name,
                        "checks": [
                            {
                                "name": check.name,
                                "required": check.required,
                                "completed": bool(check.completed_at),
                            }
                            for check in step.checkpoints.all()
                        ],
                    }
                    for step in (plans[0].steps.all() if len(plans) == 1 else [])
                ],
            })
    return summary


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
    extended = []
    with transaction.atomic():
        for project in projects:
            key = project["name"].strip().casefold()
            if key in existing:
                skipped.append({"name": project["name"], "card_id": existing[key]})
                continue
            card = create_task_from_plan(board, board.created_by, project)
            created.append({"name": project["name"], "card_id": card.pk})
        for update in payload.get("updates", []):
            card = WorkBoardCard.objects.select_related("task").get(
                board=board,
                task__name=update["name"],
                task__isnull=False,
            )
            existing_items = {
                value.casefold()
                for value in card.task.commercial_items.filter(status="active")
                .values_list("display_name", flat=True)
            }
            for item in update.get("items", []):
                if item["name"].strip().casefold() in existing_items:
                    continue
                edit_task_work(card, board.created_by, "add_item", item)
                position = card.task.commercial_items.latest("pk")
                for step in item.get("steps", []):
                    checks = step.get("checks", [])
                    if not checks:
                        raise RuntimeError("Every appended step requires checks")
                    edit_task_work(card, board.created_by, "add_step", {
                        "item_id": position.pk,
                        "name": step["name"],
                        "criterion": checks[0],
                    })
                    route_step = position.route_plans.get().steps.latest("position")
                    for criterion in checks[1:]:
                        edit_task_work(card, board.created_by, "add_check", {
                            "step_id": route_step.pk,
                            "name": criterion,
                            "required": True,
                        })
                extended.append({"card_id": card.pk, "item": item["name"]})
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
        "extended": extended,
        "verification": verification,
    }, ensure_ascii=False))
