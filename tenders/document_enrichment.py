"""Generic document enrichment helpers; no tender or product-specific rules."""
from __future__ import annotations
from html.parser import HTMLParser
import re
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell, self.attrs = [], None, None, None, {}
    def handle_starttag(self, tag, attrs):
        if tag == "table": self.table = []
        elif tag == "tr" and self.table is not None: self.row = []
        elif tag in {"td", "th"} and self.row is not None: self.cell, self.attrs = [], dict(attrs)
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append({"text": re.sub(r"\s+", " ", "".join(self.cell)).strip()[:1000], "rowspan": max(1, int(self.attrs.get("rowspan", 1) or 1)), "colspan": max(1, int(self.attrs.get("colspan", 1) or 1))}); self.cell = None
        elif tag == "tr" and self.row is not None: self.table.append(self.row); self.row = None
        elif tag == "table" and self.table is not None: self.tables.append(expand_table(self.table)); self.table = None
def expand_table(rows):
    """Expand row/col spans without flattening product-row relationships."""
    spans, result = {}, []
    for source in rows:
        row, column, cells = [], 0, iter(source); pending = next(cells, None)
        while pending is not None or any(position >= column for position in spans):
            if column in spans:
                text, remaining = spans[column]; row.append(text)
                if remaining == 1: del spans[column]
                else: spans[column] = (text, remaining - 1)
                column += 1; continue
            text, colspan, rowspan = pending["text"], pending["colspan"], pending["rowspan"]
            row.extend([text] * colspan)
            if rowspan > 1:
                for offset in range(colspan): spans[column + offset] = (text, rowspan - 1)
            column += colspan; pending = next(cells, None)
        if any(row): result.append(row)
    return result
def document_table_payload(html):
    parser = _TableParser(); parser.feed(html or "")
    return [{"rows": rows} for rows in parser.tables[:12] if rows]


def component_total_quantity(parent_quantity, quantity_per_parent):
    """One quantity rule for staging and accepting documented aggregate contents."""
    from decimal import Decimal, InvalidOperation

    if parent_quantity is None or quantity_per_parent is None:
        return None
    try:
        parent, per_parent = Decimal(str(parent_quantity)), Decimal(str(quantity_per_parent))
        if not parent.is_finite() or not per_parent.is_finite() or parent <= 0 or per_parent <= 0:
            raise ValueError("Количество должно быть положительным числом")
        total = parent * per_parent
        if total != total.quantize(Decimal("0.01")):
            raise ValueError("Количество требует большей точности; уточните единицу измерения")
        return total
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Не удалось определить количество компонента") from exc


def is_item_enrichment(item):
    return item.source_type == "document_enrichment" or (item.metadata or {}).get("relationship") == "enrich"


def notification_origin(item, *, same_item=False):
    seen = set()
    while item is not None and item.pk not in seen:
        seen.add(item.pk)
        if item.source_type == "notification":
            return item
        if same_item and not is_item_enrichment(item):
            return None
        item = item.parent if item.parent_id else None
    return None


def merge_item_requirements(baseline, additions, *, provenance=None):
    """Keep baseline facts; append new evidence, deduplicate facts and expose conflicts."""
    from copy import deepcopy
    import json

    if isinstance(additions, dict) and additions.get("reconciled") is True:
        return reconcile_item_requirements(baseline, additions, provenance=provenance)

    def rows(requirements):
        if not isinstance(requirements, dict):
            return []
        result = list(requirements.get("characteristics") or [])
        result.extend({"name": name, "value": value} for name, value in requirements.items()
                      if name not in {"characteristics", "document_requirements", "field_provenance", "reconciled", "compiled_reconciliation", "omitted_facts", "reconciliation_error"})
        if isinstance(requirements.get("document_requirements"), dict):
            result.extend(rows(requirements["document_requirements"]))
        return result

    merged = deepcopy(baseline) if isinstance(baseline, dict) else {}
    merged.pop("reconciled", None); merged.pop("compiled_reconciliation", None)
    facts, seen = [], set()
    for requirements, default_source in ((baseline, "notification"), (additions, "document")):
        for raw in rows(requirements):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("characteristicName") or "").strip()
            value = raw.get("value") if raw.get("value") not in (None, "") else raw.get("characteristicValue")
            if not name or value in (None, "", [], {}):
                continue
            key = (" ".join(name.casefold().split()), " ".join((str(value) if isinstance(value, (str, int, float)) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)).casefold().split()))
            if key in seen:
                continue
            fact = {**deepcopy(raw), "name": name, "value": value, "source": (raw.get("source") or "notification") if default_source == "notification" else "document"}
            if default_source == "document" and provenance:
                fact["provenance"] = deepcopy(provenance)
            for prior in facts:
                if " ".join(prior["name"].casefold().split()) == key[0]:
                    prior["conflict"] = fact["conflict"] = True
            facts.append(fact); seen.add(key)
    merged["characteristics"] = facts
    return merged


def effective_item_requirements(item, *, strict=False):
    chain, seen = [], set()
    while item is not None and item.pk not in seen:
        seen.add(item.pk); chain.append(item)
        if not is_item_enrichment(item) or not item.parent_id:
            break
        item = item.parent
    requirements = {}
    for source in reversed(chain):
        if not requirements and source.source_type == "notification":
            requirements = merge_item_requirements(source.requirements, {})
        else:
            try:
                requirements = merge_item_requirements(requirements, source.requirements, provenance=source.provenance)
            except ValueError as exc:
                if strict:
                    raise
                from copy import deepcopy
                fallback = deepcopy(source.requirements or {})
                fallback.pop("reconciled", None); fallback.pop("compiled_reconciliation", None)
                requirements = merge_item_requirements(requirements, fallback, provenance=source.provenance)
                requirements["reconciliation_error"] = str(exc)
    return requirements


def notification_quote_source(item):
    origin = notification_origin(item, same_item=True)
    if origin is None:
        return None
    unit = lambda value: str(value or "").strip().casefold().rstrip(".")
    if item.quantity is not None and item.quantity != origin.quantity:
        return None
    if item.unit and unit(item.unit) != unit(origin.unit):
        return None
    return origin


def reconcile_item_requirements(baseline, proposal, *, provenance=None):
    """Validate model fact references; retain every input fact in the review trail."""
    from copy import deepcopy
    import json

    base = baseline.get("characteristics", []) if isinstance(baseline, dict) else []
    def key(fact):
        return (str(fact.get("name", "")).strip(), json.dumps(fact.get("value"), ensure_ascii=False, sort_keys=True, default=str, allow_nan=False))
    result = deepcopy(proposal)
    rows = result.get("characteristics")
    if not isinstance(rows, list):
        raise ValueError("Не передан итоговый перечень характеристик")
    if result.get("compiled_reconciliation"):
        represented = [fact for row in rows + result.get("omitted_facts", []) for fact in row.get("source_facts", [])]
        if not {key(fact) for fact in base} <= {key(fact) for fact in represented}:
            raise ValueError("Исходные факты изменились или не учтены в объединении")
        return result
    keep_refs = result.pop("keep_refs", [])
    if not isinstance(keep_refs, list):
        raise ValueError("Некорректный перечень сохраняемых исходных фактов")
    rows = [{"resolution": "keep", "baseline_refs": [index]} for index in keep_refs] + rows
    covered, output, omitted = set(), [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Некорректная характеристика")
        refs = row.get("baseline_refs")
        mode = row.get("resolution")
        if not isinstance(refs, list) or any(type(index) is not int or not 0 <= index < len(base) for index in refs) or len(refs) != len(set(refs)) or covered.intersection(refs):
            raise ValueError("Некорректные или повторные ссылки на исходные факты")
        if mode not in {"keep", "merge", "conflict", "new", "omit"}:
            raise ValueError("Не указано решение по характеристике")
        if (mode == "new" and refs) or (mode in {"keep", "omit"} and not refs):
            title = str(row.get("name") or "Характеристика")[:100]
            detail = "новая характеристика ошибочно ссылается на исходные факты" if mode == "new" else "для сохранения или исключения не указан исходный факт"
            raise ValueError(f"{title}: {detail}")
        facts = [deepcopy(base[index]) for index in refs]
        if mode in {"merge", "conflict"} and not refs and len(row.get("document_facts") or []) < 2:
            raise ValueError("Для объединения нужны исходные факты или несколько документов")
        reason = str(row.get("reason") or "").strip()
        if mode in {"merge", "conflict", "omit"} and not reason:
            raise ValueError("Для объединения, расхождения или исключения нужно объяснение")
        covered.update(refs)
        if mode == "omit":
            omitted.append({"reason": reason, "source_facts": facts})
            continue
        name, value = str(row.get("name") or "").strip(), row.get("value")
        if mode == "keep" and len(facts) == 1:
            name = name or facts[0]["name"]
            value = row.get("value", facts[0]["value"])
        if not name or value in (None, "", [], {}):
            raise ValueError("Итоговая характеристика не может быть пустой")
        if mode == "keep":
            if len(facts) != 1 or key({"name": name, "value": value}) != key(facts[0]):
                raise ValueError("Сохранённый факт должен совпадать с исходным")
            final = deepcopy(facts[0])
        else:
            fact_provenance = row.get("_source_provenance") or provenance or {}
            document_facts = row.get("document_facts") or []
            if not isinstance(document_facts, list):
                raise ValueError("Некорректные ссылки на документы")
            if mode == "new" and len(document_facts) > 1:
                raise ValueError("Для сведения нескольких источников нужно объяснение")
            for document_fact in document_facts:
                if not isinstance(document_fact, dict) or document_fact.get("value") in (None, "", [], {}):
                    raise ValueError("Не указан факт из документа")
                facts.append({"name": name, "value": document_fact["value"], "source": "document", "provenance": deepcopy(document_fact.get("_source_provenance") or provenance or {})})
            document_value = row.get("document_value")
            if document_value not in (None, "", [], {}):
                facts.append({"name": name, "value": document_value, "source": "document", "provenance": deepcopy(fact_provenance)})
            if mode == "new" and not document_facts and document_value in (None, "", [], {}):
                facts.append({"name": name, "value": value, "source": "document", "provenance": deepcopy(fact_provenance)})
            if mode == "merge" and len(facts) < 2:
                raise ValueError("Для объединения нужны исходные сведения или дополнение из документа")
            if mode == "conflict" and len({key(fact) for fact in facts}) < 2:
                raise ValueError("Для расхождения нужны разные исходные значения")
            final = {"name": name, "value": value, "source": "combined" if refs else "document", "conflict": mode == "conflict", "provenance": deepcopy(fact_provenance)}
        scope = row.get("scope", final.get("scope", "product"))
        if scope not in {"product", "completeness"}:
            raise ValueError("Неизвестная область характеристики")
        if scope == "completeness":
            final["scope"] = scope
        final.update(source_facts=facts, reason=reason)
        output.append(final)
    if covered != set(range(len(base))):
        raise ValueError("Не все исходные факты учтены в итоговом описании")
    unique = {}
    for fact in output:
        identity = key(fact)
        if identity in unique:
            unique[identity]["source_facts"].extend(fact["source_facts"])
            continue
        for prior in unique.values():
            if str(prior["name"]).strip().casefold() == str(fact["name"]).strip().casefold():
                prior["conflict"] = fact["conflict"] = True
        unique[identity] = fact
    result.update(characteristics=list(unique.values()), omitted_facts=omitted, compiled_reconciliation=True)
    return result


def characteristic_display(row, *, documents=None):
    """Original rows stay black; added or changed rows are blue in templates."""
    value = str(row.get("value") if row.get("value") is not None else "")
    facts, identities = [], set()
    def collect(fact):
        if fact.get("source_facts"):
            for nested in fact["source_facts"]:
                collect(nested)
        else:
            identity = (str(fact.get("name")), str(fact.get("value")), str(fact.get("source")), str((fact.get("provenance") or {}).get("document_url")))
            if identity not in identities:
                identities.add(identity); facts.append(fact)
    for fact in row.get("source_facts") or []:
        collect(fact)
    facts = [{**fact, "document": (documents or {}).get((fact.get("provenance") or {}).get("document_url"))} for fact in facts]
    name = str(row.get("name") or row.get("characteristicName") or "")
    original_facts = [fact for fact in facts if fact.get("source") == "notification"]
    unchanged = row.get("source") == "notification" or any(str(fact.get("name") or "") == name and str(fact.get("value") if fact.get("value") is not None else "") == value for fact in original_facts)
    changed = not unchanged
    parts = [{"text": value, "added": changed}]
    removed_text = ""
    return {"name": row.get("name") or row.get("characteristicName") or "", "value": value,
            "scope": row.get("scope", "product"), "source": row.get("source"), "conflict": row.get("conflict", False), "parts": parts, "name_added": changed,
            "removed_text": removed_text, "source_facts": facts, "reason": row.get("reason", ""), "show_sources": bool(facts) and (row.get("source") != "notification" or bool(row.get("conflict")))}


def item_name_parts(item):
    origin = notification_origin(item, same_item=True)
    original = [{"name": "", "value": origin.original_text, "source": "notification"}] if origin else []
    return characteristic_display({"value": item.original_text, "source": "notification" if item.source_type == "notification" else "combined" if origin else "document", "source_facts": original})["parts"]


def document_proposal_changes(item):
    """Compare usable proposed data, ignoring citations and model explanations."""
    if item.parent_id is None:
        return {"structural": True, "rows": [], "removed": [], "name": True, "quantity": True, "unit": True}
    parent = item.parent
    metadata = item.metadata or {}
    normalize = lambda value: " ".join(str(value if value is not None else "").split()).casefold()
    def identity(row):
        import json
        value = row.get("value")
        if not isinstance(value, (str, int, float, bool, type(None))):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return normalize(row.get("name")), normalize(value), row.get("scope", "product")
    try:
        before = effective_item_requirements(parent, strict=True).get("characteristics", [])
        after = effective_item_requirements(item, strict=True).get("characteristics", [])
    except ValueError as exc:
        return {"error": str(exc), "structural": True, "rows": [], "removed": [], "name": False, "quantity": False, "unit": False}
    old = {identity(row) for row in before}
    current = {identity(row) for row in after}
    covered = set(current)
    def source_identities(row):
        children = row.get("source_facts") or []
        if children:
            for child in children:
                source_identities(child)
        else:
            covered.add(identity(row))
    for row in after:
        source_identities(row)
    changes = {
        "structural": metadata.get("relationship") != "enrich" or bool(metadata.get("replaces_source_ids")) or metadata.get("supply_kind") == "unclear",
        "name": normalize(item.original_text) != normalize(parent.original_text),
        "quantity": item.quantity != parent.quantity,
        "unit": normalize(item.unit).rstrip('.') != normalize(parent.unit).rstrip('.'),
        "rows": [row for row in after if identity(row) not in old],
        "removed": [row for row in before if identity(row) not in covered],
    }
    national = metadata.get("national_regime") or {}
    previous = (parent.metadata or {}).get("national_regime") or {}
    national = national if isinstance(national, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    if national.get("status") not in (None, "unknown") and national.get("basis") and national != previous:
        changes["national_regime"] = national
    return changes if any(changes.values()) else {}
