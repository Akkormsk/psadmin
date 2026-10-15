import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect

from .assistant_tools import AssistantToolError, execute_tool
from .assistant_conversations import add_assistant_message, archive_conversations, conversation_for_user, create_conversation, message_attachments, respond, serialize_conversation, serialize_messages, upload_attachment
from .services import TenderAIError


@login_required
def console(request):
    return redirect("tender_home")


@login_required
def conversations(request):
    from .models import OwnerInteraction
    items = OwnerInteraction.objects.filter(context__kind="assistant_conversation", context__owner_id=request.user.pk, status="open").order_by("-created_at")[:30]
    return JsonResponse({"conversations": [serialize_conversation(item) for item in items]})


@login_required
def conversations_clear(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    return JsonResponse({"archived": archive_conversations(request.user)})


@login_required
def conversation_new(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    context = {}
    for field in ("page", "label", "tender_id", "line_id"):
        if request.POST.get(field):
            context[field] = request.POST[field]
    conversation = create_conversation(request.user, request.POST.get("title", ""), context)
    return JsonResponse({"conversation": serialize_conversation(conversation)}, status=201)


@login_required
def conversation_detail(request, conversation_id):
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    return JsonResponse({"conversation": serialize_conversation(conversation), "messages": serialize_messages(conversation)})


@login_required
def conversation_message(request, conversation_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    try:
        payload = json.loads(request.body)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Некорректное сообщение")
    message = payload.get("message", "") if isinstance(payload, dict) else ""
    attachment_ids = payload.get("attachment_ids", []) if isinstance(payload, dict) else []
    try:
        attachments = message_attachments(conversation, request.user, attachment_ids)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if not isinstance(message, str) or (not message.strip() and not attachments):
        return HttpResponseBadRequest("Введите сообщение")
    context = payload.get("context", {}) if isinstance(payload, dict) else {}
    try:
        messages = respond(conversation, request.user, message, context, attachments)
    except TenderAIError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse({"messages": messages})


@login_required
def conversation_attachment(request, conversation_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    try:
        attachment = upload_attachment(conversation, request.user, request.FILES.get("file"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return JsonResponse({"attachment": {"id": attachment.pk, "name": attachment.name, "kind": attachment.kind}}, status=201)


@login_required
def conversation_action_confirm(request, conversation_id, event_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    with transaction.atomic():
        conversation = conversation_for_user(request.user, conversation_id)
        if not conversation:
            return JsonResponse({"detail": "Беседа не найдена"}, status=404)
        conversation = type(conversation).objects.select_for_update().get(pk=conversation.pk)
        event = conversation.feedback_events.filter(pk=event_id).first()
        payload = event.payload if event else {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        used = list(conversation.context.get("confirmed_action_events", []))
        if (not event or payload.get("kind") != "confirmation_required" or event.pk in used
                or conversation.feedback_events.filter(pk__gt=event.pk).exists()):
            return JsonResponse({"detail": "Подтверждение недействительно, устарело или уже использовано"}, status=409)
        used.append(event.pk)
        conversation.context["confirmed_action_events"] = used
        # Consume before execution under the same transaction: retries cannot
        # issue a second write even when the client repeats a click.
        conversation.save(update_fields=["context"])
        if request.POST.get("accept") != "yes":
            add_assistant_message(conversation, "Действие отменено.")
            return JsonResponse({"messages": serialize_messages(conversation)})
        try:
            result = execute_tool(data["tool_id"], request.user, data.get("arguments", {}), confirmed=True)
        except (KeyError, AssistantToolError) as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        add_assistant_message(conversation, "Действие выполнено.", "tool_result", {"tool_id": data["tool_id"], "result": result})
    return JsonResponse({"messages": serialize_messages(conversation)})
