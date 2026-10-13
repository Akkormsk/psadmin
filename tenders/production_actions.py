"""Backend production-knowledge actions shared by UI and assistant."""
from django.db import transaction

from .models import Lesson, ProductionTrainingExample


def deactivate_lesson(lesson_id):
    """Deactivate the learned instruction without deleting historical evidence."""
    return bool(Lesson.objects.filter(pk=lesson_id, is_active=True).update(is_active=False))


@transaction.atomic
def deactivate_route_knowledge(payload):
    """Disable both learned route representations, mirroring the UI."""
    if not isinstance(payload, dict):
        raise ValueError("Не удалось определить сохранённый опыт")
    if payload.get("example_id") not in (None, ""):
        try:
            example_id = int(payload["example_id"])
        except (ValueError, TypeError):
            raise ValueError("Неверный идентификатор маршрута")
        changed = ProductionTrainingExample.objects.filter(pk=example_id).update(is_active=False)
        Lesson.objects.filter(
            session__confirmed_example_id=example_id,
            scope__in=["route", "production_step"],
        ).update(is_active=False)
        return bool(changed)
    try:
        lesson_id = int(payload["lesson_id"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("Не указан идентификатор знания")
    lesson = Lesson.objects.filter(
        pk=lesson_id, scope__in=["route", "production_step"],
    ).first()
    if lesson is None:
        raise ValueError("Сохранённый опыт не найден")
    lesson.is_active = False
    lesson.save(update_fields=["is_active"])
    if lesson.session_id and lesson.session.confirmed_example_id:
        ProductionTrainingExample.objects.filter(
            pk=lesson.session.confirmed_example_id,
        ).update(is_active=False)
    return True
