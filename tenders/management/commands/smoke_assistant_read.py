import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.assistant_conversations import create_conversation, respond, serialize_debug_journal


class Command(BaseCommand):
    help = "Run one live Function Calling READ smoke test without changing business data."

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
        if user is None:
            raise CommandError("A superuser is required for the assistant READ smoke test.")
        conversation = create_conversation(user, "Function Calling smoke test")
        try:
            messages = respond(conversation, user, "Покажи три последние операции по кассе")
            final = messages[-1] if messages else {}
            steps = (final.get("data") or {}).get("steps") or []
            if not any(step.get("tool_id") == "read" for step in steps):
                raise CommandError("The model did not complete a READ call.")
            journal = serialize_debug_journal(conversation)
            calls = [
                item["output"].get("message", {}).get("tool_calls", [])
                for item in journal if item["stage"] == "function_call.response" and isinstance(item.get("output"), dict)
            ]
            usage = [
                item["output"].get("usage")
                for item in journal if item["stage"] == "function_call.response" and isinstance(item.get("output"), dict)
            ]
            self.stdout.write(json.dumps({
                "answer": str(final.get("text", ""))[:500],
                "tool_ids": [step.get("tool_id") for step in steps],
                "tool_call_count": sum(len(item) for item in calls),
                "usage": usage,
            }, ensure_ascii=False, default=str))
        finally:
            conversation.status = "archived"
            conversation.save(update_fields=["status"])
