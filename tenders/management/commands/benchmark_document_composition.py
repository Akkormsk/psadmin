"""Controlled, opt-in comparison of document-composition models.

The command is deliberately read-only: it loads cached documents, sends a
bounded snapshot to the configured AI Gateway and prints a compact comparison.
It never writes tender records, proposals, routes or cache entries.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from tender_selection.notification import parse_notification
from tenders.document_enrichment import document_table_payload
from tenders.gateway_budget import spend_rub
from tenders.services import _ai_gateway_json


DEFAULT_MODELS = ("gemini/gemini-3.1-flash-lite", "openai/gpt-4.1-mini")
MAX_TOTAL_COST_RUB = 20.00
MAX_DOCUMENT_CHARS = 18_000
MAX_OUTPUT_TOKENS = 1_200


def _load_snapshot(tender_id: int) -> tuple[str, list[dict], list[dict]]:
    """Read only fields that exist in both the legacy local DB and V2."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select title, notification_raw from tender_selection_tender where id = %s",
            [tender_id],
        )
        row = cursor.fetchone()
    if not row:
        raise CommandError(f"Тендер {tender_id} не найден.")
    title, raw = row
    raw = json.loads(raw) if isinstance(raw, str) else raw
    parsed = parse_notification(raw or {})
    source_items = [
        {
            "id": index,
            "name": item.get("name", ""),
            "quantity": str(item.get("quantity") or ""),
            "unit": item.get("unit", ""),
            "requirements": item.get("requirements") or {},
        }
        for index, item in enumerate(parsed.get("items", []), start=1)
    ]
    document_refs = [item for item in parsed.get("documents", []) if item.get("url")]
    if not source_items:
        raise CommandError("В извещении нет исходных позиций.")
    if not document_refs:
        raise CommandError("В извещении нет документов.")

    documents = []
    for ref in document_refs:
        with connection.cursor() as cursor:
            cursor.execute(
                "select filename, html from tender_selection_documentpreview where url = %s",
                [ref["url"]],
            )
            cached = cursor.fetchone()
        if cached and cached[1]:
            documents.append({"url": ref["url"], "name": cached[0] or ref.get("name", ""), "html": cached[1]})
    if not documents:
        raise CommandError("Для документов извещения нет сохранённого текста: тест не будет отправлять пустые данные.")
    return title or "", source_items, documents


def _load_fixture(path: str) -> tuple[str, list[dict], list[dict]]:
    """Load a reviewed local benchmark case; never reads a tender or writes data."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read benchmark fixture: {exc}") from exc
    items = payload.get("notification_items") if isinstance(payload, dict) else None
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(documents, list) or not documents:
        raise CommandError("Fixture needs non-empty notification_items and documents arrays.")
    normalized_items = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise CommandError(f"Fixture item {index} is invalid.")
        normalized_items.append({
            "id": item.get("id", index), "name": item["name"], "quantity": str(item.get("quantity") or ""),
            "unit": item.get("unit", ""), "requirements": item.get("requirements") or {},
        })
    normalized_documents = []
    for index, item in enumerate(documents, start=1):
        if not isinstance(item, dict) or not item.get("url") or not isinstance(item.get("html"), str):
            raise CommandError(f"Fixture document {index} is invalid.")
        normalized_documents.append({"url": item["url"], "name": item.get("name", ""), "html": item["html"]})
    return str(payload.get("title") or ""), normalized_items, normalized_documents


def _bounded_documents(documents: list[dict]) -> list[dict]:
    """Equal share to every available document, without filename filtering."""
    per_document = max(1, MAX_DOCUMENT_CHARS // len(documents))
    payload = []
    for item in documents:
        excerpt = item["html"][:per_document]
        row = {"url": item["url"], "name": item["name"], "text": excerpt}
        tables = document_table_payload(excerpt) if excerpt else []
        if len(json.dumps({**row, "tables": tables}, ensure_ascii=False)) <= per_document * 2:
            row["tables"] = tables
        payload.append(row)
    return payload


def _prompt(title: str, items: list[dict], documents: list[dict]) -> str:
    instructions = (
        'Read every supplied tender document and compare it with every notification item. '
        'Return JSON only {"items":[{"parent_source_item_id":integer,"name":string,'
        '"quantity":number|null,"unit":string,"requirements":object,"confidence":number,'
        '"source_url":string,"page_or_section":string,"evidence":string,'
        '"relationship":"enrich|split|component","quantity_per_parent":number}]}. '
        'The notification is the baseline. Return enrich only for explicitly documented facts about '
        'that same notification item. Return split only where a document explicitly replaces an '
        'aggregate item with separately named goods. Return component only where contents of an '
        'aggregate are explicitly enumerated. Do not use document names as evidence. Never infer '
        'products, omit a supplied document because of its title, or attach evidence to another item. '
        'If evidence is insufficient, return no row.\n'
    )
    return instructions + json.dumps(
        {"tender": {"title": title}, "notification_items": items, "documents": documents},
        ensure_ascii=False,
        default=str,
    )


def _estimate(prompt: str, model: str) -> float:
    # Same conservative envelope as the production composition path.
    cost = spend_rub({"prompt_tokens": (len(prompt) + 1) // 2, "completion_tokens": MAX_OUTPUT_TOKENS}, model)
    if cost is None:
        raise CommandError(f"Для {model} нет подтверждённого тарифа — тест не запускается.")
    return cost


def _summarize(raw: object, allowed_ids: set[int], allowed_urls: set[str]) -> dict:
    rows = raw.get("items", []) if isinstance(raw, dict) and isinstance(raw.get("items"), list) else []
    valid, invalid, relationships, with_evidence = [], 0, Counter(), 0
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        if row.get("parent_source_item_id") not in allowed_ids or row.get("source_url") not in allowed_urls:
            invalid += 1
            continue
        relation = str(row.get("relationship") or "")
        if relation not in {"enrich", "split", "component"} or not str(row.get("name") or "").strip():
            invalid += 1
            continue
        relationships[relation] += 1
        if str(row.get("evidence") or "").strip():
            with_evidence += 1
        valid.append({
            "parent": row["parent_source_item_id"], "name": str(row["name"])[:160],
            "relationship": relation, "quantity_per_parent": row.get("quantity_per_parent"),
            "evidence": str(row.get("evidence") or "")[:240],
        })
    return {
        "returned_rows": len(rows), "valid_rows": len(valid), "invalid_rows": invalid,
        "with_evidence": with_evidence, "relationships": dict(relationships), "items": valid,
    }


class Command(BaseCommand):
    help = "Контролируемо сравнить модели на сохранённых документах тендера; ничего в БД не меняет."

    def add_arguments(self, parser):
        parser.add_argument("tender_id", type=int, nargs="?")
        parser.add_argument("--fixture", help="Reviewed local JSON case; does not access the database.")
        parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
        parser.add_argument("--max-total-rub", type=float, default=MAX_TOTAL_COST_RUB)

    def handle(self, *args, **options):
        cap = float(options["max_total_rub"])
        if cap <= 0 or cap > MAX_TOTAL_COST_RUB:
            raise CommandError(f"Max comparison budget is {MAX_TOTAL_COST_RUB:g} rub.")
        models = list(dict.fromkeys(options["models"]))
        if not models:
            raise CommandError("Укажите хотя бы одну модель.")

        if options["fixture"]:
            title, source_items, source_documents = _load_fixture(options["fixture"])
            subject = f"fixture {options['fixture']}"
        elif options["tender_id"] is not None:
            title, source_items, source_documents = _load_snapshot(options["tender_id"])
            subject = f"tender {options['tender_id']}"
        else:
            raise CommandError("Provide a tender_id or --fixture.")
        documents = _bounded_documents(source_documents)
        prompt = _prompt(title, source_items, documents)
        estimates = {model: _estimate(prompt, model) for model in models}
        if sum(estimates.values()) > cap:
            raise CommandError(
                f"Preflight estimate {sum(estimates.values()):.4f} rub. exceeds the {cap:.2f} rub. limit; no calls were made."
            )

        self.stdout.write(
            f"{subject}: {len(source_items)} items, {len(documents)} documents; "
            f"conservative maximum {sum(estimates.values()):.4f} rub. of {cap:.2f} rub."
        )
        total = 0.0
        allowed_ids = {item["id"] for item in source_items}
        allowed_urls = {item["url"] for item in documents}
        for model in models:
            # Remaining models are not called once their conservative envelope
            # would exceed the owner-approved total cap.
            if total + estimates[model] > cap:
                self.stdout.write(self.style.WARNING(f"{model}: остановлено лимитом до вызова."))
                break
            started = time.monotonic()
            try:
                raw, usage = _ai_gateway_json(
                    prompt, model=model, max_tokens=MAX_OUTPUT_TOKENS, timeout=120,
                    network_attempts=1, json_attempts=1,
                )
            except Exception as exc:
                usage = getattr(exc, "usage", {}) or {}
                cost = float(spend_rub(usage, model) or 0)
                total += cost
                self.stdout.write(self.style.ERROR(f"{model}: ошибка после ~{cost:.4f} ₽: {exc}"))
                continue
            cost = float(spend_rub(usage, model) or 0)
            total += cost
            result = {
                "model": model, "seconds": round(time.monotonic() - started, 2),
                "cost_rub": cost, "usage": usage,
                **_summarize(raw, allowed_ids, allowed_urls),
            }
            self.stdout.write(json.dumps(result, ensure_ascii=True, indent=2))
        self.stdout.write(self.style.SUCCESS(f"Total spent: {total:.4f} rub. of {cap:.2f} rub."))
