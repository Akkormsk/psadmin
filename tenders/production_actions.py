"""Backend production-knowledge actions shared by UI and assistant."""
from .models import Lesson


def deactivate_lesson(lesson_id):
    """Deactivate the learned instruction without deleting historical evidence."""
    return bool(Lesson.objects.filter(pk=lesson_id, is_active=True).update(is_active=False))
