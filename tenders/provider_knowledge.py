import hashlib
import copy

from django.db import transaction
from django.utils import timezone

from .models import Counterparty, CounterpartyKnowledgeVersion, ProviderCalculatorBinding, ProviderKnowledgeStaging, StageCounterpartyLink
from .sewing_price_list import parse_sewing_workbook_bytes


def create_provider(user, name, stages, source_type="text", raw_content=b"", extracted_text=""):
    with transaction.atomic():
        counterparty = Counterparty.objects.create(name=name.strip(), created_by=user)
        for stage in stages:
            StageCounterpartyLink.objects.get_or_create(stage=stage, counterparty=counterparty)
        staging = ProviderKnowledgeStaging.objects.create(counterparty=counterparty, source_type=source_type, raw_content=raw_content or None, extracted_text=extracted_text, source_hash=hashlib.sha256(raw_content or extracted_text.encode()).hexdigest(), created_by=user)
    return counterparty, staging


def create_counterparty_with_knowledge(user, *, name, stage_ids, source_text="", upload=None):
    """Existing provider-form workflow, reusable by every client channel."""
    from .models import ProcessDefinition
    from .services import extract_calculation_source, parse_counterparty_draft

    name = str(name or "").strip()
    if not isinstance(stage_ids, list) or not all(isinstance(value, int) and not isinstance(value, bool) for value in stage_ids):
        raise ValueError("Некорректные производственные этапы")
    stages = list(ProcessDefinition.objects.filter(pk__in=stage_ids, is_active=True))
    if not name or not stages or len(stages) != len(set(stage_ids)):
        raise ValueError("Укажите название и хотя бы одну существующую возможность")
    source_text = str(source_text or "").strip()
    extracted = extract_calculation_source(source_text=source_text, upload=upload)
    extracted_text = extracted.get("content", source_text)
    if upload:
        upload.seek(0)
    raw = upload.read() if upload else source_text.encode()
    provider, staging = create_provider(user, name, stages, "file" if upload else "text", raw, extracted_text)
    draft_data = parse_counterparty_draft(extracted_text, [stage.name for stage in stages]) if extracted_text else {}
    draft = create_knowledge_draft(
        provider, user,
        {"summary": draft_data.get("summary", extracted_text[:4000]), "input_schema": [], "pricing": {},
         "ambiguities": draft_data.get("ambiguities", [])},
        staging=staging,
    )
    return {"counterparty_id": provider.pk, "knowledge_version_id": draft.pk}


def remove_counterparty_knowledge(user, *, counterparty_id, version_id):
    version = CounterpartyKnowledgeVersion.objects.filter(pk=version_id, counterparty_id=counterparty_id).first()
    if version is None:
        raise ValueError("Версия знаний не найдена")
    if version.status == CounterpartyKnowledgeVersion.STATUS_DRAFT:
        version.delete()
        return {"knowledge_version_id": version_id, "deleted": True}
    if version.status in {CounterpartyKnowledgeVersion.STATUS_SUPERSEDED, CounterpartyKnowledgeVersion.STATUS_INACTIVE}:
        version.source_metadata = {**version.source_metadata, "hidden": True}
        version.save(update_fields=["source_metadata"])
        return {"knowledge_version_id": version.pk, "hidden": True}
    raise ValueError("Активный прайс нельзя удалить: сначала подтвердите новый")


def create_knowledge_draft(counterparty, user, canonical_data, staging=None, stage=None, metadata=None):
    number = CounterpartyKnowledgeVersion.objects.filter(counterparty=counterparty, stage=stage).count() + 1
    return CounterpartyKnowledgeVersion.objects.create(counterparty=counterparty, stage=stage, staging=staging, version_number=number, canonical_data=canonical_data, source_metadata=metadata or {}, created_by=user)


def create_sewing_provider_draft(user, name, stage, raw_content, original_filename="Пошив.xls"):
    data = parse_sewing_workbook_bytes(raw_content)
    provider = Counterparty.objects.filter(name__iexact=name.strip()).first()
    if provider:
        StageCounterpartyLink.objects.get_or_create(stage=stage, counterparty=provider)
        staging = ProviderKnowledgeStaging.objects.create(counterparty=provider, source_type="file", raw_content=raw_content, extracted_text="Пошив.xls", source_hash=hashlib.sha256(raw_content).hexdigest(), created_by=user)
    else:
        provider, staging = create_provider(user, name, [stage], "file", raw_content, "Пошив.xls")
    staging.original_filename = original_filename[:255]
    staging.save(update_fields=["original_filename"])
    version = create_knowledge_draft(provider, user, data, staging=staging, stage=stage, metadata={"source_kind": "sewing_xls"})
    return provider, version


@transaction.atomic
def confirm_sewing_price_list(version, user, currency, formula_confirmed):
    version = CounterpartyKnowledgeVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != CounterpartyKnowledgeVersion.STATUS_DRAFT:
        raise ValueError("Черновик прайса уже был обработан")
    if currency.strip().upper() != "RUB" or not formula_confirmed:
        raise ValueError("Подтвердите валюту и то, что кэшированные значения XLS — цены за единицу")
    data = copy.deepcopy(version.canonical_data)
    data["requires_confirmation"] = False
    data["formula_status"] = "confirmed"
    data["pricing"]["currency"] = "RUB"
    draft = create_knowledge_draft(version.counterparty, user, data, stage=version.stage, metadata=version.source_metadata)
    confirmed = confirm_knowledge(draft, user, "Тестовое допущение: валюта RUB; кэшированные значения XLS подтверждены как цены за единицу.")
    binding = initialize_structured_rules_binding(confirmed)
    version.status = CounterpartyKnowledgeVersion.STATUS_INACTIVE
    version.save(update_fields=["status"])
    return confirmed, binding


def confirm_knowledge(version, user, note=""):
    with transaction.atomic():
        version = CounterpartyKnowledgeVersion.objects.select_for_update().get(pk=version.pk)
        if version.canonical_data.get("requires_confirmation"):
            raise ValueError("Нельзя подтвердить прайс без валюты и формулы")
        previous = CounterpartyKnowledgeVersion.objects.select_for_update().filter(counterparty=version.counterparty, stage=version.stage, status=CounterpartyKnowledgeVersion.STATUS_CONFIRMED).exclude(pk=version.pk)
        previous.update(status=CounterpartyKnowledgeVersion.STATUS_SUPERSEDED)
        version.status = CounterpartyKnowledgeVersion.STATUS_CONFIRMED
        version.confirmed_by = user
        version.confirmed_at = timezone.now()
        version.confirmation_note = note
        version.save(update_fields=["status", "confirmed_by", "confirmed_at", "confirmation_note"])
        if version.staging:
            version.staging.purge_raw()
    return version


def initialize_structured_rules_binding(version):
    if version.status != CounterpartyKnowledgeVersion.STATUS_CONFIRMED or not version.stage_id:
        raise ValueError("Для калькулятора нужна подтверждённая версия, привязанная к этапу")
    link = StageCounterpartyLink.objects.get(counterparty=version.counterparty, stage=version.stage)
    link.price_source_type = StageCounterpartyLink.SOURCE_PRICE_LIST
    link.save(update_fields=["price_source_type", "updated_at"])
    binding, _ = ProviderCalculatorBinding.objects.update_or_create(
        link=link, calculator_type=ProviderCalculatorBinding.TYPE_STRUCTURED_RULES,
        defaults={"knowledge_version": version, "is_active": True, "is_default": True, "name": "Прайс-лист"},
    )
    return binding
