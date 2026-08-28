from django.core.management.base import BaseCommand
from django.utils import timezone

from tenders.models import ProductionTrainingSession, ProductionTrainingTurn
from tenders.services import TenderAIError, build_training_hypothesis


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("session_id", type=int)

    def handle(self, *args, **options):
        session = ProductionTrainingSession.objects.get(pk=options["session_id"])
        line = (session.requirements or {}).get("line", {})
        try:
            def set_progress(stage):
                session.current_hypothesis = {"stage": stage, "started_at": session.current_hypothesis.get("started_at", timezone.now().isoformat())}
                session.save(update_fields=["current_hypothesis", "updated_at"])

            set_progress("ai")
            hypothesis = build_training_hypothesis(line, progress_callback=set_progress)
            hypothesis["session_id"] = session.pk
            session.current_hypothesis = hypothesis
            session.requirements = line.get("requirements") if isinstance(line.get("requirements"), dict) else {}
            session.save(update_fields=["current_hypothesis", "requirements", "updated_at"])
            ProductionTrainingTurn.objects.create(session=session, hypothesis=hypothesis)
        except TenderAIError as exc:
            session.current_hypothesis = {"stage": "error", "error": str(exc)}
            session.save(update_fields=["current_hypothesis", "updated_at"])
        except Exception:
            session.current_hypothesis = {"stage": "error", "error": "Не удалось построить расчёт. Попробуйте ещё раз."}
            session.save(update_fields=["current_hypothesis", "updated_at"])
            raise
