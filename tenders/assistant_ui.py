import json
from urllib.parse import urlencode

from django.core import signing

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .assistant_agent import continue_after_cancellation, continue_after_confirmation
from .assistant_tools import AssistantToolError, execute_tool
from .assistant_conversations import add_assistant_message, clear_conversations, conversation_for_user, create_conversation, message_attachments, respond, serialize_conversation, serialize_debug_journal, serialize_messages, upload_attachment
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
    return JsonResponse({"deleted": clear_conversations(request.user)})


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
def conversation_debug(request, conversation_id):
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    return JsonResponse({"debug_journal": serialize_debug_journal(conversation)})


_EXPORT_SALT = "assistant-conversation-export"


@login_required
@require_POST
def conversation_link(request, conversation_id):
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    token = signing.dumps({"conversation_id": conversation.pk, "owner_id": request.user.pk}, salt=_EXPORT_SALT)
    path = reverse("assistant_conversation_export", args=[conversation.pk])
    response = JsonResponse({"url": request.build_absolute_uri(path) + "?" + urlencode({"token": token})})
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def conversation_export(request, conversation_id):
    from .models import OwnerInteraction
    try:
        data = signing.loads(request.GET.get("token", ""), salt=_EXPORT_SALT)
    except signing.BadSignature:
        data = None
    conversation = None
    if isinstance(data, dict) and data.get("conversation_id") == conversation_id:
        conversation = OwnerInteraction.objects.filter(
            pk=conversation_id, status="open", context__kind="assistant_conversation",
            context__owner_id=data.get("owner_id"),
        ).first()
    if conversation is None:
        response = JsonResponse({"detail": "Беседа не найдена"}, status=404)
    else:
        response = JsonResponse({
            "exported_at": timezone.now().isoformat(), "conversation": serialize_conversation(conversation),
            "messages": serialize_messages(conversation), "debug_journal": serialize_debug_journal(conversation),
        }, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


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
        add_assistant_message(conversation, str(exc))
        return JsonResponse({"messages": serialize_messages(conversation)})
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
        run = conversation.context.get("agent_run", {})
        waiting = run.get("waiting_confirmation", {})
        if (not event or payload.get("kind") != "confirmation_required" or event.pk in used
                or conversation.feedback_events.filter(pk__gt=event.pk).exists()
                or waiting.get("tool_id") != data.get("tool_id")
                or waiting.get("arguments") != data.get("arguments")
                or waiting.get("plan_version") != (run.get("plan") or {}).get("version")):
            return JsonResponse({"detail": "Подтверждение недействительно, устарело или уже использовано"}, status=409)
        used.append(event.pk)
        conversation.context["confirmed_action_events"] = used
        # Consume before execution under the same transaction: retries cannot
        # issue a second write even when the client repeats a click.
        conversation.save(update_fields=["context"])
        cancelled = request.POST.get("accept") != "yes"
        if cancelled:
            result = None
        else:
            try:
                result = execute_tool(data["tool_id"], request.user, data.get("arguments", {}), confirmed=True)
            except (KeyError, AssistantToolError) as exc:
                return JsonResponse({"detail": str(exc)}, status=400)
    try:
        continuation = (
            continue_after_cancellation(conversation, request.user, data["tool_id"])
            if cancelled else continue_after_confirmation(conversation, request.user, data["tool_id"], result)
        )
    except TenderAIError as exc:
        status = "Действие отменено" if cancelled else "Действие выполнено"
        add_assistant_message(conversation, f"{status}, но продолжение задачи остановлено: {exc}")
        continuation = None
    if continuation:
        add_assistant_message(conversation, continuation["text"], continuation["kind"], continuation["data"])
    return JsonResponse({"messages": serialize_messages(conversation)})
