import hashlib

from django.db import transaction
from django.utils import timezone

from .models import Counterparty, CounterpartyKnowledgeVersion, ProviderKnowledgeStaging, StageCounterpartyLink


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


def confirm_knowledge(version, user, note=""):
    with transaction.atomic():
        version = CounterpartyKnowledgeVersion.objects.select_for_update().get(pk=version.pk)
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
