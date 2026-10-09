from __future__ import annotations

from django.utils import timezone

from .assistant_tools import available_tools, execute_tool
from .models import OwnerFeedbackEvent, OwnerInteraction


CONVERSATION_KIND = "assistant_conversation"


def _conversations(user):
    return OwnerInteraction.objects.filter(context__kind=CONVERSATION_KIND, context__owner_id=user.pk).order_by("-created_at")


def create_conversation(user, title=""):
    title = title.strip() or "Новая рабочая беседа"
    return OwnerInteraction.objects.create(
        status="open",
        question=title,
        context={"kind": CONVERSATION_KIND, "title": title, "owner_id": user.pk},
    )


def conversation_for_user(user, conversation_id):
    return _conversations(user).filter(pk=conversation_id).first()


def serialize_conversation(conversation):
    return {
        "id": conversation.pk,
        "title": conversation.context.get("title") or conversation.question,
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
            "created_at": event.created_at.isoformat(),
        })
    return messages


def _add_message(conversation, user, role, text, kind="text", data=None):
    return OwnerFeedbackEvent.objects.create(
        interaction=conversation,
        actor=user if role == "user" else None,
        raw_text=text,
        payload={"role": role, "kind": kind, "data": data or {}},
        scope="assistant_conversation",
    )


def respond(conversation, user, text):
    text = text.strip()
    _add_message(conversation, user, "user", text)
    normalized = text.casefold()
    if any(phrase in normalized for phrase in ("что ты умеешь", "какие функции", "какие возможности", "можешь создать", "можешь рассчитать")):
        tools = execute_tool("assistant.capabilities", user, {})["tools"]
        _add_message(
            conversation,
            user,
            "assistant",
            "Вот функции, которые доступны вам сейчас.",
            "capabilities",
            {"tools": tools},
        )
    elif "email" in normalized or "письм" in normalized:
        _add_message(conversation, user, "assistant", "Отправка email подрядчику пока не подключена. Я не буду обещать действие, которого нет в реестре.", "unsupported")
    elif "контрагент" in normalized or "пошив" in normalized:
        _add_message(
            conversation,
            user,
            "assistant",
            "Готов создать черновик контрагента по пошиву. Укажите название и прикрепите XLS-прайс; затем я покажу распознанные условия для подтверждения.",
            "provider_upload",
            {"stages_tool": "process.list"},
        )
    else:
        _add_message(conversation, user, "assistant", "Я могу показать доступные функции, создать черновик контрагента по пошиву или рассчитать подтверждённый прайс. Спросите «Что ты умеешь?».", "help")
    return serialize_messages(conversation)


def answer_interaction(interaction, user, text):
    if interaction.status != "open":
        return False
    OwnerFeedbackEvent.objects.create(interaction=interaction, actor=user, raw_text=text, payload={"role": "user", "kind": "answer"}, scope="current_tender")
    interaction.status = "answered"
    interaction.answered_at = timezone.now()
    interaction.save(update_fields=["status", "answered_at"])
    return True
