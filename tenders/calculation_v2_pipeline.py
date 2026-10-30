"""Milestone 2: durable tender understanding and tender-wide batch routing."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json
import re
import time
import os
from typing import Protocol

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from tender_selection.filtering import match_title, parse_terms
from tender_selection.models import DocumentPreview, FilterSettings, Tender
from tender_selection.notification import parse_notification

from .calculation_v2 import claim_next_job
from .document_enrichment import component_total_quantity, document_table_payload, effective_item_requirements, merge_item_requirements, notification_origin, notification_quote_source
from .models import (
    KnowledgeRecord,
    Lesson,
    ProcessDefinition,
    OwnerFeedbackEvent,
    OwnerInteraction,
    TenderComputeJob,
    TenderComputeLine,
    TenderSourceItem,
    TenderCommercialItem,
    CalculationComponent,
    ComponentRoutePlan,
    ComponentOperationStep,
)


class BatchRouter(Protocol):
    def route(self, *, tender: Tender, items: list[CalculationComponent], processes: list[ProcessDefinition], knowledge: list[dict]) -> list["RouteDecision"]: ...


class DocumentEnricher(Protocol):
    def extract(self, *, tender: Tender, aggregate: TenderSourceItem, documents: list[dict]) -> list["ExtractedItem"]: ...


@dataclass(frozen=True)
class RouteDecision:
    component_id: int
    process_ids: tuple[int, ...]
    confidence: Decimal | None
    rationale: dict
    needs_review: bool = False
    question: str = ""
    question_kind: str = "unspecified"
    missing_requirement_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractedItem:
    name: str
    quantity: Decimal | None
    unit: str
    requirements: dict
    provenance: dict
    confidence: Decimal | None
    relationship: str = "split"
    quantity_per_parent: Decimal = Decimal("1")
    parent_source_item_id: int | None = None
    parent_display_name: str = ""
    supply_kind: str = ""
    structure_reason: str = ""
    national_regime: dict | None = None


class ExistingKnowledgeBatchRouter:
    """Safe default: consumes existing route knowledge but never invents a route."""

    def route(self, *, tender, items, processes, knowledge):
        decisions = []
        active_by_id = {process.pk: process for process in processes}
        for item in items:
            normalized = _normal(item.name)
            matches = []
            for entry in knowledge:
                if entry["needle"] and entry["needle"] in normalized:
                    matches.extend(entry["process_ids"])
            process_ids = sorted({process_id for process_id in matches if process_id in active_by_id})
            if len(process_ids) == 1:
                decisions.append(RouteDecision(item.pk, (process_ids[0],), Decimal("0.9000"), {"source": "existing_knowledge"}))
            else:
                reason = "No applicable route knowledge" if not process_ids else "Conflicting route knowledge"
                decisions.append(RouteDecision(item.pk, (), Decimal("0.0000"), {"reason": reason}, True, "Уточните способ выполнения позиции."))
        return decisions


class NoopDocumentEnricher:
    def extract(self, *, tender, aggregate, documents):
        return []


def _route_processes(processes):
    fields = ("name", "description", "when_to_use", "when_not_to_use", "role", "supplies_input", "performs_production", "terminal_mode", "scope_tags", "parameters")
    return [{"id": process.pk, **{field: getattr(process, field) for field in fields}} for process in processes]


def _route_owner_answer_map(sources):
    from .models import OwnerFeedbackEvent
    from .document_enrichment import is_item_enrichment
    lineage = {}
    for source in sources:
        ids, current = {source.pk}, source
        while is_item_enrichment(current) and current.parent_id:
            current = current.parent
            ids.add(current.pk)
        lineage[source.pk] = ids
    ids = set().union(*lineage.values()) if lineage else set()
    events = list(OwnerFeedbackEvent.objects.filter(interaction__source_item_id__in=ids, interaction__status="resolved").select_related("interaction").order_by("pk"))
    return {source_id: [{"question": event.interaction.question, "answer": event.raw_text, "names": event.interaction.context.get("missing_requirement_names", [])}
                        for event in events if event.interaction.source_item_id in ancestors] for source_id, ancestors in lineage.items()}


def source_route_fingerprint(source, processes, knowledge, owner_answers=None):
    from .document_enrichment import effective_item_requirements
    return _fingerprint({"contract": "staged-route-v1", "source_id": source.pk, "name": source.original_text, "quantity": str(source.quantity), "unit": source.unit,
                         "requirements": effective_item_requirements(source), "owner_answers": _route_owner_answer_map([source])[source.pk] if owner_answers is None else owner_answers, "processes": _route_processes(processes), "knowledge": knowledge})


def _intake_thinking(model, namespace):
    default = "adaptive" if model.rsplit("/", 1)[-1] == "claude-haiku-5-5" else ""
    mode = os.getenv(f"CALCULATION_V2_{namespace}_THINKING_MODE", default)
    if mode not in {"", "disabled", "adaptive"}:
        raise ValueError("Некорректная настройка рассуждений при подготовке тендера")
    return {"type": mode} if mode else None


class GatewayBatchRouter:
    """One structured gateway request for all active items in a Tender."""
    def __init__(self, model=None, *, max_cost_rub=None):
        self.model = model or os.getenv("TIMEWEB_AI_V2_ROUTER_MODEL") or os.getenv("TIMEWEB_AI_ROUTE_MODEL") or os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "anthropic/claude-haiku-5-5")
        self.thinking = _intake_thinking(self.model, "ROUTE")
        self.max_cost_rub = Decimal(str(max_cost_rub if max_cost_rub is not None else os.getenv("CALCULATION_V2_ROUTE_MAX_COST_RUB", "2")))
        self.usage = {}
        self.cost_rub = 0.0
        self.diagnostics = {}

    def route(self, *, tender, items, processes, knowledge):
        from .gateway_budget import spend_rub, intake_spend_rub
        from .services import _ai_gateway_json
        request_started = timezone.now().isoformat()
        payload = {
            "tender": {"id": tender.pk, "title": tender.title, "subject": tender.object_info},
            "items": [{"component_id": item.pk, "name": item.name, "quantity": str(item.effective_quantity), "unit": item.unit, "requirements": item.requirements} for item in items],
        }
        shared_context = json.dumps({"processes": _route_processes(processes), "knowledge": knowledge}, ensure_ascii=False, default=str)
        prompt = """Route every tender item in one batch. Write all reasons and questions in Russian. A listed process is a commercially meaningful external execution capability: one provider may deliver the finished component alone, or multiple providers may be needed in a meaningful sequence. Do not decompose a finished result into internal factory operations. Do not invent a supply step merely because a product has a physical material. Use only listed process ids. Do not search, price, or calculate. Return JSON only: {\"items\":[{\"component_id\":integer,\"process_ids\":[integer],\"confidence\":number 0..1,\"reason\":string,\"alternatives\":[integer],\"needs_review\":boolean,\"question\":string,\"question_kind\":\"missing_information|conflicting_information|execution_choice|unspecified\",\"missing_requirement_names\":[string]}]}. Ask only for material uncertainty. Never ask for a requirement already present in the item requirements. For missing_information, list the exact requirement names that are missing. Context data follows:\n""" + json.dumps(payload, ensure_ascii=False, default=str)
        output_tokens = min(32000, max(8000, len(items) * 400))
        reserve = spend_rub({"prompt_tokens": len((shared_context + prompt).encode("utf-8")) + 4096, "completion_tokens": output_tokens}, self.model)
        if not self.max_cost_rub.is_finite() or self.max_cost_rub <= 0 or reserve is None or Decimal(str(reserve)) > self.max_cost_rub:
            self.diagnostics = {"provider_call_happened": False, "validation_failed": True, "final_failure_reason": "Маршрут не запущен: превышен лимит стоимости или неизвестен тариф модели."}
            raise ValueError(self.diagnostics["final_failure_reason"])
        try:
            raw, self.usage = _ai_gateway_json(prompt, shared_context=shared_context, reasoning_effort="low", model=self.model, max_tokens=output_tokens, timeout=180, network_attempts=1, json_attempts=1, capture_incomplete_response=True, **({"thinking": self.thinking} if self.thinking else {}), response_contract="Верни корневой объект с массивом items и одной записью на каждую переданную позицию. Не заменяй результат объяснением.")
        except Exception as exc:
            self.usage = dict(getattr(exc, "usage", {}) or {})
            self.cost_rub = float(intake_spend_rub(self.usage, self.model) or 0)
            self.diagnostics = {
                "provider_call_happened": True,
                "model": self.model,
                "started_at": request_started,
                "validation_failed": True,
                "retry_count": 0,
                "final_failure_reason": str(exc)[:1000],
                "usage": self.usage,
                "cost_rub": str(self.cost_rub),
            }
            raise
        self.cost_rub = float(intake_spend_rub(self.usage, self.model) or 0)
        self.diagnostics = {"provider_call_happened": True, "model": self.model, "started_at": request_started, "thinking": self.thinking, "output_token_limit": output_tokens, "reserved_cost_rub": str(reserve), "retry_count": 0, "usage": self.usage, "cost_rub": str(self.cost_rub)}
        rows = raw.get("items") if isinstance(raw, dict) else None
        rows = rows if isinstance(rows, list) else []
        process_ids = {process.pk for process in processes}
        output, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = row.get("component_id")
            if type(item_id) is not int or item_id not in {item.pk for item in items} or item_id in seen:
                continue
            seen.add(item_id)
            raw_process_ids = row.get("process_ids")
            if not isinstance(raw_process_ids, list):
                raw_process_ids = [row.get("process_id")]
            selected_process_ids = tuple(value for value in raw_process_ids if type(value) is int and value in process_ids)
            valid = bool(selected_process_ids) and len(selected_process_ids) == len(raw_process_ids)
            confidence = _as_decimal(row.get("confidence"))
            confidence = confidence if confidence is not None and Decimal("0") <= confidence <= Decimal("1") else Decimal("0")
            needs_review = bool(row.get("needs_review")) or not valid
            question_kind = str(row.get("question_kind") or "unspecified")
            if question_kind not in {"missing_information", "conflicting_information", "execution_choice", "unspecified"}:
                question_kind = "unspecified"
            missing_names = row.get("missing_requirement_names")
            missing_names = tuple(str(name).strip()[:200] for name in missing_names if str(name).strip()) if isinstance(missing_names, list) else ()
            output.append(RouteDecision(item_id, selected_process_ids if valid else (), confidence, {"reason": str(row.get("reason") or "")[:1000], "alternatives": row.get("alternatives") if isinstance(row.get("alternatives"), list) else []}, needs_review, str(row.get("question") or "Уточните способ выполнения позиции.")[:500], question_kind, missing_names))
        for item in items:
            if item.pk not in seen:
                output.append(RouteDecision(item.pk, (), Decimal("0"), {"reason": "Не получен маршрут этой позиции"}, True, "Нужна проверка маршрута позиции."))
        return output


class GatewayDocumentEnricher:
    """One bounded, evidence-only document analysis for the pre-routing intake stage."""

    # One provider attempt avoids paid retries. One ruble is a portfolio target,
    # tracked in diagnostics, not a reason to
    # leave an unusually rich tender without document analysis.
    DOCUMENT_OUTPUT_TOKEN_LIMIT = int(os.getenv("CALCULATION_V2_DOCUMENT_OUTPUT_TOKEN_LIMIT", "32000"))
    DOCUMENT_AVERAGE_COST_TARGET_RUB = 1.00

    def __init__(self, model=None, *, max_cost_rub=None):
        self.model = model or os.getenv("TIMEWEB_AI_V2_EXTRACTION_MODEL") or os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "anthropic/claude-haiku-5-5")
        # Haiku 5.5 thinks adaptively by default; that shares the JSON output cap.
        # Other extraction models retain their existing provider defaults.
        default_effort = "low" if self.model.rsplit("/", 1)[-1] == "claude-haiku-5-5" else ""
        self.reasoning_effort = os.getenv("CALCULATION_V2_DOCUMENT_REASONING_EFFORT", default_effort) or None
        self.thinking = _intake_thinking(self.model, "DOCUMENT")
        if self.reasoning_effort not in {None, "low", "medium", "high"}:
            raise ValueError("Некорректная настройка глубины разбора документов")
        self.max_cost_rub = Decimal(str(max_cost_rub)) if max_cost_rub is not None else None
        self.usage = {}
        self.cost_rub = 0.0
        self.diagnostics = {}
        self.document_images = []

    STAGED_CONTRACT = "intake-steps-v1"
    STAGED_VALIDATION_VERSION = "math-symbols-and-cell-segments-v2"

    def prepare_stages(self, tender, sources, documents):
        from tender_selection.services import documents_html
        documents_html(tender, [doc for doc in documents if doc.get('url')], timeout=5, archive_timeout=5, direct_timeout=5)
        self.stage_sources = {source.pk: source for source in sources}
        self.stage_documents, considered = self._bounded_documents(documents, compact=True)
        if not self.stage_documents or any(doc['reading_status'] != 'read' for doc in considered):
            raise ValueError('Документы ещё не прочитаны полностью; разбор не отправлен модели.')
        self.document_lookup = {index: doc for index, doc in enumerate(self.stage_documents, 1)}
        self.baselines = {source.pk: [{'id': i, 'name': fact['name'], 'value': fact['value']} for i, fact in enumerate(effective_item_requirements(source).get('characteristics', []))] for source in sources}
        self.shared_context = json.dumps({'notification': tender.notification_raw,
            'working_items': [{'source_id': source.pk, 'name': source.original_text, 'quantity': str(source.quantity), 'unit': source.unit, 'baseline_facts': self.baselines[source.pk]} for source in sources],
            'documents': [{'doc_id': index, 'name': doc['name'], 'paragraphs': doc['text'], 'tables': doc['tables'], 'image_indexes': doc['image_indexes']} for index, doc in self.document_lookup.items()]}, ensure_ascii=False, default=str)
        return {'documents': considered, 'context_characters': len(self.shared_context), 'contract': self.STAGED_CONTRACT}

    def ask_stage(self, job, stage, instruction, task, *, output_tokens):
        from .services import _ai_gateway_json
        from .gateway_budget import spend_rub, intake_spend_rub
        prompt = instruction + '\n' + json.dumps(task, ensure_ascii=False, default=str)
        fingerprint = _fingerprint({'contract': self.STAGED_CONTRACT, 'context': self.shared_context, 'task': prompt, 'model': self.model, 'thinking': self.thinking, 'effort': self.reasoning_effort})
        history = TenderComputeJob.objects.filter(tender=job.tender, **{
            f'diagnostics__stages__{stage}__input_fingerprint': fingerprint,
            f'diagnostics__stages__{stage}__state__in': ['complete', 'failed'],
        }).exclude(pk=job.pk).order_by('-pk')
        recoverable = None
        for cached in history:
            previous = cached.diagnostics['stages'][stage]
            rows = previous.get('result')
            if not isinstance(rows, list):
                continue
            if previous.get('state') == 'complete':
                return rows, {'state': 'complete', 'input_fingerprint': fingerprint, 'reused_from': cached.pk,
                    'result': rows, 'validation_version': self.STAGED_VALIDATION_VERSION, 'cost_rub': '0', 'duration_ms': 0}
            if previous.get('state') == 'failed' and previous.get('validation_version') != self.STAGED_VALIDATION_VERSION and recoverable is None:
                recoverable = (cached.pk, rows)
        if recoverable:
            source_id, rows = recoverable
            return rows, {'state': 'pending_validation', 'input_fingerprint': fingerprint, 'reused_from': source_id,
                'result': rows, 'validation_version': self.STAGED_VALIDATION_VERSION, 'cost_rub': '0', 'duration_ms': 0}
        # Reserve the uncached cache-write price, not an assumed future cache hit.
        reserve = spend_rub({'prompt_tokens': len((self.shared_context + prompt).encode('utf-8')) + 4096 + sum(len(value) for value in self.document_images), 'completion_tokens': output_tokens}, self.model)
        remaining = self.max_cost_rub - Decimal(str(self.cost_rub)) if self.max_cost_rub is not None else Decimal('10')
        if reserve is None or Decimal(str(reserve)) * Decimal('1.25') > remaining:
            raise ValueError('Следующий этап превышает оставшийся бюджет; запрос не отправлен.')
        started = time.monotonic()
        started_at = timezone.now().isoformat()
        try:
            raw, usage = _ai_gateway_json(prompt, shared_context=self.shared_context, model=self.model,
                max_tokens=output_tokens, timeout=180, network_attempts=1, json_attempts=1,
                capture_incomplete_response=True, thinking=self.thinking, reasoning_effort=self.reasoning_effort,
                image_data_urls=self.document_images, response_contract='Верни один полный JSON-объект с массивом items. Весь ответ должен быть валидным JSON.')
        except Exception as exc:
            usage = getattr(exc, 'usage', {}) or {}
            self.cost_rub += float(intake_spend_rub(usage, self.model) or 0)
            job.diagnostics.setdefault('stages', {})[stage] = {'state': 'failed', 'model': self.model, 'usage': usage, 'cost_rub': str(intake_spend_rub(usage, self.model)), 'started_at': started_at, 'error': str(exc), 'duration_ms': round((time.monotonic()-started)*1000)}
            raise
        amount = intake_spend_rub(usage, self.model)
        self.cost_rub += float(amount or 0)
        self.usage = {key: self.usage.get(key, 0) + usage.get(key, 0) for key in ('prompt_tokens', 'completion_tokens')}
        if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
            job.diagnostics.setdefault('stages', {})[stage] = {'state': 'failed', 'model': self.model, 'usage': usage, 'cost_rub': str(amount), 'started_at': started_at, 'error': 'Не получен полный список результатов'}
            raise ValueError('Этап не вернул полный список результатов.')
        return raw['items'], {'state': 'pending_validation', 'model': self.model, 'usage': usage, 'cost_rub': str(amount), 'started_at': started_at,
            'duration_ms': round((time.monotonic()-started)*1000), 'input_fingerprint': fingerprint, 'validation_version': self.STAGED_VALIDATION_VERSION, 'result': raw['items']}

    def stage_quote(self, row, *, required=True):
        if isinstance(row.get('evidence'), list):
            if not row['evidence']:
                if required:
                    raise ValueError('Не указаны подтверждающие фрагменты документа.')
                return {}
            sources = [self.stage_quote(part) for part in row['evidence']]
            return {**sources[0], 'evidence': '\n'.join(source['evidence'] for source in sources), 'evidence_sources': sources}
        doc = self.document_lookup.get(row.get('doc_id'))
        quote = str(row.get('quote') or '').strip()
        def normalize(value):
            # Document converters and LLM output use equivalent mathematical glyphs.
            # Do not remove inequality signs, digits, words or invert comparisons.
            import unicodedata
            text = unicodedata.normalize('NFKC', str(value)).casefold()
            text = text.translate(str.maketrans({'⩽': '≤', '≦': '≤', '⩾': '≥', '≧': '≥'}))
            text = ' '.join(text.split())
            text = re.sub(r'\\s*([≤≥<>])\\s*', r'\\1', text)
            # The supplier may quote a standard unit abbreviation while the
            # document table spells out the same unit. Only literal equivalents
            # are admitted; amounts and numeric ranges must still match.
            for full, short in (('миллиметр', 'мм'), ('сантиметр', 'см')):
                text = re.sub(r'(?<!\\w)' + full + r'(?!\\w)', short, text)
            return text

        verified = bool(doc and quote and normalize(quote) in normalize(doc.get('evidence_text', '')))
        section = str(row.get('section') or '')[:300]
        if doc and quote and not verified:
            # Match pipe-delimited excerpts inside ONE source row. Some converters
            # keep label and value in one cell; others split them into columns.
            # A fragment may continue within the SAME or the NEXT nonempty cell;
            # never skip a nonempty cell or join adjacent rows.
            parts = [normalize(part) for part in quote.split('|') if normalize(part)]
            for table_index, table in enumerate(doc.get('tables', []), 1):
                for row_index, cells in enumerate(table['rows'], 1):
                    texts = [cell if isinstance(cell, str) else cell['text'] for cell in cells]
                    texts = [text for text in texts if normalize(text)]
                    normalized = [normalize(text) for text in texts]
                    for start in range(len(texts)):
                        column, cursor, matched = start, 0, True
                        for part in parts:
                            hit = normalized[column].find(part, cursor)
                            if hit < 0 and cursor and column + 1 < len(texts):
                                column += 1
                                cursor = 0
                                hit = normalized[column].find(part)
                            if hit < 0:
                                matched = False
                                break
                            cursor = hit + len(part)
                        if parts and matched:
                            verified = True
                            quote = ' | '.join(texts[start:column + 1])
                            section = f'Таблица {table_index}, строка {row_index}'
                            break
                    if verified:
                        break
                if verified:
                    break
        if not verified:
            if required:
                excerpt = quote[:120].replace('\\n', ' ')
                raise ValueError(f'Результат не подтверждён цитатой из документа (doc_id={row.get("doc_id")}, фрагмент={excerpt!r}).')
            return {}
        return {'document_url': doc['url'], 'page_or_section': section, 'evidence': quote[:1000], 'extraction_model': self.model, 'extraction_version': self.STAGED_CONTRACT}

    def validate_structure(self, rows):
        seen, goods = set(), []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Некорректная запись структуры поставки")
            source_id = row.get('source_id')
            if type(source_id) is not int or source_id not in self.stage_sources or source_id in seen:
                raise ValueError('Структура не соответствует исходным позициям.')
            seen.add(source_id)
            kind = row.get('kind')
            if kind not in {'finished', 'kit', 'unclear'} or not str(row.get('reason') or '').strip():
                raise ValueError('Не определена структура позиции.')
            content = row.get('goods')
            if not isinstance(content, list) or not content or (kind != 'kit' and len(content) != 1):
                raise ValueError('Для позиции не получен корректный состав.')
            if kind == 'kit' and len(content) < 2:
                raise ValueError('Набор распознан, но его составляющие не перечислены.')
            source = self.stage_sources[source_id]
            provenance = self.stage_quote(row, required=kind != 'unclear')
            names = set()
            for index, item in enumerate(content):
                name = str(item.get('name') or '').strip()
                if not name or _normal(name) in names:
                    raise ValueError('В составе пустое или повторяющееся изделие.')
                names.add(_normal(name))
                per_parent = _as_decimal(item.get('quantity_per_parent')) if kind == 'kit' else Decimal('1')
                if per_parent is None or not per_parent.is_finite() or per_parent <= 0:
                    raise ValueError('Набор распознан, но количество составляющей не определено.')
                component_total_quantity(source.quantity, per_parent)
                goods.append({'key': f'{source_id}:{index}', 'source_id': source_id, 'name': name, 'kind': kind,
                    'unit': str(item.get('unit') or source.unit), 'quantity_per_parent': str(per_parent),
                    'parent_name': str(row.get('name') or source.original_text), 'reason': row['reason'], 'provenance': provenance})
        if seen != set(self.stage_sources):
            raise ValueError('Модель пропустила исходные позиции при проверке состава.')
        return goods

    def prepared_entries(self, goods, specifications=None, national=None):
        from tender_selection.national_regime import document_item
        entries = []
        for good in goods:
            source = self.stage_sources[good['source_id']]
            component = good['kind'] == 'kit'
            requirements = (specifications or {}).get(good['key']) or {'reconciled': True, 'keep_refs': [] if component else list(range(len(self.baselines[source.pk]))), 'characteristics': []}
            entries.append(ExtractedItem(good['name'], None if component else source.quantity, good['unit'] if component else source.unit,
                requirements, good['provenance'], Decimal('0.9'), 'component' if component else 'enrich', Decimal(good['quantity_per_parent']), source.pk,
                good['parent_name'], good['kind'], good['reason'], (national or {}).get(good['key'], {})))
        self.prepared_items = entries
        self.diagnostics = {'outcome': 'success', 'model': self.model, 'usage': self.usage, 'cost_rub': str(self.cost_rub), 'provider_call_happened': bool(self.usage)}
        return entries

    def validate_specifications(self, rows, goods):
        expected, result = {good['key']: good for good in goods}, {}
        for row in rows:
            key = row.get('key')
            if key not in expected or key in result or not isinstance(row.get('characteristics'), list):
                raise ValueError('ТЗ не соответствует списку товаров.')
            good = expected[key]
            facts = []
            for raw in row['characteristics']:
                fact = dict(raw)
                if not str(fact.get('name') or '').strip() or fact.get('value') in (None, '', [], {}):
                    raise ValueError('В ТЗ получена пустая характеристика.')
                if fact.get('resolution') != 'keep' and fact.get('resolution') != 'omit':
                    evidence = self.stage_quote(fact)
                    fact['source_url'] = evidence['document_url']
                    fact['_source_provenance'] = evidence
                    if fact.get('resolution') in {'merge', 'conflict'}:
                        fact['document_value'] = str(raw.get('document_value') or raw.get('quote') or '')
                    fact['page_or_section'] = evidence['page_or_section']
                fact.pop('doc_id', None)
                fact.pop('quote', None)
                fact.pop('section', None)
                facts.append(fact)
            result[key] = {'reconciled': True, 'keep_refs': row.get('keep_refs', []), 'characteristics': facts}
            if good['kind'] != 'kit':
                merge_item_requirements(effective_item_requirements(self.stage_sources[good['source_id']]), result[key], provenance=good['provenance'])
        if set(result) != set(expected):
            raise ValueError('ТЗ получено не для всех товаров.')
        return result

    def _bounded_documents(self, documents, *, compact=False):
        """Prepare full extracted text; budget checks must reject, never clip, the request."""
        from lxml import html as html_parser
        from tender_selection.documents import MAX_BYTES

        candidates = []
        self.document_images = []
        for doc in documents:
            url = doc.get("url")
            if not url:
                continue
            cached = DocumentPreview.objects.filter(url=url).values("html", "kind", "error").first() or {}
            html = cached.get("html") or ""
            if len(html.encode("utf-8")) > MAX_BYTES:
                raise ValueError("Document preview exceeds the parsing size limit; no model request was sent")
            root = html_parser.fragment_fromstring(html or "<div></div>", create_parent="div")
            image_nodes = root.xpath(".//img")
            image_indexes = []
            for node in image_nodes:
                image_url = node.get("src", "")
                if not re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+", image_url):
                    continue
                try:
                    import base64
                    import io
                    from PIL import Image
                    with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1], validate=True))) as image:
                        image.verify()
                except Exception:
                    continue
                self.document_images.append(image_url)
                image_indexes.append(len(self.document_images))
            missing_images = len(image_nodes) != len(image_indexes)
            warning = missing_images or bool(root.xpath(".//*[@data-document-warning] | .//*[contains(concat(' ', normalize-space(@class), ' '), ' ts-sub ')]"))
            warnings = [" ".join(node.itertext()).strip()[:500] for node in root.xpath(".//*[@data-document-warning] | .//*[contains(concat(' ', normalize-space(@class), ' '), ' ts-sub ')]")]
            if missing_images:
                warnings.append("Некоторые изображения недоступны для визуального чтения.")
            for node in root.xpath(".//script | .//style | .//img | .//*[@data-document-info] | .//*[@data-document-warning] | .//*[contains(concat(' ', normalize-space(@class), ' '), ' ts-sub ')] | .//*[contains(concat(' ', normalize-space(@class), ' '), ' ts-doc-page__number ')]"):
                node.drop_tree()
            compact_tables = document_table_payload(html_parser.tostring(root, encoding="unicode"), table_limit=None, cell_limit=None, expand=False) if compact else None
            evidence_text = " ".join(" ".join(root.itertext()).split())
            if compact:
                for table in root.xpath(".//table"):
                    if table.getparent() is not None:
                        table.drop_tree()
            content_text = " ".join(root.xpath(".//text()[not(ancestor::h4)]")).strip()
            text = " ".join(" ".join(root.itertext()).split()) if content_text and cached.get("kind") != "zip" else ""
            candidates.append({"url": url, "name": doc.get("name", ""), "text": text,
                               "html": html_parser.tostring(root, encoding="unicode"), "error": cached.get("error") or "", "warning": warning, "warnings": warnings, "cache_kind": cached.get("kind"), "image_indexes": image_indexes, "compact_tables": compact_tables, "evidence_text": evidence_text})
        if not candidates:
            return [], []
        available, considered = [], []
        for candidate in candidates:
            text = candidate["text"]
            status = "unreadable" if not text and not candidate["image_indexes"] and not candidate.get("compact_tables") else "partial" if candidate["error"] or candidate["warning"] else "read"
            entry = {"url": candidate["url"], "name": candidate["name"], "text": text, "image_indexes": candidate["image_indexes"]}
            entry["tables"] = candidate["compact_tables"] if compact else document_table_payload(candidate["html"]) if text else []
            if compact:
                entry["evidence_text"] = candidate["evidence_text"]
            available.append(entry)
            considered.append({"url": candidate["url"], "name": candidate["name"],
                               "cached_text_chars": len(candidate["text"]), "sent_text_chars": len(text),
                               "table_count": len(entry.get("tables", [])), "reading_status": status,
                               "error": candidate["error"], "warnings": candidate["warnings"], "cache_kind": candidate["cache_kind"], "image_count": len(candidate["image_indexes"])})
        return available, considered

    def _preflight_cost(self, prompt):
        """Return a conservative upper estimate, or None when the tariff is unknown."""
        from .gateway_budget import spend_rub

        # Normal diagnostic estimate; explicitly budgeted runs reserve by bytes
        # instead, including overhead, before allowing any gateway request.
        usage = {
            "prompt_tokens": (len(prompt) + 1) // 2,
            "completion_tokens": self.DOCUMENT_OUTPUT_TOKEN_LIMIT,
        }
        if self.max_cost_rub is not None:
            usage["prompt_tokens"] = len(prompt.encode("utf-8")) + 4096
        # Reserve encoded image bytes as an additional conservative input allowance.
        usage["prompt_tokens"] += sum(len(value.encode("ascii")) for value in self.document_images)
        return spend_rub(usage, self.model), usage

    def _record_gateway_failure(self, exc, *, usage, phase, started):
        failure_usage = dict(getattr(exc, "usage", {}) or {})
        total_usage = {
            key: (usage.get(key, 0) or 0) + (failure_usage.get(key, 0) or 0)
            for key in {**usage, **failure_usage}
        }
        self.usage = total_usage
        from .gateway_budget import spend_rub
        self.cost_rub = float(spend_rub(total_usage, self.model) or 0)
        self.diagnostics = {
            "outcome": "system_extraction_failure",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort, "thinking": self.thinking,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "cost_rub": str(self.cost_rub),
            "provider_call_happened": True,
            "validation_failed": True,
            "retry_count": 1,
            "final_failure_reason": str(exc)[:1000],
            "failure_phase": phase,
            "usage": total_usage,
        }
        if isinstance(getattr(exc, "incomplete_response", None), dict):
            self.diagnostics["incomplete_response"] = exc.incomplete_response

    def extract_tender(self, *, tender, source_items, documents):
        """Read every available document once and map evidence back to source rows."""
        from .gateway_budget import spend_rub
        from .services import _ai_gateway_json

        if getattr(self, "prepared_items", None) is not None:
            return self.prepared_items
        started = time.monotonic()
        self.diagnostics = {}
        allowed_source_ids = {item.pk for item in source_items}
        from tender_selection.services import documents_html
        documents_html(tender, [doc for doc in documents if doc.get("url")], timeout=5, archive_timeout=5, direct_timeout=5)
        available, considered = self._bounded_documents(documents)
        incomplete = any(doc["reading_status"] != "read" for doc in considered)
        if not any(doc["text"] or doc["image_indexes"] for doc in available):
            self.diagnostics = {"outcome": "document_reading_incomplete", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "documents": {"considered": considered, "skipped_reason": "document_preview_missing"}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        notification_rows = _notification_rows(tender)
        notification_items = []
        for item in source_items:
            original_source = notification_origin(item, same_item=True)
            index = (original_source.metadata or {}).get("notification_index") if original_source else None
            original = notification_rows[index] if isinstance(index, int) and 0 <= index < len(notification_rows) else {}
            notification_items.append({"id": item.pk, "name": item.original_text, "quantity": str(item.quantity or ""),
                                       "unit": item.unit,
                                       "baseline_facts": [{"id": index, "name": fact["name"], "value": fact["value"]} for index, fact in enumerate(effective_item_requirements(item).get("characteristics", []))],
                                       "notification_position": index + 1 if isinstance(index, int) else None, "original_notification_name": original.get("name"),
                                       "notification_unit_price": original.get("price") if notification_quote_source(item) else None, "notification_total": original.get("sum") if notification_quote_source(item) else None,
                                       "relationship": (item.metadata or {}).get("relationship"), "aggregate_name": (item.metadata or {}).get("parent_display_name"),
                                       "existing_components": [{"name": child.original_text, "quantity": str(child.quantity), "unit": child.unit, "facts": [{"name": fact["name"], "value": fact["value"], **({"scope": fact["scope"]} if fact.get("scope") else {})} for fact in effective_item_requirements(child).get("characteristics", [])]} for child in tender.v2_source_items.filter(parent=item, is_active=True, metadata__relationship="component")]})
        if callable(getattr(self, "progress_callback", None)):
            self.progress_callback()
        payload = {"notification_items": notification_items, "documents": available}
        payload["notification_payload"] = tender.notification_raw or {}
        prompt = """Read every supplied tender document and compare it with every notification item. Also extract optional national_regime per item: {status:ban|restriction|advantage|none|unknown,basis:[{quote:exact_verbatim_excerpt,source_url,page_or_section,scope:item|all_items,item_position:integer}]}. Report only the customer's explicit PP1875 requirements, never infer them from OKPD2, general legal knowledge or silence. Excerpts must contain the actual requirement and the PP1875 reference; for item scope include this item's name or its notification position explicitly in the quote. all_items is allowed only when the quote explicitly applies to all supplied goods. When structured notification flags already supply the status, do not repeat them as document evidence; unknown national_regime may be omitted. Components cannot inherit the parent set restriction without item-specific or explicit all-goods evidence. none requires an explicit statement of absence; missing evidence means unknown. Do not rewrite or summarize quotes. Use the entire notification_payload as context, including structured national-regime fields; never treat organizational/SME benefits as goods-origin preferences. Attached images are numbered from 1 in attachment order; document image_indexes associate each picture with its source. Read the images together with that document text. Return JSON only {\"items\":[{\"parent_source_item_id\":integer,\"parent_display_name\":string,\"name\":string,\"quantity\":number|null,\"unit\":string,\"requirements\":object,\"confidence\":number,\"source_url\":string,\"page_or_section\":string,\"evidence\":string,\"relationship\":\"enrich|split|component\",\"quantity_per_parent\":number,\"supply_kind\":\"finished|kit|unclear\",\"structure_reason\":string}]}. The supplied working items and their requirements are the baseline, not disposable data. existing_components describe a previously accepted breakdown of this source. Reassess that entire structure against the documents: keep a genuine kit or propose one finished product with completeness when the prior split was wrong. Account for prior component requirements in the proposed product/completeness or explain excluded information; do not silently discard specifics. Reconcile every item across ALL supplied sources, attribute by attribute. For enrich, return exactly ONE synthesized row per atomic notification item, combining documented additions or corrections across all sources; never omit known baseline facts in favor of a shorter description. Reuse the existing characteristic label for the same attribute; avoid repeated or synonymous labels. Preserve differing source values with evidence instead of silently choosing one. Choose the clearest specific human product name across the sources, without procurement boilerplate, repetition or loss of identifying meaning. Technical position codes identify the parent; they are not product characteristics. The backend preserves baseline facts and original quoted money for the same item. For enrich preserve quantity and the existing unit label unless documents explicitly change them; do not paraphrase an equivalent unit. For enrich set requirements.reconciled=true. Return requirements.keep_refs as a compact array of baseline fact ids to preserve UNCHANGED; do not repeat these facts in the response. requirements.characteristics contains ONLY new, changed, conflicting or explicitly omitted facts. The backend restores unchanged facts and compiles ONE complete canonical specification. Cover every baseline fact exactly once across keep_refs and characteristic baseline_refs. Empty baseline means keep_refs=[]. Each entry has name,value,resolution (keep|merge|conflict|new|omit), baseline_refs (array of baseline_facts.id), reason, and document_facts [{value,source_url,page_or_section}] when using document evidence. For a new fact from a single document, use source_url and page_or_section directly on the characteristic; do NOT repeat the value in document_facts, document_value, reason or evidence. Only merge/conflict need document_facts with differing source values. Each document fact cites its own supplied source_url; never attribute different documents to one shared source. merge/conflict may have no baseline_refs if supported by two or more document_facts. Reasons are concise human explanations without identifiers or technical keys. Cover EVERY baseline fact exactly once. Use keep_refs for unchanged facts; a single keep row is needed only to assign scope without changing the fact. Never repeat unchanged names, values or evidence: the backend copies them exactly. Keep metadata reasons and evidence to one concise sentence, but never shorten actual technical facts. For renamed or changed wording use merge/conflict instead of keep; merge combines compatible general and specific statements into one informative value without losing specifics; conflict retains genuinely incompatible alternatives, never silently chooses one. new MUST use baseline_refs=[]; when baseline_facts is empty, all output facts must be new with document evidence, never keep/merge/omit referencing nonexistent baseline facts. existing_components are contextual prior descriptions, not numbered baseline_facts; preserve their specifics as source-backed new facts when absent from the actual baseline. keep requires exactly one valid baseline reference and cannot rename or change its value. Do not put baseline references on new entries; use merge/conflict when reconciling referenced original facts. omit requires an explicit reason (e.g. repeated technical code or generic legal boilerplate) and preserves the removed fact for visible review. For merge/conflict/omit explain the decision briefly. All original values remain in the source trail. Do not mistake compatible descriptions for contradictions. Do not use this contract to inherit aggregate facts into components. Classify each source as supply_kind=finished|kit|unclear and provide structure_reason with a concise document-grounded explanation. A finished product remains ONE enrich row even when its mandatory delivered contents are enumerated. Its included parts, accessories, documents and packaging belong to its completeness, not automatically to separate merchandise. A kit contains distinct independently specified goods combined into one supplied unit; return component rows only when the documents substantiate that distinction, not merely because there is a list, separate specifications or quantities. Never decide by product-name keyword lists. For unclear cases preserve ONE whole source as enrich with supply_kind=unclear, explain the ambiguity for the existing human accept/reject review; do not invent a split or a new question flow. For kit component rows, supply_kind must be kit, structure_reason explains why these are separate goods, and parent_display_name names the supplied kit. Set component quantity=null and give documented positive quantity_per_parent; backend computes totals. Do not emit a priced aggregate as an extra component. requirements.characteristics entries may have scope=product|completeness: describe delivered completeness as source-backed facts with scope=completeness on the finished product, using the same reconciliation contract. Completeness is preserved in the same canonical specification with its scope. Merge repeated mentions across documents without duplicating physical items; preserve separate variants and separate parents. parent_display_name is the concise documented name of the aggregate. Each name identifies the product clearly. requirements.characteristics is an array of human-readable {name,value} entries describing the product, its manufacture, branding, packaging and item-specific constraints. Do not substitute generic contract conditions or procurement wording for technical characteristics, and do not repeat global legal clauses for every product. Return enrich for one finished supplied product including its completeness, or an unclear whole position. Return split only where a document explicitly replaces an aggregate with separately quantified goods. Where notification names repeat, distinguish the source rows by their notification positions, documented quantities, prices and characteristics; a repeated name alone does not establish a parent match. An aggregate price belongs to the aggregate, never to each component. Do not use document names as evidence. Never infer products, omit a supplied document merely because of its title, or attach evidence to a different notification item. If evidence is insufficient, return no row.\n""" + json.dumps(payload, ensure_ascii=False, default=str)
        estimated_cost, estimated_usage = self._preflight_cost(prompt)
        if estimated_cost is None:
            self.diagnostics = {"outcome": "unpriced_model", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "documents": {"considered": considered}, "budget": {"average_target_rub": str(self.DOCUMENT_AVERAGE_COST_TARGET_RUB), "estimated_cost_rub": None, "estimated_usage": estimated_usage}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        if self.max_cost_rub is not None and Decimal(str(estimated_cost)) > self.max_cost_rub:
            self.diagnostics = {"outcome": "budget_blocked", "model": self.model, "documents": {"considered": considered},
                                "budget": {"max_cost_rub": str(self.max_cost_rub), "reserved_cost_rub": str(estimated_cost)}}
            return []
        try:
            raw, extract_usage = _ai_gateway_json(prompt, model=self.model, max_tokens=self.DOCUMENT_OUTPUT_TOKEN_LIMIT, timeout=240, network_attempts=1, json_attempts=1, capture_incomplete_response=True, response_contract='Корневой JSON — объект с обязательным массивом items. Если товаров для уточнения нет: {"items":[]}. Не заменяй результат объяснением или объектом ошибки.', **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort is not None and self.thinking != {"type": "disabled"} else {}), **({"thinking": self.thinking} if self.thinking else {}), **({"image_data_urls": self.document_images} if self.document_images else {}))
        except Exception as exc:
            self._record_gateway_failure(exc, usage={}, phase="document_analysis", started=started)
            raise
        self.usage = extract_usage
        self.cost_rub = float(spend_rub(self.usage, self.model) or 0)
        response_shape = type(raw).__name__
        if isinstance(raw, list):
            raw = {"items": raw}
        elif isinstance(raw, dict) and {"parent_source_item_id", "name", "requirements", "source_url"} <= raw.keys():
            raw = {"items": [raw]}
        elif isinstance(raw, dict) and isinstance(raw.get("items"), dict) and {"parent_source_item_id", "name", "requirements", "source_url"} <= raw["items"].keys():
            raw = {**raw, "items": [raw["items"]]}
        allowed_urls = {doc["url"] for doc in available if doc["text"] or doc["image_indexes"]}
        output, rejections = [], []
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            rejections.append({"reason": "missing_items_array"})
        for index, row in enumerate(raw["items"] if isinstance(raw, dict) and isinstance(raw.get("items"), list) else []):
            if not isinstance(row, dict):
                rejections.append({"index": index, "reason": "not_object"}); continue
            parent_id = row.get("parent_source_item_id")
            if not isinstance(parent_id, int) or parent_id not in allowed_source_ids:
                rejections.append({"index": index, "reason": "invalid_parent_source_item_id"}); continue
            if not str(row.get("name") or "").strip() or row.get("source_url") not in allowed_urls:
                rejections.append({"index": index, "reason": "invalid_name_or_source"}); continue
            confidence = _as_decimal(row.get("confidence"))
            requirements = row.get("requirements")
            if isinstance(requirements, dict) and requirements.get("compiled_reconciliation"):
                rejections.append({"index": index, "reason": "untrusted_compiled_reconciliation"}); continue
            relationship = str(row.get("relationship") or "").lower()
            supply_kind = str(row.get("supply_kind") or "").lower()
            structure_reason = str(row.get("structure_reason") or "").strip()
            if (supply_kind and (supply_kind not in {"finished", "kit", "unclear"} or not structure_reason)) or (relationship == "component" and (supply_kind != "kit" or not structure_reason)) or (relationship == "enrich" and supply_kind == "kit"):
                rejections.append({"index": index, "reason": "unsubstantiated_supply_structure"}); continue
            if relationship == "enrich" and (not isinstance(requirements, dict) or requirements.get("reconciled") is not True):
                rejections.append({"index": index, "reason": "missing_fact_reconciliation"}); continue
            invalid_fact_source = False
            if relationship == "enrich" and isinstance(requirements.get("characteristics"), list):
                for fact in requirements["characteristics"]:
                    if not isinstance(fact, dict):
                        continue
                    fact.pop("_source_provenance", None)
                    document_facts = fact.get("document_facts") or []
                    if not isinstance(document_facts, list):
                        invalid_fact_source = True; break
                    for document_fact in document_facts:
                        if not isinstance(document_fact, dict) or document_fact.get("source_url") not in allowed_urls:
                            invalid_fact_source = True; break
                        document_fact["_source_provenance"] = {"document_url": document_fact["source_url"], "page_or_section": str(document_fact.get("page_or_section") or "")[:300], "evidence": str(document_fact.get("evidence") or "")[:1000]}
                    if invalid_fact_source:
                        break
                    if (fact.get("resolution") == "new" and not document_facts) or fact.get("document_value") not in (None, "", [], {}):
                        if fact.get("source_url") not in allowed_urls:
                            invalid_fact_source = True; break
                        fact["_source_provenance"] = {"document_url": fact["source_url"], "page_or_section": str(fact.get("page_or_section") or "")[:300], "evidence": str(fact.get("evidence") or "")[:1000]}
            if invalid_fact_source:
                rejections.append({"index": index, "reason": "invalid_characteristic_source"}); continue
            per_parent = _as_decimal(row.get("quantity_per_parent")) if relationship == "component" else Decimal("1")
            if confidence is None or not Decimal("0") <= confidence <= Decimal("1") or not isinstance(requirements, dict) or relationship not in {"enrich", "split", "component"} or per_parent is None or per_parent <= 0 or (relationship == "component" and not str(row.get("unit") or "").strip()):
                rejections.append({"index": index, "reason": "invalid_structure"}); continue
            from tender_selection.national_regime import document_item
            original_parent = next((item for item in source_items if item.pk == parent_id), None)
            national_origin = notification_origin(original_parent) if original_parent else None
            notification_position = (national_origin.metadata or {}).get("notification_index") if national_origin else None
            national = document_item(row.get("national_regime"), {doc["url"]: doc for doc in available}, name=str(row["name"]), position=notification_position + 1 if type(notification_position) is int else None, component=relationship != "enrich")
            output.append(ExtractedItem(str(row["name"])[:1000], _as_decimal(row.get("quantity")), str(row.get("unit") or "")[:64], requirements, {"document_url": row["source_url"], "page_or_section": str(row.get("page_or_section") or "")[:300], "evidence": str(row.get("evidence") or "")[:1000], "extraction_model": self.model, "extraction_version": "tender-document-analysis-v2"}, confidence, relationship, per_parent, parent_id, str(row.get("parent_display_name") or "")[:300], supply_kind, structure_reason[:1000], national_regime=national))
        modes = {}
        for entry in output:
            modes.setdefault(entry.parent_source_item_id, set()).add(entry.relationship)
        if any(sum(entry.parent_source_item_id == parent_id and entry.relationship == "enrich" for entry in output) > 1 for parent_id in modes):
            rejections.append({"reason": "multiple_enrichments_for_one_item"})
        if any("component" in values and len(values) > 1 for values in modes.values()):
            rejections.append({"reason": "aggregate_and_components_mixed"})
        outcome = "validation_failure" if rejections else "document_reading_incomplete" if incomplete else "success" if output else "no_data"
        self.diagnostics = {"outcome": outcome, "model": self.model, "reasoning_effort": self.reasoning_effort, "thinking": self.thinking, "duration_ms": round((time.monotonic() - started) * 1000, 2), "cost_rub": str(self.cost_rub), "documents": {"considered": considered}, "budget": {"average_target_rub": str(self.DOCUMENT_AVERAGE_COST_TARGET_RUB), "estimated_cost_rub": str(estimated_cost), "estimated_usage": estimated_usage, "target_exceeded": self.cost_rub > self.DOCUMENT_AVERAGE_COST_TARGET_RUB}, "extraction": {"selected_urls": sorted(allowed_urls), "response_shape": response_shape, "accepted_count": len(output), "rejected_count": len(rejections), "rejections": rejections[:50], "usage": extract_usage}}
        return output

    def extract(self, *, tender, aggregate, documents):
        from .gateway_budget import spend_rub
        from .services import _ai_gateway_json
        started = time.monotonic()
        self.diagnostics = {}
        available = []
        for doc in documents:
            if not doc.get("url"):
                continue
            html = (DocumentPreview.objects.filter(url=doc["url"]).values_list("html", flat=True).first() or "")[:30000]
            available.append({"url": doc["url"], "name": doc.get("name", ""), "text": html, "tables": document_table_payload(html)})
        if not any(doc["text"] for doc in available):
            self.diagnostics = {"outcome": "system_extraction_failure", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [], "skipped_reason": "document_preview_missing"}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        triage_prompt = "Return JSON only {\"relevant_urls\":[string]}. Select only documents containing an item/specification/table. Documents:\n" + json.dumps(available, ensure_ascii=False)
        try:
            triage, triage_usage = _ai_gateway_json(triage_prompt, model=self.model, max_tokens=800, timeout=90, network_attempts=2)
        except Exception as exc:
            self._record_gateway_failure(exc, usage={}, phase="triage", started=started)
            raise
        urls = set(triage.get("relevant_urls", [])) if isinstance(triage, dict) else set()
        selected = [doc for doc in available if doc["url"] in urls and doc["text"]]
        if not selected:
            self.usage = triage_usage
            self.cost_rub = float(spend_rub(triage_usage, self.model) or 0)
            self.diagnostics = {"outcome": "no_data", "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "cost_rub": str(self.cost_rub), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [], "skipped_reason": "no_relevant_document_selected", "usage": triage_usage}, "extraction": {"accepted_count": 0, "rejected_count": 0, "rejections": []}}
            return []
        prompt = """Extract only explicitly evidenced tender items. Return JSON only {\"items\":[{\"name\":string,\"quantity\":number|null,\"unit\":string,\"requirements\":object,\"confidence\":number,\"source_url\":string,\"page_or_section\":string,\"evidence\":string}]}. Do not invent missing facts. Preserve all explicit requirements. Emit separate rows only for independently specified goods or variants, not for the mandatory delivered completeness of a finished product. A list of included parts, accessories, packaging or documents alone does not establish a multi-product kit. Keep unclear positions whole for human review and preserve completeness in their requirements. Do not copy a requirement that applies only to the aggregate into every child.\n""" + json.dumps({"aggregate": aggregate.original_text, "documents": selected}, ensure_ascii=False)
        try:
            raw, extract_usage = _ai_gateway_json(prompt, model=self.model, max_tokens=5000, timeout=120, network_attempts=2)
        except Exception as exc:
            self._record_gateway_failure(exc, usage=triage_usage, phase="extraction", started=started)
            raise
        self.usage = {key: (triage_usage.get(key, 0) or 0) + (extract_usage.get(key, 0) or 0) for key in {**triage_usage, **extract_usage}}
        self.cost_rub = float(spend_rub(self.usage, self.model) or 0)
        output, rejections = [], []
        allowed_urls = {doc["url"] for doc in selected}
        for index, row in enumerate(raw["items"] if isinstance(raw, dict) and isinstance(raw.get("items"), list) else []):
            if not isinstance(row, dict):
                rejections.append({"index": index, "reason": "not_object"})
                continue
            if not str(row.get("name") or "").strip():
                rejections.append({"index": index, "reason": "missing_name"})
                continue
            if row.get("source_url") not in allowed_urls:
                rejections.append({"index": index, "reason": "invalid_source_url"})
                continue
            confidence = _as_decimal(row.get("confidence"))
            if confidence is None or not (Decimal("0") <= confidence <= Decimal("1")):
                rejections.append({"index": index, "reason": "invalid_confidence"})
                continue
            requirements = row.get("requirements")
            if not isinstance(requirements, dict):
                rejections.append({"index": index, "reason": "invalid_requirements"})
                continue
            relationship = str(row.get("relationship") or "split").lower()
            if relationship not in {"enrich", "split", "component"}:
                rejections.append({"index": index, "reason": "invalid_relationship"})
                continue
            per_parent = _as_decimal(row.get("quantity_per_parent")) or Decimal("1")
            if per_parent <= 0:
                rejections.append({"index": index, "reason": "invalid_quantity_per_parent"})
                continue
            output.append(ExtractedItem(str(row["name"])[:1000], _as_decimal(row.get("quantity")), str(row.get("unit") or "")[:64], requirements, {"document_url": row["source_url"], "page_or_section": str(row.get("page_or_section") or "")[:300], "evidence": str(row.get("evidence") or "")[:1000], "extraction_model": self.model, "extraction_version": "document-enrichment-v1"}, confidence, relationship, per_parent))
        outcome = "success" if output else ("validation_failure" if rejections else "system_extraction_failure")
        self.diagnostics = {"outcome": outcome, "model": self.model, "duration_ms": round((time.monotonic() - started) * 1000, 2), "cost_rub": str(self.cost_rub), "triage": {"documents_considered": [{"url": doc["url"], "name": doc["name"], "cached_text_chars": len(doc["text"]), "table_count": len(doc["tables"])} for doc in available], "selected_urls": [doc["url"] for doc in selected], "usage": triage_usage}, "extraction": {"selected_urls": [doc["url"] for doc in selected], "raw_item_count": len(raw.get("items", [])) if isinstance(raw, dict) and isinstance(raw.get("items"), list) else 0, "accepted_count": len(output), "rejected_count": len(rejections), "rejections": rejections[:50], "accepted_summary": [{"name": entry.name, "quantity": str(entry.quantity or ""), "requirement_keys": sorted(entry.requirements), "document_url": entry.provenance["document_url"]} for entry in output], "usage": extract_usage}}
        return output


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().replace("ё", "е")).strip()


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def is_visible_incoming(tender: Tender, settings: FilterSettings | None = None) -> bool:
    settings = settings or FilterSettings.load()
    if tender.status != Tender.NEW or tender.review != Tender.UNREVIEWED:
        return False
    if settings.min_price and tender.max_price is not None and tender.max_price < settings.min_price:
        return False
    if tender.collecting_finished_at and tender.collecting_finished_at < timezone.now():
        return False
    return match_title(tender.title or tender.object_info, parse_terms(settings.include_words), parse_terms(settings.exclude_words))[0]


def trigger_visible_tender(tender: Tender, *, trigger: str = "incoming_visible", version: str = "v2") -> TenderComputeJob | None:
    """The only V2 intake boundary. It deliberately does not run a worker."""
    from django.conf import settings
    if not settings.CALCULATION_V2_ENABLED or not is_visible_incoming(tender):
        return None
    active = [TenderComputeJob.Status.QUEUED, TenderComputeJob.Status.RUNNING, TenderComputeJob.Status.PREPARING_INPUT, TenderComputeJob.Status.ROUTING, TenderComputeJob.Status.PREPARING]
    with transaction.atomic():
        Tender.objects.select_for_update().get(pk=tender.pk)
        existing = TenderComputeJob.objects.filter(tender=tender, version__startswith="v2", status__in=active).order_by("pk").first()
        return existing or TenderComputeJob.objects.create(tender=tender, trigger=trigger, version=version, diagnostics={"intake_refresh": True, "refresh": {"stage": "queued", "label": "Подготовка в очереди…"}})


def eligible_backfill_tenders(*, limit: int) -> list[Tender]:
    """Return unprocessed, currently eligible incoming tenders in stable newest-first order."""
    if limit < 1:
        return []
    prior_v2_job = TenderComputeJob.objects.filter(tender_id=OuterRef("pk"), version="v2")
    candidates = (
        Tender.objects.filter(status=Tender.NEW, review=Tender.UNREVIEWED)
        .annotate(has_v2_job=Exists(prior_v2_job))
        .filter(has_v2_job=False)
        .order_by("-published_at", "pk")
    )
    eligible = []
    for tender in candidates.iterator(chunk_size=100):
        if is_visible_incoming(tender):
            eligible.append(tender)
            if len(eligible) == limit:
                break
    return eligible


def queue_visible_backfill(*, limit: int) -> list[TenderComputeJob]:
    """Durably queue a bounded backfill without reprocessing any existing V2 job."""
    from django.conf import settings
    if not settings.CALCULATION_V2_ENABLED:
        return []
    jobs = []
    for candidate in eligible_backfill_tenders(limit=limit):
        with transaction.atomic():
            tender = Tender.objects.select_for_update().get(pk=candidate.pk)
            if TenderComputeJob.objects.filter(tender=tender, version="v2").exists() or not is_visible_incoming(tender):
                continue
            jobs.append(TenderComputeJob.objects.create(tender=tender, trigger="incoming_backfill", version="v2"))
    return jobs


def eligible_existing_recheck_tenders(*, limit: int) -> list[Tender]:
    """Opt-in future recheck set: current Incoming plus Kanban, never dismissed/archived."""
    from django.conf import settings
    if limit < 1 or not settings.CALCULATION_V2_EXISTING_RECHECK_ENABLED:
        return []
    already_queued = TenderComputeJob.objects.filter(tender_id=OuterRef("pk"), version="v2-existing-recheck")
    return list(
        Tender.objects.filter(status__in=[Tender.NEW, Tender.PUSHED], archived_at__isnull=True)
        .annotate(has_recheck=Exists(already_queued)).filter(has_recheck=False)
        .order_by("-published_at", "pk")[:limit]
    )


def queue_existing_recheck(*, limit: int) -> list[TenderComputeJob]:
    from django.conf import settings
    if not settings.CALCULATION_V2_EXISTING_RECHECK_ENABLED:
        return []
    return [
        TenderComputeJob.objects.get_or_create(
            tender=tender, version="v2-existing-recheck",
            defaults={"trigger": "owner_enabled_existing_recheck"},
        )[0]
        for tender in eligible_existing_recheck_tenders(limit=limit)
    ]


def _notification_rows(tender: Tender) -> list[dict]:
    if not tender.notification_raw:
        return []
    return parse_notification(tender.notification_raw).get("items", [])


def _source_key(row: dict, index: int) -> str:
    code = str(row.get("code") or "").strip()
    if code:
        return f"notification:code:{code}:{index}"
    identity = {"name": _normal(str(row.get("name") or "")), "quantity": str(row.get("quantity") or ""), "unit": str(row.get("unit") or "")}
    return f"notification:item:{_fingerprint(identity)[:20]}:{index}"


def _as_decimal(value) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", "."))
        return number if number.is_finite() else None
    except Exception:
        return None


def ingest_source_items(tender: Tender) -> list[TenderSourceItem]:
    """Append-only-ish notification ingestion; changed rows supersede, never overwrite source truth."""
    result = []
    for index, row in enumerate(_notification_rows(tender)):
        name = str(row.get("name") or "").strip()
        requirements = {"characteristics": row.get("characteristics") or []}
        payload = {"name": name, "quantity": row.get("quantity"), "unit": row.get("unit"), "requirements": requirements, "code": row.get("code") or ""}
        source_key = _source_key(row, index)
        fingerprint = _fingerprint(payload)
        current = TenderSourceItem.objects.filter(tender=tender, source_key__startswith=source_key, is_active=True).order_by("-pk").first()
        if current and current.metadata.get("source_fingerprint") == fingerprint:
            result.append(current)
            continue
        if current:
            current.is_active = False
            current.save(update_fields=["is_active"])
        key = source_key if current is None else f"{source_key}:v:{fingerprint[:12]}"
        item, _ = TenderSourceItem.objects.get_or_create(
            tender=tender, source_key=key,
            defaults={
                "source_type": "notification", "original_text": name, "quantity": _as_decimal(row.get("quantity")),
                "unit": str(row.get("unit") or ""), "requirements": requirements,
                "metadata": {"source_fingerprint": fingerprint, "notification_index": index, "code": row.get("code") or ""},
                "provenance": {"kind": "notification", "notification_index": index}, "supersedes": current,
            },
        )
        result.append(item)
    return result


def _diagnostic_document(document: dict) -> dict:
    value = document.get("date")
    return {
        "url": str(document.get("url") or ""),
        "name": str(document.get("name") or "Документ"),
        "kind": str(document.get("kind") or ""),
        "size_kb": document.get("size_kb"),
        "index": document.get("index"),
        "date": value.isoformat() if hasattr(value, "isoformat") else value,
    }


def assess_quality(tender: Tender, items: list[TenderSourceItem]) -> dict:
    reasons = []
    documents = [_diagnostic_document(document) for document in parse_notification(tender.notification_raw).get("documents", [])] if tender.notification_raw else []
    if not items:
        reasons.append("no_source_items")
    if len(items) == 1:
        item = items[0]
        text = _normal(item.original_text)
        if len(text) < 40:
            reasons.append("single_sparse_item")
        if item.quantity == 1 and len(text.split()) <= 6:
            reasons.append("single_aggregate_item")
        if _normal(tender.title or tender.object_info) == text:
            reasons.append("item_repeats_tender_subject")
        if not item.requirements.get("characteristics"):
            reasons.append("requirements_missing")
    if tender.max_price and tender.max_price >= Decimal("100000") and sum(len(item.original_text) for item in items) < 80:
        reasons.append("high_value_low_detail")
    useful_documents = [doc for doc in documents if doc.get("url")]
    if useful_documents:
        reasons.append("documents_available")
    score = min(1, Decimal("0.20") * len(reasons))
    return {"status": "suspicious" if reasons else "sufficient", "reasons": reasons, "confidence": str(Decimal("1.0000") - score), "enrichment_recommended": bool(useful_documents), "documents": useful_documents}


def active_calculation_items(tender: Tender) -> list[TenderSourceItem]:
    """Canonical deterministic active set: active derived rows replace their aggregate parent."""
    items = list(tender.v2_source_items.filter(is_active=True).order_by("source_key", "pk"))
    derived_parents = {item.parent_id for item in items if item.parent_id and item.source_type in {"document_enrichment", "document_extraction"}}
    return [item for item in items if item.pk not in derived_parents and item.source_type != "document_component"]


def _characteristic_values(requirements: dict) -> dict[str, str]:
    values = {}
    for entry in requirements.get("characteristics", []) if isinstance(requirements, dict) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("characteristicName") or "").strip()
        value = str(entry.get("value") or entry.get("characteristicValue") or "").strip()
        if name and value:
            values[_normal(name)] = value
    return values


def _enriched_requirements(source: TenderSourceItem, extracted: ExtractedItem) -> tuple[dict, dict, bool]:
    """Keep every notification fact; add only genuinely new document evidence."""
    base = source.requirements if isinstance(source.requirements, dict) else {}
    characteristics = []
    existing = set()
    for raw in base.get("characteristics") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("characteristicName") or "").strip()
        value = raw.get("value") if raw.get("value") not in (None, "") else raw.get("characteristicValue")
        if not name or value in (None, "", [], {}):
            continue
        row = {"name": name, "value": value, "source": raw.get("source") or "notification"}
        characteristics.append(row)
        existing.add((_normal(name), _normal(str(value))))

    field_provenance = {}
    changed = False
    for raw_name, value in extracted.requirements.items():
        if value in (None, "", [], {}):
            continue
        name = str(raw_name).strip()
        key = (_normal(name), _normal(str(value)))
        if key in existing:
            continue
        characteristics.append({"name": name, "value": value, "source": "document"})
        field_provenance[name] = {
            "value": value,
            "source": dict(extracted.provenance),
            "confidence": str(extracted.confidence or ""),
            "extraction_version": extracted.provenance.get("extraction_version", ""),
        }
        existing.add(key)
        changed = True
    if not changed:
        return base, {}, False
    return {"characteristics": characteristics, "document_requirements": extracted.requirements}, field_provenance, True

def _match_extracted_item(entry: ExtractedItem, candidates: list[TenderSourceItem]) -> TenderSourceItem | None:
    entry_name = _normal(entry.name)
    entry_values = {_normal(str(key)): _normal(str(value)) for key, value in entry.requirements.items() if value not in (None, "", [], {})}
    scored = []
    for source in candidates:
        score = 4 if _normal(source.original_text) == entry_name else 0
        if entry.quantity is not None and source.quantity == entry.quantity:
            score += 1
        source_values = {_normal(key): _normal(value) for key, value in _characteristic_values(source.requirements).items()}
        score += sum(1 for key, value in entry_values.items() if source_values.get(key) == value)
        scored.append((score, source.pk, source))
    if not scored:
        return None
    score, _, source = max(scored)
    return source if score >= 4 else None


def document_analysis_sources(tender):
    replacement_groups, analysis_sources = {}, {}
    for item in active_calculation_items(tender):
        if item.metadata.get("relationship") == "component" and item.parent_id and not item.parent.is_active:
            replacement_groups.setdefault(item.parent_id, []).append(item.pk)
            analysis_sources[item.parent_id] = item.parent
        else:
            analysis_sources[item.pk] = item
    return analysis_sources, replacement_groups


def enrich_suspicious_tender(tender: Tender, quality: dict, enricher: DocumentEnricher) -> dict:
    """Document evidence can split or enrich a source item, never overwrite it."""
    if not quality["enrichment_recommended"]:
        return {"created": [], "state": "no_data", "diagnostics": {"outcome": "no_data", "reason": "not_recommended"}}
    analysis_sources, replacement_groups = document_analysis_sources(tender)
    active = active_calculation_items(tender)
    extract_tender = getattr(enricher, "extract_tender", None)
    if callable(extract_tender):
        extracted = extract_tender(tender=tender, source_items=list(analysis_sources.values()), documents=quality["documents"])
        detail = getattr(enricher, "diagnostics", {}) or {}
        outcome = detail.get("outcome")
        if outcome in {"system_extraction_failure", "validation_failure", "budget_blocked", "budget_exceeded", "unpriced_model"}:
            return {"created": [], "state": outcome, "diagnostics": [detail]}
        parents = analysis_sources
        proposals = []
        prepared = []
        for entry in extracted:
            parent = parents.get(entry.parent_source_item_id)
            if parent is None:
                continue
            if entry.relationship == "component":
                entry = replace(entry, quantity=component_total_quantity(parent.quantity, entry.quantity_per_parent))
            elif entry.relationship == "enrich":
                try:
                    combined = merge_item_requirements(effective_item_requirements(parent), entry.requirements, provenance=entry.provenance)
                except ValueError as exc:
                    detail.update(outcome="validation_failure", final_failure_reason=f"{entry.name}: {exc}")
                    return {"created": [], "state": "validation_failure", "diagnostics": [detail]}
                entry = replace(entry, quantity=entry.quantity if entry.quantity is not None else parent.quantity, unit=entry.unit or parent.unit, requirements=combined)
            prepared.append((entry, parent))
        with transaction.atomic():
            for entry, parent in prepared:
                payload = {"parent": parent.pk, "parent_display_name": entry.parent_display_name, "name": entry.name, "quantity": str(entry.quantity or ""), "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance, "relationship": entry.relationship, "quantity_per_parent": str(entry.quantity_per_parent), "replaces_source_ids": sorted(replacement_groups.get(parent.pk, [])), "supply_kind": entry.supply_kind, "structure_reason": entry.structure_reason, "national_regime": entry.national_regime}
                from .document_enrichment import document_proposal_changes
                candidate = TenderSourceItem(source_type="document_proposal", parent=parent, provenance=entry.provenance, original_text=entry.name, quantity=entry.quantity, unit=entry.unit, requirements=entry.requirements, metadata=payload)
                if not document_proposal_changes(candidate):
                    continue
                proposal, _ = TenderSourceItem.objects.get_or_create(
                    tender=tender,
                    source_key=f"proposal:{parent.pk}:{_fingerprint(payload)[:20]}",
                    defaults={"source_type": "document_proposal", "original_text": entry.name, "quantity": entry.quantity,
                              "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance,
                              "metadata": {"relationship": entry.relationship, "parent_display_name": entry.parent_display_name,
                                       "supply_kind": entry.supply_kind, "structure_reason": entry.structure_reason,
                                       "national_regime": entry.national_regime,
                                       "replaces_source_ids": sorted(replacement_groups.get(parent.pk, [])),
                                           **({"quantity_per_parent": str(entry.quantity_per_parent)} if entry.relationship == "component" else {})},
                              "confidence": entry.confidence, "parent": parent, "is_active": False},
                )
                proposals.append(proposal)
        return {"created": proposals, "state": "needs_review" if proposals else "document_reading_incomplete" if outcome == "document_reading_incomplete" else ("no_change" if prepared else "no_data"), "diagnostics": [detail]}
    else:
        extracted_groups = None
    groups = {}
    if extracted_groups is None:
        for item in active:
            groups.setdefault(_normal(item.original_text), []).append(item)
    created, diagnostics, extracted_any = [], [], False
    for group, extracted in extracted_groups or ((group, None) for group in groups.values()):
        if extracted is None:
            extracted = enricher.extract(tender=tender, aggregate=group[0], documents=quality["documents"])
        detail = getattr(enricher, "diagnostics", {}) or {}
        diagnostics.append(detail)
        outcome = detail.get("outcome")
        if outcome in {"system_extraction_failure", "validation_failure", "budget_blocked", "budget_exceeded", "unpriced_model"}:
            return {"created": created, "state": outcome, "diagnostics": diagnostics}
        if not extracted:
            continue
        extracted_any = True
        remaining = list(group)
        for entry in extracted:
            matched = _match_extracted_item(entry, remaining)
            if matched:
                requirements, field_provenance, requirements_changed = _enriched_requirements(matched, entry)
                quantity_changed = entry.quantity is not None and entry.quantity != matched.quantity
                unit_changed = bool(entry.unit and entry.unit != matched.unit)
                if not (requirements_changed or quantity_changed or unit_changed):
                    continue
                payload = {"parent": matched.pk, "requirements": requirements, "quantity": str(entry.quantity or matched.quantity or ""), "unit": entry.unit or matched.unit, "provenance": entry.provenance}
                item, _ = TenderSourceItem.objects.get_or_create(
                    tender=tender, source_key=f"enriched:{matched.pk}:{_fingerprint(payload)[:20]}",
                    defaults={"source_type": "document_enrichment", "original_text": matched.original_text,
                              "quantity": entry.quantity if entry.quantity is not None else matched.quantity,
                              "unit": entry.unit or matched.unit, "requirements": requirements,
                              "metadata": {"field_provenance": field_provenance},
                              "provenance": entry.provenance, "confidence": entry.confidence, "parent": matched},
                )
                matched.is_active = False
                matched.save(update_fields=["is_active"])
                remaining.remove(matched)
                created.append(item)
                continue
            payload = {"name": entry.name, "quantity": str(entry.quantity), "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance}
            parent = group[0]
            is_component = entry.relationship == "component"
            item, _ = TenderSourceItem.objects.get_or_create(
                tender=tender, source_key=f"{'component' if is_component else 'derived'}:{parent.pk}:{_fingerprint(payload)[:20]}",
                defaults={"source_type": "document_component" if is_component else "document_extraction", "original_text": entry.name, "quantity": entry.quantity,
                          "unit": entry.unit, "requirements": entry.requirements, "provenance": entry.provenance,
                          "metadata": {"quantity_per_parent": str(entry.quantity_per_parent)},
                          "confidence": entry.confidence, "parent": parent},
            )
            if not is_component:
                parent.is_active = False
                parent.save(update_fields=["is_active"])
            created.append(item)
    state = "success" if created else ("no_change" if extracted_any else "no_data")
    return {"created": created, "state": state, "diagnostics": diagnostics}

def _knowledge_for(items: list[TenderSourceItem]) -> list[dict]:
    knowledge = []
    for lesson in Lesson.objects.filter(scope__in=["route", "production_step"], is_active=True).only("item_word", "outcome").order_by("pk"):
        route = lesson.outcome.get("route") if isinstance(lesson.outcome, dict) else {}
        ids = [int(step["process_id"]) for step in route.get("processes", []) if str(step.get("process_id", "")).isdigit()]
        if lesson.item_word and ids:
            knowledge.append({"needle": _normal(lesson.item_word), "process_ids": ids})
    for record in KnowledgeRecord.objects.filter(status="active").only("scope_type", "scope_context", "payload", "applicability").order_by("pk"):
        needle = _normal(str(record.applicability.get("item_text", "")))
        process_ids = [value for value in record.payload.get("process_ids", []) if isinstance(value, int)]
        if needle and process_ids:
            knowledge.append({"needle": needle, "process_ids": process_ids})
    return knowledge


def _commercial_requirements(source: TenderSourceItem) -> dict:
    from .document_enrichment import effective_item_requirements
    return effective_item_requirements(source)


def routing_source_items(tender):
    """Plan complete proposed goods without accepting changes or creating an estimate."""
    from .document_enrichment import document_proposal_changes
    active = active_calculation_items(tender)
    proposals = [item for item in tender.v2_source_items.filter(source_type="document_proposal", is_active=False).select_related("parent").order_by("parent_id", "pk") if not item.metadata.get("analysis_incomplete") and document_proposal_changes(item)]
    replaced = {item.parent_id for item in proposals}
    replaced.update(source_id for item in proposals for source_id in item.metadata.get("replaces_source_ids", []))
    return [item for item in active if item.pk not in replaced] + proposals


def build_commercial_items(job: TenderComputeJob) -> list[TenderCommercialItem]:
    """Turn immutable source evidence into commercial items and calculation components."""
    tender = job.tender
    active = routing_source_items(tender)
    result = []
    for source in active:
        component_sources = list(source.derived_items.filter(is_active=True, source_type="document_component").order_by("source_key", "pk"))
        structure = TenderCommercialItem.Structure.COMPOSITE if component_sources else TenderCommercialItem.Structure.SIMPLE
        commercial, _ = TenderCommercialItem.objects.update_or_create(
            tender=tender, source_key=f"commercial:{source.pk}",
            defaults={"job": job, "display_name": source.original_text, "quantity": source.quantity, "unit": source.unit,
                      "requirements": _commercial_requirements(source), "provenance": {"source_item_ids": [source.pk], "draft": not source.is_active},
                      "structure": structure, "status": "active"},
        )
        commercial.source_items.set([source])
        component_rows = component_sources or [source]
        for index, component_source in enumerate(component_rows):
            per_parent = _as_decimal((component_source.metadata or {}).get("quantity_per_parent")) or Decimal("1")
            CalculationComponent.objects.update_or_create(
                commercial_item=commercial, source_item=component_source,
                defaults={"name": component_source.original_text, "quantity_per_parent": per_parent, "unit": component_source.unit,
                          "requirements": _commercial_requirements(component_source), "provenance": component_source.provenance,
                          "status": "active", "sort_order": index},
            )
        result.append(commercial)
    return result


def _component_conflicts(commercial: TenderCommercialItem) -> list[str]:
    values: dict[str, set[str]] = {}
    for row in (_commercial_requirements(commercial.source_items.order_by("pk").first()).get("characteristics") or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("characteristicName") or "").strip()
        value = row.get("value") if row.get("value") not in (None, "") else row.get("characteristicValue")
        if name and value not in (None, "", [], {}):
            values.setdefault(_normal(name), set()).add(_normal(str(value)))
    return [name for name, choices in values.items() if len(choices) > 1]


QUESTION_KNOWN = "known"
QUESTION_MISSING = "missing"
QUESTION_CONFLICTING = "conflicting"


def _requirement_facts(requirements: object) -> list[tuple[str, str]]:
    if not isinstance(requirements, dict):
        return []
    facts = []
    for answer in requirements.get("owner_answers", []):
        if isinstance(answer, dict) and len(answer.get("names", [])) == 1 and str(answer.get("answer") or "").strip():
            facts.append((str(answer["names"][0]), str(answer["answer"])))
    for entry in requirements.get("characteristics") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("characteristicName") or "").strip()
        value = entry.get("value") if entry.get("value") not in (None, "") else entry.get("characteristicValue")
        if name and value not in (None, "", [], {}):
            facts.append((name, str(value)))
    for name, value in requirements.items():
        if name in {"characteristics", "document_requirements"} or value in (None, "", [], {}):
            continue
        if isinstance(value, (str, int, float, Decimal)):
            facts.append((str(name), str(value)))
    if isinstance(requirements.get("document_requirements"), dict):
        facts.extend(_requirement_facts(requirements["document_requirements"]))
    return facts


def _labels_match(left: str, right: str) -> bool:
    left, right = _normal(left), _normal(right)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_words = set(re.findall(r"[\wа-я]+", left))
    right_words = set(re.findall(r"[\wа-я]+", right))
    return bool(left_words) and left_words <= right_words


def _question_evidence_state(item: CalculationComponent, decision: RouteDecision, conflicts: list[str]) -> tuple[str, list[str]]:
    if conflicts:
        return QUESTION_CONFLICTING, conflicts
    local_facts = _requirement_facts(item.requirements) + _requirement_facts(item.commercial_item.requirements)
    if item.source_item_id:
        local_facts.extend(_requirement_facts(item.source_item.requirements))
    local_values: dict[str, set[str]] = {}
    for label, value in local_facts:
        local_values.setdefault(_normal(label), set()).add(_normal(value))
    if any(len(values) > 1 for values in local_values.values()):
        return QUESTION_CONFLICTING, []
    facts = list(local_facts)
    facts.append(("quantity", str(item.effective_quantity)))
    facts.append(("количество", str(item.effective_quantity)))
    for sibling in CalculationComponent.objects.filter(commercial_item__tender=item.commercial_item.tender, status="active").select_related("source_item"):
        facts.extend(_requirement_facts(sibling.requirements))
        if sibling.source_item_id:
            facts.extend(_requirement_facts(sibling.source_item.requirements))
    for source in item.commercial_item.tender.v2_source_items.filter(is_active=True):
        facts.extend(_requirement_facts(source.requirements))
    for payload in OwnerFeedbackEvent.objects.filter(interaction__tender=item.commercial_item.tender).values_list("payload", flat=True):
        if isinstance(payload, dict):
            facts.extend(_requirement_facts(payload.get("requirements") or payload.get("answers") or {}))
    for record in KnowledgeRecord.objects.filter(status="active", scope_type__in=["current_tender", "tender"]).only("scope_context", "payload"):
        if str(record.scope_context.get("tender_id", "")) == str(item.commercial_item.tender_id):
            facts.extend(_requirement_facts(record.payload.get("requirements") if isinstance(record.payload, dict) else {}))
    claimed = list(decision.missing_requirement_names)
    if not claimed:
        question = _normal(decision.question)
        claimed = [label for label, _ in facts if _normal(label) in question]
    if claimed and all(any(_labels_match(label, known_label) for known_label, _ in facts) for label in claimed):
        return QUESTION_KNOWN, claimed
    return QUESTION_MISSING, claimed


def route_tender_batch(job: TenderComputeJob, router: BatchRouter) -> list[RouteDecision]:
    commercials = build_commercial_items(job)
    items = list(CalculationComponent.objects.filter(commercial_item__in=commercials, status="active").select_related("commercial_item", "source_item").order_by("commercial_item_id", "sort_order", "pk"))
    answers = _route_owner_answer_map([item.source_item for item in items])
    for item in items:
        item.requirements = {**item.requirements, "owner_answers": answers[item.source_item_id]}
    processes = list(ProcessDefinition.objects.filter(is_active=True).order_by("pk"))
    knowledge = _knowledge_for([])
    fingerprints = {item.pk: source_route_fingerprint(item.source_item, processes, knowledge, answers[item.source_item_id]) for item in items}
    plans = {plan.component_id: plan for plan in ComponentRoutePlan.objects.filter(component__in=items, scope="component").prefetch_related("steps")}
    cached, pending = [], []
    for item in items:
        plan = plans.get(item.pk)
        if plan and plan.metadata.get("input_fingerprint") == fingerprints[item.pk] and plan.status in {"planned", "ready", "needs_review"}:
            data = plan.metadata
            cached.append(RouteDecision(item.pk, tuple(step.process_id for step in sorted(plan.steps.all(), key=lambda step: step.position)),
                _as_decimal(data.get("confidence")), {"reason": data.get("reason", ""), "alternatives": data.get("alternatives", [])},
                plan.status == "needs_review", data.get("question", ""), data.get("question_kind", "unspecified"), tuple(data.get("missing_requirement_names", []))))
        else:
            pending.append(item)
    if pending and processes:
        fresh = router.route(tender=job.tender, items=pending, processes=processes, knowledge=knowledge)
    else:
        fresh = [RouteDecision(item.pk, (), Decimal("0"), {"reason": "Нет активных этапов производства"}, True, "Добавьте доступные этапы в базу производства.") for item in pending]
    if len(fresh) != len(pending) or {decision.component_id for decision in fresh} != {item.pk for item in pending}:
        raise ValueError("Batch router must return exactly one decision per calculation component")
    with transaction.atomic():
        Tender.objects.select_for_update().get(pk=job.tender_id)
        current_processes = list(ProcessDefinition.objects.filter(is_active=True).order_by("pk"))
        current_knowledge = _knowledge_for([])
        current_sources = {source.pk: source for source in routing_source_items(job.tender)}
        current_answers = _route_owner_answer_map(current_sources.values())
        for item in items:
            source = current_sources.get(item.source_item_id)
            if source is None or source_route_fingerprint(source, current_processes, current_knowledge, current_answers[source.pk]) != fingerprints[item.pk]:
                raise ValueError("ТЗ или этапы изменились во время построения; обновите маршруты.")
        job.diagnostics.update(routing_reused_count=len(cached), routing_batch_count=1 if pending and processes else 0)
        decisions = cached + fresh
        cached_ids = {decision.component_id for decision in cached}
        by_item = {decision.component_id: decision for decision in decisions}
        if set(by_item) != {item.pk for item in items}:
            raise ValueError("Batch router must return exactly one decision per calculation component")
        effective_decisions = []
        OwnerInteraction.objects.filter(tender=job.tender, source_item__is_active=False, compute_line__isnull=False, status="open").update(status="superseded")
        for item in items:
            decision = by_item[item.pk]
            conflicts = _component_conflicts(item.commercial_item)
            evidence_state, evidence_labels = _question_evidence_state(item, decision, conflicts)
            needs_review = decision.needs_review or bool(conflicts)
            if needs_review and decision.process_ids and evidence_state == QUESTION_KNOWN:
                needs_review = False
            required_inputs = []
            for process in (process for process in processes if process.pk in decision.process_ids):
                required_inputs.extend(
                    str(value).strip() for value in (process.parameters or {}).get("required", [])
                    if str(value).strip()
                )
            missing_inputs = [
                required for required in required_inputs
                if _question_evidence_state(
                    item,
                    replace(decision, missing_requirement_names=(required,)),
                    conflicts,
                )[0] != QUESTION_KNOWN
            ]
            if missing_inputs:
                needs_review = True
                decision = replace(
                    decision,
                    question=f"Нужны данные для выбранного способа выполнения: {', '.join(missing_inputs)}.",
                    question_kind="missing_information",
                    missing_requirement_names=tuple(missing_inputs),
                )
            effective = replace(decision, needs_review=needs_review)
            line, _ = TenderComputeLine.objects.update_or_create(
                job=job, source_item=item.source_item, component=item,
                defaults={"commercial_item": item.commercial_item, "route_key": ",".join(map(str, effective.process_ids)), "route_confidence": effective.confidence,
                          "route_metadata": effective.rationale, "status": "needs_review" if effective.needs_review else "routed",
                          "input_snapshot": {"name": item.name, "quantity": str(item.effective_quantity), "requirements": item.requirements, "input_fingerprint": fingerprints[item.pk]}},
            )
            plan, _ = ComponentRoutePlan.objects.update_or_create(
                commercial_item=item.commercial_item, component=item, scope=ComponentRoutePlan.Scope.COMPONENT,
                defaults={"status": "needs_review" if effective.needs_review else "planned", "metadata": {**effective.rationale, "input_fingerprint": fingerprints[item.pk], "confidence": str(effective.confidence or 0), "question": effective.question, "question_kind": effective.question_kind, "missing_requirement_names": list(effective.missing_requirement_names)}},
            )
            if item.pk not in cached_ids:
                plan.steps.all().delete()
                for position, process_id in enumerate(effective.process_ids, start=1):
                    ComponentOperationStep.objects.create(route_plan=plan, process_id=process_id, position=position)
            question = effective.question
            if conflicts:
                question = f"Уточните противоречивые характеристики: {', '.join(conflicts)}."
            if effective.needs_review and not OwnerInteraction.objects.filter(tender=job.tender, source_item=item.source_item, context__component_id=item.pk, status="open").exists():
                OwnerInteraction.objects.create(tender=job.tender, source_item=item.source_item, compute_line=line, question=question, reason=effective.rationale.get("reason", ""), confidence=effective.confidence,
                                                context={"commercial_item_id": item.commercial_item_id, "component_id": item.pk, "conflicts": conflicts, "evidence_state": evidence_state, "evidence_labels": evidence_labels, "question_kind": effective.question_kind, "missing_requirement_names": list(effective.missing_requirement_names)})
            if not effective.needs_review:
                OwnerInteraction.objects.filter(tender=job.tender, source_item=item.source_item, context__component_id=item.pk, status="open").update(status="superseded")
            effective_decisions.append(effective)
        return effective_decisions


def run_next_tender_understanding_job(*, router: BatchRouter | None = None, enricher: DocumentEnricher | None = None) -> TenderComputeJob | None:
    job = claim_next_job()
    if job is None:
        return None
    from tender_selection.services import _run_document_analysis
    return _run_document_analysis(job.pk, router=router, enricher=enricher)


def source_route_snapshots(tender, sources=None):
    """Current accepted-position routes, shared by card, estimate and assistant; SELECT only."""
    sources = list(routing_source_items(tender) if sources is None else sources)
    processes = list(ProcessDefinition.objects.filter(is_active=True).order_by("pk"))
    knowledge = _knowledge_for([])
    plans = {}
    for plan in ComponentRoutePlan.objects.filter(commercial_item__tender=tender, component__source_item_id__in=[item.pk for item in sources], component__status="active", commercial_item__status="active", scope="component").select_related("component").prefetch_related("steps__process").order_by("-updated_at", "-pk"):
        plans.setdefault(plan.component.source_item_id, plan)
    job = TenderComputeJob.objects.filter(tender=tender, version__startswith="v2").order_by("-pk").first()
    active = bool(job and job.status in {"queued", "preparing_input", "running", "routing", "preparing"})
    from .document_enrichment import document_proposal_changes
    pending_parents = {item.parent_id for item in TenderSourceItem.objects.filter(tender=tender, source_type="document_proposal", is_active=False).select_related("parent") if document_proposal_changes(item)}
    answers = _route_owner_answer_map(sources)
    result = {}
    for source in sources:
        plan = plans.get(source.pk)
        current = bool(plan and plan.status in {"planned", "ready", "needs_review"} and (plan.status == "needs_review" or plan.steps.all()) and plan.metadata.get("input_fingerprint") == source_route_fingerprint(source, processes, knowledge, answers[source.pk]))
        state = ("needs_review" if plan.status == "needs_review" else "ready") if current else "building" if active else "stale" if plan else "pending"
        steps = [{"id": step.pk, "process_id": step.process_id, "name": step.process.name, "description": step.process.description, "details": step.details, "position": step.position} for step in sorted(plan.steps.all(), key=lambda step: step.position)] if plan else []
        result[source.pk] = {"source_item_id": source.pk, "tender_id": tender.pk, "name": source.original_text, "state": state,
            "plan_id": plan.pk if plan else None, "steps": steps, "pending_changes": source.pk in pending_parents, "reason": plan.metadata.get("reason", "") if plan else "",
            "question": plan.metadata.get("question", "") if current and state == "needs_review" else "",
            "label": f"Маршрут · {len(steps)} {'этап' if len(steps) % 10 == 1 and len(steps) % 100 != 11 else 'этапа' if 2 <= len(steps) % 10 <= 4 and not 12 <= len(steps) % 100 <= 14 else 'этапов'}" if state == "ready" else {"needs_review": "Маршрут · нужно уточнение", "building": "Маршрут строится…", "stale": "Маршрут устарел", "pending": "Маршрут не построен"}[state]}
    return result
