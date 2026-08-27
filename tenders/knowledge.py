import hashlib
import json
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .models import ProcessDefinition, ProductionTrainingExample, ProductionType, TenderKnowledgeSource


SCHEMA_VERSION = 1


def _signature(payload, fields):
    value = {field: payload.get(field) for field in fields}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


EXAMPLE_SIGNATURE_FIELDS = (
    "production_type", "position_name", "requirements", "features", "routes", "note",
)
SOURCE_SIGNATURE_FIELDS = (
    "title", "supplier_name", "source_type", "url", "content_summary", "structured_data",
)


def export_knowledge_bundle(include_embeddings=False):
    examples = list(ProductionTrainingExample.objects.select_related("production_type", "superseded_by").order_by("created_at", "pk"))
    sources = list(TenderKnowledgeSource.objects.filter(is_active=True).order_by("created_at", "pk"))
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": timezone.now().isoformat(),
        "production_types": [{
            "code": value.code,
            "name": value.name,
            "description": value.description,
            "sort_order": value.sort_order,
            "is_active": value.is_active,
        } for value in ProductionType.objects.order_by("sort_order", "pk")],
        "process_definitions": [{
            "name": value.name,
            "role": value.role,
            "description": value.description,
            "is_active": value.is_active,
        } for value in ProcessDefinition.objects.order_by("role", "name")],
        "training_examples": [{
            "knowledge_id": str(value.knowledge_id),
            "production_type": value.production_type.code,
            "position_name": value.position_name,
            "requirements": value.requirements,
            "features": value.features,
            "routes": value.routes,
            "note": value.note,
            "is_active": value.is_active,
            "superseded_by": str(value.superseded_by.knowledge_id) if value.superseded_by else None,
            **({
                "embedding": value.embedding,
                "embedding_model": value.embedding_model,
                "embedding_updated_at": value.embedding_updated_at.isoformat() if value.embedding_updated_at else None,
            } if include_embeddings else {}),
        } for value in examples],
        "knowledge_sources": [{
            "knowledge_id": str(value.knowledge_id),
            "title": value.title,
            "supplier_name": value.supplier_name,
            "source_type": value.source_type,
            "url": value.url,
            "content_summary": value.content_summary,
            "structured_data": value.structured_data,
            "is_active": value.is_active,
        } for value in sources],
    }


@transaction.atomic
def import_knowledge_bundle(bundle, user):
    if not isinstance(bundle, dict) or bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия базы знаний.")

    for raw in bundle.get("production_types", []):
        ProductionType.objects.update_or_create(code=raw["code"], defaults={
            "name": raw["name"],
            "description": raw.get("description", ""),
            "sort_order": raw.get("sort_order", 0),
            "is_active": raw.get("is_active", True),
        })
    for raw in bundle.get("process_definitions", []):
        ProcessDefinition.objects.update_or_create(name=raw["name"], role=raw["role"], defaults={
            "description": raw.get("description", ""),
            "is_active": raw.get("is_active", True),
        })

    example_map = {}
    created_examples = 0
    for raw in bundle.get("training_examples", []):
        knowledge_id = UUID(raw["knowledge_id"])
        production_type = ProductionType.objects.get(code=raw["production_type"])
        fields = {
            "production_type": production_type.code,
            "position_name": raw["position_name"],
            "requirements": raw.get("requirements", {}),
            "features": raw.get("features", []),
            "routes": raw.get("routes", []),
            "note": raw.get("note", ""),
        }
        example = ProductionTrainingExample.objects.filter(knowledge_id=knowledge_id).first()
        if example is None:
            incoming_signature = _signature(fields, EXAMPLE_SIGNATURE_FIELDS)
            candidates = ProductionTrainingExample.objects.filter(
                production_type=production_type, position_name=raw["position_name"],
            )
            example = next((value for value in candidates if _signature({
                "production_type": value.production_type.code,
                "position_name": value.position_name,
                "requirements": value.requirements,
                "features": value.features,
                "routes": value.routes,
                "note": value.note,
            }, EXAMPLE_SIGNATURE_FIELDS) == incoming_signature), None)
        changed = example is not None and _signature({
            "production_type": example.production_type.code,
            "position_name": example.position_name,
            "requirements": example.requirements,
            "features": example.features,
            "routes": example.routes,
            "note": example.note,
        }, EXAMPLE_SIGNATURE_FIELDS) != _signature(fields, EXAMPLE_SIGNATURE_FIELDS)
        if example is None:
            example = ProductionTrainingExample(knowledge_id=knowledge_id, created_by=user)
            created_examples += 1
        else:
            example.knowledge_id = knowledge_id
        example.production_type = production_type
        example.position_name = fields["position_name"][:500]
        example.requirements = fields["requirements"]
        example.features = fields["features"]
        example.routes = fields["routes"]
        example.note = fields["note"][:500]
        example.is_active = raw.get("is_active", True)
        if changed:
            example.embedding = []
            example.embedding_model = ""
            example.embedding_updated_at = None
        if "embedding" in raw:
            example.embedding = raw.get("embedding") or []
            example.embedding_model = raw.get("embedding_model", "")[:100]
            updated_at = raw.get("embedding_updated_at")
            example.embedding_updated_at = datetime.fromisoformat(updated_at) if updated_at else None
        example.save()
        example_map[str(knowledge_id)] = example

    for raw in bundle.get("training_examples", []):
        example = example_map[str(UUID(raw["knowledge_id"]))]
        superseded_id = raw.get("superseded_by")
        superseded_by = example_map.get(str(UUID(superseded_id))) if superseded_id else None
        if example.superseded_by_id != (superseded_by.pk if superseded_by else None):
            example.superseded_by = superseded_by
            example.save(update_fields=["superseded_by"])

    created_sources = 0
    for raw in bundle.get("knowledge_sources", []):
        knowledge_id = UUID(raw["knowledge_id"])
        source = TenderKnowledgeSource.objects.filter(knowledge_id=knowledge_id).first()
        if source is None:
            incoming_signature = _signature(raw, SOURCE_SIGNATURE_FIELDS)
            candidates = TenderKnowledgeSource.objects.filter(title=raw["title"], supplier_name=raw.get("supplier_name", ""))
            source = next((value for value in candidates if _signature({
                "title": value.title,
                "supplier_name": value.supplier_name,
                "source_type": value.source_type,
                "url": value.url,
                "content_summary": value.content_summary,
                "structured_data": value.structured_data,
            }, SOURCE_SIGNATURE_FIELDS) == incoming_signature), None)
        if source is None:
            source = TenderKnowledgeSource(knowledge_id=knowledge_id, created_by=user)
            created_sources += 1
        else:
            source.knowledge_id = knowledge_id
        source.title = raw["title"][:300]
        source.supplier_name = raw.get("supplier_name", "")[:200]
        source.source_type = raw["source_type"]
        source.url = raw.get("url", "")[:1000]
        source.content_summary = raw.get("content_summary", "")
        source.structured_data = raw.get("structured_data", {})
        source.is_active = raw.get("is_active", True)
        source.save()

    return {
        "created_examples": created_examples,
        "created_sources": created_sources,
        "examples": len(example_map),
        "sources": len(bundle.get("knowledge_sources", [])),
    }
