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
            journal = serialize_debug_journal(conversation)
            calls = [
                item["output"].get("message", {}).get("tool_calls", [])
                for item in journal if item["stage"] == "function_call.response" and isinstance(item.get("output"), dict)
            ]
            usage = [
                item["output"].get("usage")
                for item in journal if item["stage"] == "function_call.response" and isinstance(item.get("output"), dict)
            ]
            tool_events = [
                item["output"] for item in journal
                if item["stage"] == "function_call.tool" and isinstance(item.get("output"), dict)
            ]
            self.stdout.write(json.dumps({
                "tool_ids": [step.get("tool_id") for step in steps],
                "tool_call_names": [
                    call.get("function", {}).get("name") for item in calls for call in item
                ],
                "tool_call_count": sum(len(item) for item in calls),
                "tool_events": tool_events,
                "usage": usage,
            }, ensure_ascii=False, default=str))
            if not any(step.get("tool_id") == "read" for step in steps):
                raise CommandError("The model did not complete a READ call.")
        finally:
            conversation.status = "archived"
            conversation.save(update_fields=["status"])
