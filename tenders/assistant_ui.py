import copy
import json

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .assistant_tools import available_tools
from .assistant_conversations import conversation_for_user, create_conversation, respond, serialize_conversation, serialize_messages
from .models import CounterpartyKnowledgeVersion, ProcessDefinition
from .provider_knowledge import create_knowledge_draft, create_sewing_provider_draft, confirm_knowledge, initialize_structured_rules_binding


@login_required
def console(request):
    stages = ProcessDefinition.objects.filter(is_active=True).order_by("name")
    version = get_object_or_404(CounterpartyKnowledgeVersion, pk=request.GET["version"]) if request.GET.get("version") else None
    context = {"tools": available_tools(request.user), "stages": stages, "version": version}
    if request.method != "POST":
        return render(request, "tenders/assistant_console.html", context)
    if not request.user.is_superuser:
        messages.error(request, "Создавать и подтверждать знания может только администратор.")
        return redirect("assistant_console")
    action = request.POST.get("action")
    if action == "upload_sewing":
        upload = request.FILES.get("source")
        stage = get_object_or_404(stages, pk=request.POST.get("stage_id"))
        name = request.POST.get("name", "").strip()
        if not upload or upload.name.lower().rsplit(".", 1)[-1] != "xls" or not name:
            messages.error(request, "Нужны название, этап и исходный файл XLS.")
            return redirect("assistant_console")
        provider, version = create_sewing_provider_draft(request.user, name, stage, upload.read(), upload.name)
        return redirect("assistant_console", version=version.pk)
    version = get_object_or_404(CounterpartyKnowledgeVersion, pk=request.POST.get("version_id"))
    if action == "confirm_sewing":
        currency = request.POST.get("currency", "").strip().upper()
        if currency != "RUB" or request.POST.get("formula_confirmed") != "yes":
            messages.error(request, "Подтвердите валюту и то, что кэшированные значения XLS — цены за единицу.")
            return redirect("assistant_console", version=version.pk)
        data = copy.deepcopy(version.canonical_data)
        data["requires_confirmation"] = False
        data["formula_status"] = "confirmed"
        data["pricing"]["currency"] = currency
        confirmed = confirm_knowledge(create_knowledge_draft(version.counterparty, request.user, data, stage=version.stage, metadata=version.source_metadata), request.user, "Тестовое допущение: валюта RUB; кэшированные значения XLS подтверждены как цены за единицу.")
        binding = initialize_structured_rules_binding(confirmed)
        version.status = CounterpartyKnowledgeVersion.STATUS_INACTIVE
        version.save(update_fields=["status"])
        return redirect("provider_calculator", binding_id=binding.pk)
    return redirect("assistant_console")


@login_required
def conversations(request):
    from .models import OwnerInteraction
    items = OwnerInteraction.objects.filter(context__kind="assistant_conversation", context__owner_id=request.user.pk).order_by("-created_at")[:30]
    return JsonResponse({"conversations": [serialize_conversation(item) for item in items]})


@login_required
def conversation_new(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    conversation = create_conversation(request.user, request.POST.get("title", ""))
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
    if not isinstance(message, str) or not message.strip():
        return HttpResponseBadRequest("Введите сообщение")
    return JsonResponse({"messages": respond(conversation, request.user, message)})
