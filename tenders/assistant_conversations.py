from __future__ import annotations

from django.utils import timezone

from .assistant_agent import reply
from .models import OwnerFeedbackEvent, OwnerInteraction


CONVERSATION_KIND = "assistant_conversation"


def _conversations(user):
    return OwnerInteraction.objects.filter(context__kind=CONVERSATION_KIND, context__owner_id=user.pk).order_by("-created_at")


def create_conversation(user, title="", context=None):
    title = title.strip() or "Новая рабочая беседа"
    context = context if isinstance(context, dict) else {}
    context = {key: str(value)[:240] for key, value in context.items() if key in {"page", "label", "tender_id", "line_id"} and value not in (None, "")}
    return OwnerInteraction.objects.create(
        status="open",
        question=title,
        context={"kind": CONVERSATION_KIND, "title": title, "owner_id": user.pk, "business_context": context},
    )


def conversation_for_user(user, conversation_id):
    return _conversations(user).filter(pk=conversation_id).first()


def serialize_conversation(conversation):
    return {
        "id": conversation.pk,
        "title": conversation.context.get("title") or conversation.question,
        "business_context": conversation.context.get("business_context", {}),
        "created_at": conversation.created_at.isoformat(),
    }


def serialize_messages(conversation):
    messages = []
    for event in conversation.feedback_events.order_by("created_at", "pk"):
        payload = event.payload or {}
        messages.append({
            "id": event.pk,
            "role": payload.get("role", "user"),
            "kind": payload.get("kind", "text"),
            "text": event.raw_text,
            "data": payload.get("data", {}),
            "context": payload.get("context", {}),
            "created_at": event.created_at.isoformat(),
        })
    return messages


def _add_message(conversation, user, role, text, kind="text", data=None, context=None):
    return OwnerFeedbackEvent.objects.create(
        interaction=conversation,
        actor=user if role == "user" else None,
        raw_text=text,
        payload={"role": role, "kind": kind, "data": data or {}, "context": context or {}},
        scope="assistant_conversation",
    )


def add_assistant_message(conversation, text, kind="text", data=None):
    return _add_message(conversation, None, "assistant", text, kind, data)


def respond(conversation, user, text, context=None):
    text = text.strip()
    context = {key: str(value)[:240] for key, value in (context or {}).items() if key in {"page", "label", "tender_id", "line_id"} and value not in (None, "")}
    _add_message(conversation, user, "user", text, context=context)
    response = reply(conversation, user, text, context)
    _add_message(conversation, user, "assistant", response["text"], response["kind"], response["data"])
    return serialize_messages(conversation)


def answer_interaction(interaction, user, text):
    if interaction.status != "open":
        return False
    OwnerFeedbackEvent.objects.create(interaction=interaction, actor=user, raw_text=text, payload={"role": "user", "kind": "answer"}, scope="current_tender")
    interaction.status = "answered"
    interaction.answered_at = timezone.now()
    interaction.save(update_fields=["status", "answered_at"])
    return True
