from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException
from pypdf.errors import PyPdfError
from xlrd.biffh import XLRDError

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from .assistant_agent import reply
from .models import ConversationAttachment, OwnerFeedbackEvent, OwnerInteraction
from .services import TenderAIError, extract_tender_source


CONVERSATION_KIND = "assistant_conversation"
ASSISTANT_DEBUG_SCOPE = "assistant_debug"
DEBUG_RETENTION = timedelta(days=1)
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 12_000
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_IMAGE_ATTACHMENTS_PER_MESSAGE = 3
_TEXT_SUFFIXES = {".txt", ".csv"}
_DOCUMENT_SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".xls", ".doc", ".docx", ".docm"}
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _attachment_kind(name):
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_TYPES:
        return "image", _IMAGE_TYPES[suffix]
    if suffix in _TEXT_SUFFIXES:
        return "document", "text/plain" if suffix == ".txt" else "text/csv"
    if suffix in _DOCUMENT_SUFFIXES:
        return "document", "application/octet-stream"
    raise ValueError("Поддерживаются PDF, Excel, Word, CSV, TXT и изображения PNG/JPEG/WebP.")


def _attachment_text(name, content):
    suffix = Path(name).suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return content.decode("utf-8-sig", errors="replace").strip()[:MAX_ATTACHMENT_TEXT_CHARS]
    if suffix not in _DOCUMENT_SUFFIXES:
        return ""
    try:
        text, _truncated = extract_tender_source(SimpleUploadedFile(name, content))
    except (
        TenderAIError, OSError, UnicodeError, ValueError, BadZipFile, ParseError,
        XMLSyntaxError, InvalidFileException, PyPdfError, XLRDError, KeyError,
    ) as exc:
        raise ValueError("Не удалось прочитать документ. Проверьте, что файл исправен, не защищён паролем и соответствует своему формату.") from exc
    return text[:MAX_ATTACHMENT_TEXT_CHARS]


def upload_attachment(conversation, user, uploaded):
    if uploaded is None:
        raise ValueError("Выберите файл.")
    name = Path(str(getattr(uploaded, "name", ""))).name
    if not name:
        raise ValueError("У файла нет имени.")
    kind, content_type = _attachment_kind(name)
    content = uploaded.read(MAX_ATTACHMENT_BYTES + 1)
    if not content:
        raise ValueError("Нельзя прикрепить пустой файл.")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Размер вложения не должен превышать 10 МБ.")
    return ConversationAttachment.objects.create(
        conversation=conversation,
        created_by=user,
        name=name,
        content_type=content_type,
        kind=kind,
        content=content,
        extracted_text=_attachment_text(name, content),
    )


def serialize_attachment(attachment, *, include_text=False):
    result = {"id": attachment.pk, "name": attachment.name, "kind": attachment.kind}
    if attachment.kind == "document":
        if not attachment.extracted_text:
            result["warning"] = "Текст из файла не извлечён. Для скана приложите страницы как изображения или файл с текстовым слоем."
        elif len(attachment.extracted_text) >= MAX_ATTACHMENT_TEXT_CHARS:
            result["warning"] = f"Достигнут лимит {MAX_ATTACHMENT_TEXT_CHARS} символов; содержимое может быть неполным."
        if include_text:
            result["text"] = attachment.extracted_text
    return result


def message_attachments(conversation, user, attachment_ids):
    if attachment_ids in (None, []):
        return []
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError("Можно передать не более пяти вложений.")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in attachment_ids):
        raise ValueError("Некорректный идентификатор вложения.")
    attachments = list(ConversationAttachment.objects.filter(
        conversation=conversation, created_by=user, pk__in=attachment_ids,
    ).order_by("pk"))
    if len(attachments) != len(set(attachment_ids)):
        raise ValueError("Вложение не принадлежит этой беседе.")
    if sum(item.kind == "image" for item in attachments) > MAX_IMAGE_ATTACHMENTS_PER_MESSAGE:
        raise ValueError("Можно передать не более трёх изображений за раз.")
    return attachments


def active_attachments(conversation, user):
    identifiers = conversation.context.get("attachment_ids", [])
    if not isinstance(identifiers, list):
        return []
    return list(ConversationAttachment.objects.filter(
        conversation=conversation, created_by=user, pk__in=identifiers[-MAX_ATTACHMENTS_PER_MESSAGE:],
    ).order_by("pk"))


def attachment_image_urls(attachments):
    return [
        f"data:{attachment.content_type};base64,{base64.b64encode(bytes(attachment.content)).decode('ascii')}"
        for attachment in attachments if attachment.kind == "image"
    ]


def _conversations(user):
    return OwnerInteraction.objects.filter(context__kind=CONVERSATION_KIND, context__owner_id=user.pk, status="open").order_by("-created_at")


def clear_conversations(user):
    conversations = OwnerInteraction.objects.filter(
        context__kind=CONVERSATION_KIND, context__owner_id=user.pk,
    )
    # QuerySet.delete is atomic and cascades only to messages and attachments.
    _total, deleted = conversations.delete()
    return deleted.get(OwnerInteraction._meta.label, 0)


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
    for event in conversation.feedback_events.filter(scope="assistant_conversation").order_by("created_at", "pk"):
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


def serialize_debug_journal(conversation):
    return [
        {"stage": (event.payload or {}).get("stage", ""), "input": (event.payload or {}).get("input"),
         "output": (event.payload or {}).get("output"), "error": (event.payload or {}).get("error", ""),
         "created_at": event.created_at.isoformat()}
        for event in conversation.feedback_events.filter(scope=ASSISTANT_DEBUG_SCOPE).order_by("created_at", "pk")
    ]


def add_debug_event(conversation, stage, *, event_input=None, event_output=None, error=""):
    OwnerFeedbackEvent.objects.filter(
        scope=ASSISTANT_DEBUG_SCOPE, created_at__lt=timezone.now() - DEBUG_RETENTION,
    ).delete()
    payload = {"stage": stage}
    if event_input is not None:
        payload["input"] = event_input
    if event_output is not None:
        payload["output"] = event_output
    if error:
        payload["error"] = error
    return OwnerFeedbackEvent.objects.create(
        interaction=conversation, raw_text="", payload=payload, scope=ASSISTANT_DEBUG_SCOPE,
    )


def _add_message(conversation, user, role, text, kind="text", data=None, context=None):
    data = json.loads(json.dumps(data or {}, ensure_ascii=False, default=str))
    return OwnerFeedbackEvent.objects.create(
        interaction=conversation,
        actor=user if role == "user" else None,
        raw_text=text,
        payload={"role": role, "kind": kind, "data": data, "context": context or {}},
        scope="assistant_conversation",
    )


def add_assistant_message(conversation, text, kind="text", data=None):
    return _add_message(conversation, None, "assistant", text, kind, data)


def respond(conversation, user, text, context=None, attachments=None):
    text = text.strip()
    context = {key: str(value)[:240] for key, value in (context or {}).items() if key in {"page", "label", "tender_id", "line_id"} and value not in (None, "")}
    attachments = attachments or active_attachments(conversation, user)
    if attachments:
        conversation.context["attachment_ids"] = [item.pk for item in attachments][-MAX_ATTACHMENTS_PER_MESSAGE:]
        conversation.save(update_fields=["context"])
    if not text and attachments:
        text = "Прикреплён файл: " + ", ".join(item.name for item in attachments)
    _add_message(conversation, user, "user", text, data={"attachments": [serialize_attachment(item) for item in attachments]}, context=context)
    response = reply(
        conversation, user, text, context,
        attachments=[serialize_attachment(item, include_text=True) for item in attachments],
        image_data_urls=attachment_image_urls(attachments),
    )
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
