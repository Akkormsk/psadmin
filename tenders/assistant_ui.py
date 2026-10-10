import json

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .assistant_tools import available_tools
from .assistant_conversations import add_assistant_message, archive_conversations, conversation_for_user, create_conversation, respond, serialize_conversation, serialize_messages
from .models import CounterpartyKnowledgeVersion, ProcessDefinition
from .provider_knowledge import confirm_sewing_price_list, create_sewing_provider_draft


@login_required
def console(request):
    return redirect("tender_home")


@login_required
def legacy_console(request):
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
        try:
            _confirmed, binding = confirm_sewing_price_list(version, request.user, request.POST.get("currency", ""), request.POST.get("formula_confirmed") == "yes")
        except ValueError as error:
            messages.error(request, str(error))
            return redirect("assistant_console", version=version.pk)
        return redirect("provider_calculator", binding_id=binding.pk)
    return redirect("assistant_console")


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
    if not isinstance(message, str) or not message.strip():
        return HttpResponseBadRequest("Введите сообщение")
    context = payload.get("context", {}) if isinstance(payload, dict) else {}
    return JsonResponse({"messages": respond(conversation, request.user, message, context)})


@login_required
def conversation_sewing_upload(request, conversation_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    if not request.user.is_superuser:
        return JsonResponse({"detail": "Создавать контрагента может только администратор"}, status=403)
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    upload = request.FILES.get("source")
    name = request.POST.get("name", "").strip()
    stage = ProcessDefinition.objects.filter(pk=request.POST.get("stage_id"), is_active=True).first()
    extension = upload.name.lower().rsplit(".", 1)[-1] if upload and "." in upload.name else ""
    if not upload or extension not in {"xls", "xlsx"} or not name or not stage:
        return HttpResponseBadRequest("Нужны название, этап и прайс XLS/XLSX")
    try:
        provider, version = create_sewing_provider_draft(request.user, name, stage, upload.read(), upload.name)
    except ValueError as error:
        return JsonResponse({"detail": str(error)}, status=400)
    variants = version.canonical_data.get("pricing", {}).get("variants", {})
    add_assistant_message(
        conversation,
        f"Черновик «{provider.name}» создан. Распознано вариантов: {len(variants)}. В XLS валюта и смысл кэшированных формул не подтверждены.",
        "sewing_review",
        {"version_id": version.pk, "provider_id": provider.pk, "variant_count": len(variants), "formula_note": version.canonical_data.get("formula_note", "")},
    )
    return JsonResponse({"messages": serialize_messages(conversation)})


@login_required
def conversation_sewing_confirm(request, conversation_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    if not request.user.is_superuser:
        return JsonResponse({"detail": "Подтверждать прайс может только администратор"}, status=403)
    conversation = conversation_for_user(request.user, conversation_id)
    if not conversation:
        return JsonResponse({"detail": "Беседа не найдена"}, status=404)
    version = get_object_or_404(CounterpartyKnowledgeVersion, pk=request.POST.get("version_id"))
    try:
        confirmed, binding = confirm_sewing_price_list(version, request.user, request.POST.get("currency", ""), request.POST.get("formula_confirmed") == "yes")
    except ValueError as error:
        return JsonResponse({"detail": str(error)}, status=400)
    add_assistant_message(
        conversation,
        f"Прайс подтверждён: версия {confirmed.version_number}. Калькулятор активирован.",
        "sewing_confirmed",
        {"provider_id": confirmed.counterparty_id, "binding_id": binding.pk, "provider_url": f"/tenders/production/base/providers/{confirmed.counterparty_id}/?tab=calculator&binding_id={binding.pk}"},
    )
    return JsonResponse({"messages": serialize_messages(conversation)})
