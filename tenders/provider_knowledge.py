import hashlib

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


def create_knowledge_draft(counterparty, user, canonical_data, staging=None, stage=None, metadata=None):
    number = CounterpartyKnowledgeVersion.objects.filter(counterparty=counterparty, stage=stage).count() + 1
    return CounterpartyKnowledgeVersion.objects.create(counterparty=counterparty, stage=stage, staging=staging, version_number=number, canonical_data=canonical_data, source_metadata=metadata or {}, created_by=user)


def create_sewing_provider_draft(user, name, stage, raw_content, original_filename="Пошив.xls"):
    data = parse_sewing_workbook_bytes(raw_content)
    provider, staging = create_provider(user, name, [stage], "file", raw_content, "Пошив.xls")
    staging.original_filename = original_filename[:255]
    staging.save(update_fields=["original_filename"])
    version = create_knowledge_draft(provider, user, data, staging=staging, stage=stage, metadata={"source_kind": "sewing_xls"})
    return provider, version


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
