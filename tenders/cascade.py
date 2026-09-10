"""Каскад подбора товара под строку тендера — 8 шагов, один класс, чёткие границы.

    Cascade(line, ...).run() -> CascadeResult

Каждый шаг — отдельный метод ``step_N_*``. Между шагами данные идут только через
типизированные структуры (:class:`Criterion` / карточка-``dict`` /
:class:`CascadeResult`). Ни один шаг не парсит сырой текст ТЗ — шаг 1 превращает
его в критерии, дальше работают только они.

Шаг 6 ограничивает новые проверки и переиспользует полный кэш. Расход зависит
от токенов и числа вызовов; без живого замера рублёвую стоимость не гарантируем.
Контракты шагов, диагностика и границы изменений: docs/cascade_steps.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.utils import timezone

from .catalog import (
    COLOR_PARENTS,
    CatalogSyncError,
    OasisClient,
    _aggregate_color_variants,
    _attribute_values,
    _capacity_mb,
    _color_family,
    _colors_compatible,
    _gifts_name_colors,
    _meaningful_tokens,
    _normalized,
    _product_variants,
    _refresh_live_oasis_prices,
    _score_pool_relevance,
    _text,
    _text_search_pool,
    _variant_size,
)
from .models import CascadeCache, CatalogProduct, CatalogSupplier

logger = logging.getLogger(__name__)

_STRONG_MODEL = os.getenv("TIMEWEB_AI_MODEL_SEARCH_PLAN", "").strip() or "anthropic/claude-sonnet-4-5"
_AGENT_MODEL = os.getenv("TIMEWEB_AI_MODEL_SHORTLIST", "").strip() or "anthropic/claude-sonnet-4-5"


def _axis_short(value, axis: str) -> str:
    if axis == "capacity":
        gb = value / 1024
        return f"{int(gb) if gb == int(gb) else round(gb, 1)} ГБ"
    if axis == "volume":
        return f"{int(value) if value == int(value) else round(value, 1)} мл"
    return str(value)

import re as _re

_VOLUME_RE = _re.compile(r"(\d+(?:\.\d+)?)\s*(мл|ml|л|l|литр\w*)(?![а-яa-z])", _re.I)
_CAP_LABEL_RE = _re.compile(r"\d+(?:[.,]\d+)?\s*(?:гб|gb|тб|tb|мб|mb|гигабайт|терабайт|мегабайт)", _re.I)
_VOL_LABEL_RE = _re.compile(r"\d+(?:[.,]\d+)?\s*(?:мл|ml|л|l|литр\w*)(?![а-яa-z])", _re.I)


# --------------------------------------------------------------------------- #
#  Типы на границах шагов
# --------------------------------------------------------------------------- #
@dataclass
class Criterion:
    """Одна строка ТЗ после смыслового разбора (шаг 1)."""

    label: str          # исходное название строки ТЗ (для панели и уроков)
    raw_value: str       # исходное значение строки ТЗ
    concept: str         # о чём строка, словами модели («ёмкость памяти»)
    operator: str        # >= <= = != ~ in
    value: str           # «32 ГБ», «синий», «металл»
    unit: str = ""
    checked: bool = True  # участвует ли в подборе
    axis: str = ""        # "capacity" | "volume" | "size" — чем отличаются варианты
    num_min: Decimal | None = None   # нижняя граница в канонических единицах (МБ / мл)
    num_max: Decimal | None = None
    options: list[str] = field(default_factory=list)  # набор допустимых значений

    def as_row(self) -> tuple[str, str]:
        return (self.label or self.concept, self.value or self.raw_value)


@dataclass
class CascadeResult:
    item: str
    queries: list[str]
    tz: list[Criterion]
    candidates: list[dict]
    catalog_intent: dict
    requirement_selection: list[dict]
    instructions: list[dict] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)
    removed: list[dict] = field(default_factory=list)
    ranking: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    usage_by_model: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------- #
#  Мелкие помощники
# --------------------------------------------------------------------------- #
def _cell(value) -> str:
    import re

    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_label(value) -> str:
    import re

    return re.sub(r"[^a-zа-я0-9]+", " ", _cell(value).lower().replace("ё", "е")).strip()


def _decimal(value):
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _volume_ml(text: str):
    """Объём строки («450 мл», «0,5 л») в миллилитрах, либо None."""
    match = _VOLUME_RE.search(str(text or "").lower().replace(",", "."))
    if not match:
        return None
    number = _decimal(match.group(1))
    if number is None:
        return None
    unit = match.group(2).lower()
    return number * (1000 if unit.startswith(("л", "l")) and unit != "ml" else 1)


def _variant_label(product) -> str:
    """Метка варианта: «XL» / «размер 50» из _variant_size, иначе ёмкость или
    объём из названия («16 ГБ», «450 мл») — флешки/кружки не кладут ёмкость в
    поле «Размер», а различаются именно ей."""
    label = _variant_size(product)
    if label:
        return label
    text = f"{product.name} {product.full_name or ''}"
    match = _CAP_LABEL_RE.search(text) or _VOL_LABEL_RE.search(text)
    return _cell(match.group(0)) if match else ""


def _cache_get(kind: str, key: str):
    try:
        row = CascadeCache.objects.filter(kind=kind, key=key).first()
        return row.payload if row and isinstance(row.payload, dict) else None
    except Exception:
        logger.exception("CascadeCache read failed (%s)", kind)
        return None


def _cache_put(kind: str, key: str, payload: dict) -> None:
    try:
        CascadeCache.objects.update_or_create(kind=kind, key=key, defaults={"payload": payload})
    except Exception:
        logger.exception("CascadeCache write failed (%s)", kind)


def _ai_json(prompt, *, max_tokens, timeout=45, model, images=None):
    """Тонкая обёртка над services._ai_gateway_json — импорт ленивый, чтобы не
    ловить цикл (services -> cascade -> services)."""
    from .services import _ai_gateway_json

    return _ai_gateway_json(
        prompt, max_tokens=max_tokens, timeout=timeout, network_attempts=3,
        model=model, image_data_urls=images or None, image_detail="low",
    )


# --------------------------------------------------------------------------- #
#  Каскад
# --------------------------------------------------------------------------- #
class Cascade:
    def __init__(
        self,
        line: dict,
        *,
        session_feedback=(),
        lessons_provider=None,
        prior: dict | None = None,
        skip_labels=frozenset(),
        ranking_override: dict | None = None,
        client=None,
        progress=None,
        top: int = 10,
    ):
        self.line = line if isinstance(line, dict) else {}
        self.session_feedback = [
            v for v in session_feedback
            if isinstance(v, dict) and _cell(v.get("text")) and (v.get("scope") or "catalog") in {"catalog", "requirements"}
        ]
        self.lessons_provider = lessons_provider
        self.prior = prior if isinstance(prior, dict) else {}
        self.skip_labels = frozenset(skip_labels)
        self.ranking = dict(ranking_override) if isinstance(ranking_override, dict) and ranking_override.get("price") in {"asc", "desc"} else {}
        self.client = client
        self.progress = progress
        self.top = top

        try:
            self.quantity = int(Decimal(str(self.line.get("quantity") or 0).replace(",", ".")))
        except (InvalidOperation, TypeError, ValueError):
            self.quantity = 0

        self.item = ""
        self.queries: list[str] = []
        self.tz: list[Criterion] = []
        self.feedback_instructions: list[dict] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.usage_by_model: dict[str, dict] = {}
        self.sources = {"oasis": {"status": "not_searched"}, "gifts": {"status": "not_searched"}}
        self.diagnostics = {"verdict_cache_hits": 0}
        self._oasis_mirror = False
        self._tz_hash = ""
        self.error = ""

    # -- запуск ----------------------------------------------------------- #
    def run(self) -> CascadeResult:
        self._ping("ai")
        self.step_1_parse_tz()
        phrases = self.step_2_search_plan()
        self._ping("catalog")
        pool = self.step_3_search_by_name(phrases)
        pool = self.step_4_name_filter(pool)
        cards = self.step_5_hard_gates_and_collapse(pool)
        self._ping("shortlist")
        graded = self.step_6_agent_matrix(cards)
        ranked = self.step_7_collapse_and_sort(graded)
        shown = self.step_8_price_and_top(ranked)
        cards = graded  # полный список (с _removed) для removed/outcome ниже
        return CascadeResult(
            item=self.item,
            queries=list(self.queries),
            tz=self.tz,
            candidates=shown,
            catalog_intent=self._catalog_intent(),
            requirement_selection=[
                {"label": c.label, "value": c.value or c.raw_value, "selected": c.checked}
                for c in self.tz if c.label
            ],
            instructions=self.feedback_instructions_result,
            outcome=self._outcome(cards),
            removed=[
                {"id": c.get("id"), "name": _cell(c.get("name"))[:120],
                 "article": _cell(c.get("article"))[:60], "reason": _cell(c.get("_removed_reason"))[:200]}
                for c in cards if c.get("_removed")
            ],
            ranking=self.ranking,
            usage=self.usage,
            usage_by_model=self.usage_by_model,
            sources=self.sources,
            diagnostics=self.diagnostics,
            error=self.error,
        )

    # -- шаг 1: разбор ТЗ ------------------------------------------------- #
    def step_1_parse_tz(self) -> list[Criterion]:
        """Один вызов сильной модели: сырые строки ТЗ → критерии {понятие,
        оператор, значение, ед., ось, границы} + чистое название товара +
        12–20 синонимов того же товара. Смысловой дедуп, нормализация
        единиц, решение по галочкам. Кэш по хэшу отмеченных строк."""
        rows = self._raw_requirement_rows()
        self._tz_hash = hashlib.sha1(
            json.dumps(
                [_cell(self.line.get("name"))] + [[_cell(r.get("label")), _cell(r.get("value"))] for r in rows],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        cached = _cache_get("tz", self._tz_hash)
        if cached:
            self._load_step1(cached, rows)
            self.diagnostics["tz_cache_hit"] = True
            self._pull_lessons()
            return self.tz

        numbered = "\n".join(
            f"{i}. {_cell(r.get('label'))}: {_cell(r.get('value'))}" for i, r in enumerate(rows, 1)
        ) or "(явных требований нет — дай только название и синонимы)"
        prompt = f"""Ты разбираешь ТЗ тендера на сувенирную/полиграфическую продукцию и готовишь поиск товара.

Позиция: {_cell(self.line.get('name'))[:300]}
Строки ТЗ:
{numbered}

Верни только JSON:
{{"item":"короткое название товара, 1-3 слова",
  "queries":["12-20 названий ЭТОГО ЖЕ товара: синонимы, разговорные, англ., альтернативные написания"],
  "criteria":[{{"n":1,"concept":"о чём строка, своими словами","operator":">=|<=|=|!=|~|in","value":"...","unit":"...","keep":true,"axis":"","num_min":null,"num_max":null,"options":[]}}]}}

Правила:
- item — конкретный вид товара. Убери «с логотипом», «с символикой», «услуги по изготовлению и поставке». Не заменяй вид товара более общим словом.
- queries — только названия ВИДА товара, без характеристик (цвет/объём/размер/материал). Не уходи в другой товар: флешка не карта microSD, шопер не рюкзак. Каталог называет один товар по-разному — дай все формы.
- criteria: одна запись на СМЫСЛОВУЮ характеристику. Объедини дубли («синий» и «цвет: синий» — одна; «объём», «ёмкость», «память» — одно понятие; возьми самую полную формулировку).
- keep=false для строк, которые НЕ признак готового товара: маркировка (Честный Знак, ЦРПТ), требования к пошиву и швам, макет и расположение логотипа, бумажные документы, сроки, гарантия. Физические свойства (материал, размер, цвет, конструкция, интерфейс) — keep=true.
- axis: "capacity" (память), "volume" (объём), "size" (размер одежды) — если это то, чем отличаются варианты ОДНОГО товара. Иначе "".
- num_min/num_max: переведи границу в канонические единицы — МБ для памяти (32 ГБ → 32768), мл для объёма (0,5 л → 500). «не менее» → num_min, «не более» → num_max, диапазон → оба. Иначе null.
- options: список допустимых значений, если требование перечислением (размеры «M, L, XL»; несколько цветов). Иначе [].
"""
        try:
            result, usage = _ai_json(prompt, max_tokens=1600, timeout=50, model=_STRONG_MODEL)
            self._add_usage(usage, _STRONG_MODEL)
        except Exception as exc:
            logger.exception("Cascade step 1 failed")
            self.error = _cell(exc)[:200]
            self.item = _cell(self.line.get("name"))[:120]
            self.queries = [self.item] if self.item else []
            self.tz = [self._fallback_criterion(r) for r in rows]
            self._pull_lessons()
            return self.tz

        payload = self._parse_step1(result, rows)
        _cache_put("tz", self._tz_hash, payload)
        self._load_step1(payload, rows)
        self._pull_lessons()
        return self.tz

    def _parse_step1(self, result, rows) -> dict:
        result = result if isinstance(result, dict) else {}
        item = _cell(result.get("item"))[:120] or _cell(self.line.get("name"))[:120]
        queries, seen = [], set()
        for value in result.get("queries") if isinstance(result.get("queries"), list) else []:
            text = _cell(value)[:150]
            if text and text.lower() not in seen:
                seen.add(text.lower())
                queries.append(text)
        if item and item.lower() not in seen:
            queries.insert(0, item)
        raw_criteria = result.get("criteria") if isinstance(result.get("criteria"), list) else []
        by_n = {}
        for entry in raw_criteria:
            if not isinstance(entry, dict):
                continue
            try:
                n = int(entry.get("n"))
            except (TypeError, ValueError):
                n = None
            by_n[n] = entry
        criteria = []
        for i, row in enumerate(rows, 1):
            entry = by_n.get(i, {})
            axis = _cell(entry.get("axis")).lower()
            axis = axis if axis in {"capacity", "volume", "size"} else ""
            options = [
                _cell(v)[:60] for v in (entry.get("options") if isinstance(entry.get("options"), list) else [])
                if _cell(v)
            ][:12]
            criteria.append({
                "label": _cell(row.get("label"))[:200],
                "raw_value": _cell(row.get("value"))[:500],
                "concept": _cell(entry.get("concept"))[:120] or _cell(row.get("label"))[:120],
                "operator": _cell(entry.get("operator"))[:4] or "~",
                "value": _cell(entry.get("value"))[:200] or _cell(row.get("value"))[:200],
                "unit": _cell(entry.get("unit"))[:24],
                "keep": entry.get("keep") is not False,
                "axis": axis,
                "num_min": str(_decimal(entry.get("num_min"))) if _decimal(entry.get("num_min")) is not None else None,
                "num_max": str(_decimal(entry.get("num_max"))) if _decimal(entry.get("num_max")) is not None else None,
                "options": options,
            })
        return {"item": item, "queries": queries[:24], "criteria": criteria}

    def _load_step1(self, payload, rows) -> None:
        self.item = _cell(payload.get("item"))[:120] or _cell(self.line.get("name"))[:120]
        self.queries = [
            _cell(v)[:150] for v in (payload.get("queries") if isinstance(payload.get("queries"), list) else [])
            if _cell(v)
        ] or ([self.item] if self.item else [])
        explicit = {
            _norm_label(r.get("label")): r.get("selected")
            for r in rows if isinstance(r, dict) and "selected" in r
        }
        self.tz = []
        for entry in payload.get("criteria") if isinstance(payload.get("criteria"), list) else []:
            label_n = _norm_label(entry.get("label"))
            ai_keep = entry.get("keep") is not False
            # приоритет: явная галочка клиента > сохранённое правило "вне подбора" > решение ИИ
            if label_n in explicit and explicit[label_n] is not None:
                checked = bool(explicit[label_n])
            elif label_n in self.skip_labels:
                checked = False
            else:
                checked = ai_keep
            self.tz.append(Criterion(
                label=_cell(entry.get("label"))[:200],
                raw_value=_cell(entry.get("raw_value"))[:500],
                concept=_cell(entry.get("concept"))[:120],
                operator=_cell(entry.get("operator"))[:4] or "~",
                value=_cell(entry.get("value"))[:200],
                unit=_cell(entry.get("unit"))[:24],
                checked=checked,
                axis=_cell(entry.get("axis")),
                num_min=_decimal(entry.get("num_min")),
                num_max=_decimal(entry.get("num_max")),
                options=[_cell(v) for v in (entry.get("options") or []) if _cell(v)],
            ))

    def _fallback_criterion(self, row) -> Criterion:
        return Criterion(
            label=_cell(row.get("label"))[:200], raw_value=_cell(row.get("value"))[:500],
            concept=_cell(row.get("label"))[:120], operator="~", value=_cell(row.get("value"))[:200],
            checked=_norm_label(row.get("label")) not in self.skip_labels and row.get("selected") is not False,
        )

    # -- шаг 2: план поиска --------------------------------------------- #
    def step_2_search_plan(self) -> list[str]:
        """Синонимы получены в шаге 1 (один вызов на оба). Здесь — сбор
        уникальных поисковых фраз."""
        phrases, seen = [], set()
        for value in [self.item, *self.queries]:
            text = _text(value, 150)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                phrases.append(text)
        self.diagnostics["query_phrases"] = phrases
        return phrases[:24]

    # -- шаг 3: поиск по названиям ------------------------------------- #
    def step_3_search_by_name(self, phrases) -> list:
        pool = []
        if CatalogProduct.objects.filter(supplier__code="oasis", is_active=True).exists():
            self._oasis_mirror = True
            oasis = _aggregate_color_variants(_text_search_pool("oasis", phrases), "oasis")
            pool.extend(oasis)
            self.sources["oasis"] = {"status": "success", "received": len(oasis)}
        else:
            self.sources["oasis"] = {"status": "not_configured"}
        if CatalogSupplier.objects.filter(code="gifts", is_active=True).exists():
            gifts = _aggregate_color_variants(_text_search_pool("gifts", phrases), "gifts")
            pool.extend(gifts)
            self.sources["gifts"] = {"status": "success", "received": len(gifts)}
        else:
            self.sources["gifts"] = {"status": "not_configured"}
        pool = _score_pool_relevance(pool, self.item, phrases)
        for product in pool:
            if not hasattr(product, "_relevance"):
                product._relevance = 1
        self.diagnostics["pool_after_search"] = len(pool)
        return pool

    # -- шаг 4: ИИ-фильтр названий ------------------------------------ #
    def step_4_name_filter(self, pool) -> list:
        """Дешёвая модель читает КАЖДОЕ название и отсеивает не-товар
        (коробка/чехол/картхолдер/кабель/набор). Спорное остаётся. Кэш по
        (item + набор id пула) — пул для одного товара стабилен между
        синками каталога, повтор пропускает вызов."""
        if not pool:
            return pool
        from .services import _run_name_filter

        ids = sorted(str(p.external_id) for p in pool)
        key = hashlib.sha1(
            (_norm_label(self.item) + "|" + "|".join(ids)).encode("utf-8")
        ).hexdigest()
        cached = _cache_get("namefilter", key)
        if cached and isinstance(cached.get("keep"), list):
            keep = {str(v) for v in cached["keep"]}
            self.diagnostics["name_filter"] = "cache"
        else:
            id_names = [(p.external_id, p.full_name or p.name) for p in pool]
            nf_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            keep = _run_name_filter(self.item or _cell(self.line.get("name")), id_names, usage=nf_usage)
            self._add_usage(nf_usage, os.getenv("TIMEWEB_AI_MODEL_NAME_FILTER", "").strip() or "openai/gpt-4.1-mini")
            if keep is None:
                self.diagnostics["name_filter"] = "skipped"
                return pool
            keep = {str(v) for v in keep}
            _cache_put("namefilter", key, {"keep": sorted(keep)})
        kept = [p for p in pool if str(p.external_id) in keep]
        self.diagnostics["name_filter_removed"] = len(pool) - len(kept)
        return kept

    # -- шаг 5: цвет + остаток + схлопывание -------------------------- #
    def step_5_hard_gates_and_collapse(self, pool) -> list[dict]:
        colour = next((c for c in self.tz if c.checked and ("цвет" in _norm_label(c.concept) or "цвет" in _norm_label(c.label) or c.axis == "color")), None)
        survivors = []
        for product in pool:
            if colour and self._colour_conflict(product, colour.value):
                continue
            transit = max(0, int(getattr(product, "stock_transit", 0) or 0))
            if self.quantity > 0 and product.total_stock <= 0 and transit <= 0 and not product.is_on_order:
                continue
            survivors.append(product)

        axis_criteria = [c for c in self.tz if c.checked and c.axis in {"capacity", "volume", "size"}]
        groups: dict[str, list] = {}
        for product in survivors:
            groups.setdefault(product.group_id or product.external_id, []).append(product)

        cards = []
        for skus in groups.values():
            fitting = self._variants_fitting_axes(skus, axis_criteria)
            face = max(
                fitting or skus,
                key=lambda p: (getattr(p, "_name_hits", 0), -(p.effective_price or Decimal("Infinity"))),
            )
            cards.append(self._serialize(face, skus))
        self.diagnostics["groups"] = len(cards)
        return cards

    def _colour_conflict(self, product, required: str) -> bool:
        if not _meaningful_tokens(required):
            return False
        values = product.colors if isinstance(product.colors, list) and product.colors else _attribute_values(product, ("цвет",))
        offered = " ".join(values)
        if not _meaningful_tokens(offered) or _colors_compatible(required, offered)[0]:
            return False
        name_colors = []
        if isinstance(product.raw_data, dict):
            name_colors = [str(v) for v in product.raw_data.get("name_colors", []) if str(v).strip()]
        name_colors = name_colors or _gifts_name_colors(product.full_name or product.name)
        if name_colors and _colors_compatible(required, " ".join(name_colors))[0]:
            return False
        rf, of = _color_family(required), _color_family(offered)
        return bool(
            rf and of and rf != of
            and COLOR_PARENTS.get(rf) != of and COLOR_PARENTS.get(of) != rf
        )

    def _variants_fitting_axes(self, skus, criteria) -> list:
        """SKU группы, которые НЕ нарушают ни один axis-критерий (молчащие по
        оси — не нарушают). Пусто → вызывающий берёт всю группу."""
        if not criteria:
            return []
        fitting = []
        for product in skus:
            ok = True
            for crit in criteria:
                value = self._axis_value(product, crit.axis)
                if value is not None:
                    if crit.num_min is not None and value < crit.num_min:
                        ok = False
                    if crit.num_max is not None and value > crit.num_max:
                        ok = False
                if crit.options:
                    label = _variant_size(product)
                    if label and _normalized(label) not in {_normalized(o) for o in crit.options}:
                        ok = False
            if ok:
                fitting.append(product)
        return fitting

    @staticmethod
    def _axis_value(product, axis: str):
        text = f"{product.name} {product.full_name or ''} {product.size or ''}"
        if axis == "capacity":
            return _capacity_mb(text)
        if axis == "volume":
            return _volume_ml(text)
        return None

    def _serialize(self, face, skus) -> dict:
        variants, variant_ids, sizes = [], [], []
        for product in sorted(skus, key=lambda p: p.effective_price or Decimal("Infinity")):
            raw = product.raw_data if isinstance(product.raw_data, dict) else {}
            inner = raw.get("variants") if isinstance(raw.get("variants"), list) and raw.get("variants") else None
            label = _variant_label(product)
            if inner:
                for variant in inner:
                    if isinstance(variant, dict) and variant.get("product_id") not in variant_ids:
                        entry = dict(variant)
                        entry["size"] = _cell(entry.get("size")) or label
                        variant_ids.append(entry.get("product_id"))
                        variants.append(entry)
            elif product.external_id not in variant_ids:
                variant_ids.append(product.external_id)
                variants.append({
                    "size": label,
                    "product_id": product.external_id,
                    "article": product.article,
                    "stock": max(0, product.total_stock),
                    "price": str(product.effective_price.quantize(Decimal("0.01"))) if product.effective_price is not None else None,
                })
            if label and label not in sizes:
                sizes.append(label)
        price = face.effective_price
        product_url = face.product_url
        supplier_site = urlparse(product_url or face.supplier.base_url).netloc.lower()
        if supplier_site.startswith("www."):
            supplier_site = supplier_site[4:]
        return {
            "id": face.external_id,
            "supplier_code": face.supplier.code,
            "supplier_name": face.supplier.name,
            "supplier_site": supplier_site,
            "external_id": face.external_id,
            "article": face.article,
            "name": face.full_name or face.name,
            "price": str(price) if price is not None else None,
            "cost_total": str((price * self.quantity).quantize(Decimal("0.01"))) if price is not None and self.quantity > 0 else None,
            "stock": face.total_stock,
            "delivery_days": face.delivery_days,
            "image_url": face.image_url,
            "url": product_url,
            "fit": "partial",
            "matches": [],
            "mismatches": [],
            "unknown": [],
            "mismatch_count": 0,
            "unknown_count": 0,
            "match_count": 0,
            "priority": 1,
            "relevance": getattr(face, "_relevance", 1),
            "eligibility": "exact_eligible",
            "eligibility_reasons": [],
            "synced_at": timezone.now().isoformat(),
            "category": (
                face.category_names[0]
                if isinstance(face.category_names, list) and face.category_names
                else "Поиск по названию"
            ),
            "sizes": sizes,
            "variant_ids": variant_ids or [face.external_id],
            "color_group_id": face.color_group_id or face.external_id,
            "variants": variants or list(_product_variants(face)),
            "description": _text(face.description, 800),
            "attributes": [
                {"name": _text(v.get("name"), 100), "value": _text(v.get("value"), 250)}
                for v in (face.attributes if isinstance(face.attributes, list) else [])
                if isinstance(v, dict) and _text(v.get("name"), 100) and _text(v.get("value"), 250)
            ][:20],
            "materials": [_text(v, 200) for v in (face.materials if isinstance(face.materials, list) else []) if _text(v, 200)],
            "colors": [_text(v, 120) for v in (face.colors if isinstance(face.colors, list) else []) if _text(v, 120)],
        }

    # -- шаг 6: умный агент, матрица ТЗ ------------------------------- #
    def step_6_agent_matrix(self, cards) -> list[dict]:
        rows = [c.as_row() for c in self.tz if c.checked]
        self.feedback_instructions_result = []
        for card in cards:
            self._init_unknown(card, rows)

        if not rows and not self.feedback_instructions:
            for card in cards:
                card.update(fit="exact", matches=[], mismatches=[], unknown=[], mismatch_count=0, unknown_count=0, match_count=0)
            return cards

        # Матрица ТЗ — только по ТЗ, независимо от фидбека. Клетки из кэша
        # (карточка, хэш ТЗ); к модели идут только карточки без записи.
        todo, cached_cards = [], []
        for card in cards:
            cached = _cache_get("verdict", f"{self._tz_hash}|{card['id']}") if rows else None
            grid = cached.get("grid", {}) if isinstance(cached, dict) else {}
            cells = {
                index: tuple(grid[str(index)])
                for index in range(1, len(rows) + 1)
                if isinstance(grid, dict) and isinstance(grid.get(str(index)), (list, tuple))
            }
            if self._complete_grid(cells, rows):
                self._apply_cells(card, cells, rows)
                card["matrix_status"] = "complete"
                cached_cards.append(card)
                self.diagnostics["verdict_cache_hits"] += 1
            else:
                todo.append(card)
        if rows:
            self._grade_bounded(todo, rows, cached_cards=cached_cards)

        if self.feedback_instructions:
            self._classify_feedback(cards, rows)
        return cards

    def _init_unknown(self, card, rows) -> None:
        card["matches"] = []
        card["mismatches"] = []
        card["unknown"] = [label for label, _ in rows]
        card["mismatch_count"] = 0
        card["match_count"] = 0
        card["unknown_count"] = len(rows)
        card["fit"] = "partial" if rows else "exact"
        card["matrix_status"] = "pending" if rows else "complete"
        card.pop("_ai_graded", None)

    @staticmethod
    def _complete_grid(cells, rows) -> bool:
        return bool(rows) and all(
            index in cells and len(cells[index]) == 2 and cells[index][0] in {"y", "n", "m"}
            for index in range(1, len(rows) + 1)
        )

    @staticmethod
    def _suitable_for_stop(card) -> bool:
        return (
            card.get("matrix_status") == "complete"
            and card.get("mismatch_count", 0) <= 1
            and card.get("match_count", 0) > 0
        )

    @staticmethod
    def _preagent_key(card, axis_cells):
        cells = axis_cells.get(str(card["id"]), {})
        price = _decimal(card.get("price"))
        return (
            card.get("relevance", 1),
            sum(v == "n" for v, _ in cells.values()),
            -sum(v == "y" for v, _ in cells.values()),
            price if price is not None else Decimal("Infinity"),
        )

    def _grade_bounded(self, todo, rows, *, cached_cards=()) -> None:
        """Ограничивает новые проверки; кэш участвует в условии остановки.

        matrix_status — контракт с шагом 7: complete / incomplete / pending.
        Полный ответ «m» отличается от пропущенной клетки и может кэшироваться.
        """
        axis_cells = self._axis_prefill(todo, rows)
        ranked = sorted(todo, key=lambda card: self._preagent_key(card, axis_cells))
        first = max(1, int(os.getenv("CASCADE_STEP6_FIRST", "25")))
        ceiling = max(0, int(os.getenv("CASCADE_STEP6_CEILING", "75")))
        suitable = sum(self._suitable_for_stop(card) for card in cached_cards)
        diagnostics = {
            "pool": len(todo), "graded": 0, "batches": 0, "cached": len(cached_cards),
            "complete": len(cached_cards), "suitable": suitable,
        }
        self.diagnostics["step6"] = diagnostics
        stop_reason = "exhausted"
        for offset in range(0, min(len(ranked), ceiling), first):
            if suitable >= 10:
                break
            batch = ranked[offset:min(offset + first, ceiling)]
            grids = self._grade_grid(batch, rows, model=_AGENT_MODEL, batch_size=3)
            diagnostics["graded"] += len(batch)
            diagnostics["batches"] += 1
            for card in batch:
                cid = str(card["id"])
                model_cells = grids.get(cid, {})
                complete = self._complete_grid(model_cells, rows)
                cells = {**model_cells, **axis_cells.get(cid, {})}
                self._apply_cells(card, cells, rows)
                card["matrix_status"] = "complete" if complete else "incomplete"
                if complete:
                    diagnostics["complete"] += 1
                    suitable += self._suitable_for_stop(card)
                    _cache_put("verdict", f"{self._tz_hash}|{cid}", {
                        "grid": {str(k): list(v) for k, v in cells.items()},
                    })
                else:
                    card["fit"] = "partial"
            if not grids:
                stop_reason = "empty_response"
                break
        diagnostics["suitable"] = suitable
        diagnostics["pending"] = len(todo) - diagnostics["graded"]
        if suitable >= 10:
            stop_reason = "enough_suitable"
        elif stop_reason != "empty_response" and diagnostics["pending"]:
            stop_reason = "ceiling"
        diagnostics["stop_reason"] = stop_reason

    def _axis_prefill(self, cards, rows) -> dict:
        """Клетки, которые код считает точнее модели: числовая ось из шага 1
        (ёмкость/объём). Ставим ТОЛЬКО когда число однозначно — иначе строку
        отдаём агенту. Никогда не вносит вердикт, которого агент бы не дал."""
        axis_by_row = {}
        checked = [c for c in self.tz if c.checked]
        for idx, crit in enumerate(checked, 1):
            if crit.axis in {"capacity", "volume"} and (crit.num_min is not None or crit.num_max is not None):
                axis_by_row[idx] = crit
        if not axis_by_row:
            return {}
        out: dict[str, dict] = {}
        for card in cards:
            caps = self._card_axis_values(card)
            for row_idx, crit in axis_by_row.items():
                values = caps.get(crit.axis, [])
                if not values:
                    continue  # карточка молчит про ось — пусть судит агент
                fits = [
                    v for v in values
                    if (crit.num_min is None or v >= crit.num_min) and (crit.num_max is None or v <= crit.num_max)
                ]
                if fits:
                    out.setdefault(str(card["id"]), {})[row_idx] = ("y", "по варианту")
                else:
                    best = max(values) if crit.num_min is not None else min(values)
                    out.setdefault(str(card["id"]), {})[row_idx] = ("n", f"{_axis_short(best, crit.axis)}, нужно {crit.value}")
        return out

    def _card_axis_values(self, card) -> dict:
        """{'capacity': [МБ...], 'volume': [мл...]} по названию + всем вариантам."""
        texts = [f"{card.get('name', '')}"] + [
            _cell(v.get("size")) for v in (card.get("variants") or []) if isinstance(v, dict) and _cell(v.get("size"))
        ] + [_cell(s) for s in (card.get("sizes") or [])]
        caps, vols = [], []
        for text in texts:
            mb = _capacity_mb(text)
            if mb is not None:
                caps.append(mb)
            ml = _volume_ml(text)
            if ml is not None:
                vols.append(ml)
        return {"capacity": caps, "volume": vols}

    def _grade_grid(self, cards, rows, *, model, batch_size) -> dict:
        """Возвращает {id карточки: {номер строки: (v, w)}}. Не применяет и не
        кэширует — это делает вызывающий."""
        if not cards:
            return {}
        batches = [cards[i:i + batch_size] for i in range(0, len(cards), batch_size)] or [cards]
        cells_by_card: dict[str, dict[int, tuple]] = {}
        errors = []

        def run_batch(indexed):
            _index, batch = indexed
            local = {pos: str(card["id"]) for pos, card in enumerate(batch, 1)}
            cards_text = "\n\n".join(
                f"КАРТОЧКА {pos} | id {card['id']}\n{self._card_brief(card)}"
                for pos, card in enumerate(batch, 1)
            )
            prompt = self._step6_prompt(
                rows, cards_text, [],
                batch_ids=", ".join(f"{p}={c['id']}" for p, c in enumerate(batch, 1)) if len(batches) > 1 else "",
            )
            try:
                raw, usage = _ai_json(prompt, max_tokens=700 + len(batch) * (len(rows) + 2) * 24,
                                      timeout=60, model=model)
            except Exception as exc:
                logger.exception("Cascade step 6 grid batch failed (%s)", model)
                return {"_error": _cell(exc)[:200]}, {}
            return raw if isinstance(raw, dict) else {}, usage, local, model

        results = (
            [run_batch((0, batches[0]))]
            if len(batches) == 1
            else list(ThreadPoolExecutor(max_workers=min(12, len(batches))).map(run_batch, list(enumerate(batches))))
        )
        for result in results:
            if len(result) == 2:
                errors.append(result[0].get("_error", ""))
                continue
            raw, usage, local, used_model = result
            self._add_usage(usage, used_model)
            for cell in raw.get("grid") if isinstance(raw.get("grid"), list) else []:
                if not isinstance(cell, dict):
                    continue
                try:
                    pos, row = int(cell.get("c")), int(cell.get("r"))
                except (TypeError, ValueError):
                    continue
                verdict = next((ch for ch in _cell(cell.get("v")).lower() if ch in "ynm"), "")
                card_id = local.get(pos)
                if card_id and verdict and 1 <= row <= len(rows):
                    cells_by_card.setdefault(card_id, {})[row] = (verdict, _cell(cell.get("w"))[:90])

        if errors and not cells_by_card:
            self.error = self.error or errors[0]
        return cells_by_card

    def _classify_feedback(self, cards, rows) -> None:
        """Один проход: классифицирует замечания администратора и уроки в
        priority / exclude / keep_only / soften / ranking и раздаёт по
        карточкам. Матрицу ТЗ не трогает."""
        from .services import _shortlist_card_images

        images, image_ids = ([], [])
        if any(v.get("origin") == "session" for v in self.feedback_instructions):
            images, image_ids = _shortlist_card_images(cards)
        instr_numbered = [
            f"{i}. ({'эта сессия' if v.get('origin') == 'session' else 'раньше на похожих позициях'}) {_cell(v.get('text'))}"
            for i, v in enumerate(self.feedback_instructions, 1)
        ]
        batches = [cards[i:i + 12] for i in range(0, len(cards), 12)] or [cards]
        raw_instructions: list[dict] = []
        errors = []

        def run_batch(indexed):
            index, batch = indexed
            local = {pos: str(card["id"]) for pos, card in enumerate(batch, 1)}
            cards_text = "\n\n".join(
                f"КАРТОЧКА {pos} | id {card['id']}\n{self._card_brief(card)}"
                for pos, card in enumerate(batch, 1)
            )
            with_images = bool(images) and index == 0
            prompt = self._step6_prompt(
                rows, cards_text, instr_numbered, want_grid=False,
                image_ids=image_ids if with_images else None,
                batch_ids=", ".join(f"{p}={c['id']}" for p, c in enumerate(batch, 1)) if len(batches) > 1 else "",
            )
            try:
                raw, usage = _ai_json(prompt, max_tokens=400 + len(self.feedback_instructions) * 120,
                                      timeout=60, model=_AGENT_MODEL,
                                      images=images if with_images else None)
            except Exception as exc:
                logger.exception("Cascade feedback classification batch failed")
                return {"_error": _cell(exc)[:200]}, {}
            return raw if isinstance(raw, dict) else {}, usage, local

        results = (
            [run_batch((0, batches[0]))]
            if len(batches) == 1
            else list(ThreadPoolExecutor(max_workers=min(10, len(batches))).map(run_batch, list(enumerate(batches))))
        )
        for result in results:
            if len(result) == 2:
                errors.append(result[0].get("_error", ""))
                continue
            raw, usage, local = result
            self._add_usage(usage, _AGENT_MODEL)
            for item in raw.get("instructions") if isinstance(raw.get("instructions"), list) else []:
                if isinstance(item, dict):
                    item["cards"] = [local.get(c, c) if isinstance(c, int) else c for c in (item.get("cards") or [])]
                    raw_instructions.append(item)

        self._apply_instructions(raw_instructions, {str(c["id"]): c for c in cards}, bool(errors))

    def _apply_cells(self, card, cells, rows) -> None:
        matches, mismatches, unknown = [], [], []
        for index, (label, value) in enumerate(rows, 1):
            verdict, reason = cells.get(index, ("m", ""))
            tail = f" — {reason}" if reason else ""
            if verdict == "y":
                matches.append(f"{label}: {value}{tail}")
            elif verdict == "n":
                mismatches.append(f"{label}: требуется {value}{tail}")
            else:
                unknown.append(f"{label}{tail or ' — нет данных в карточке'}")
        card["matches"], card["mismatches"], card["unknown"] = matches, mismatches, unknown
        card["match_count"] = len(matches)
        card["mismatch_count"] = len(mismatches)
        card["unknown_count"] = len(unknown)
        card["fit"] = "exact" if not mismatches and not unknown else "partial"
        card["_ai_graded"] = True

    def _card_brief(self, card) -> str:
        lines = [f"[{card['id']}] {_cell(card.get('name'))[:110]}"]
        meta = []
        if _cell(card.get("article")):
            meta.append(f"арт {_cell(card.get('article'))[:24]}")
        if card.get("price") not in (None, ""):
            meta.append(f"{card['price']} ₽")
        materials = ", ".join(_cell(v)[:40] for v in (card.get("materials") or [])[:4] if _cell(v))
        if materials:
            meta.append(materials)
        colors = ", ".join(_cell(v)[:24] for v in (card.get("colors") or [])[:6] if _cell(v))
        if colors:
            meta.append(colors)
        if meta:
            lines.append("   " + " | ".join(meta))
        attributes = "; ".join(
            f"{_cell(v.get('name'))[:40]}: {_cell(v.get('value'))[:70]}"
            for v in (card.get("attributes") or [])[:16]
            if isinstance(v, dict) and _cell(v.get("name")) and _cell(v.get("value"))
        )
        if attributes:
            lines.append("   " + attributes)
        variant_labels = [
            _cell(v.get("size")) for v in (card.get("variants") or [])
            if isinstance(v, dict) and _cell(v.get("size"))
        ]
        if not variant_labels:
            variant_labels = [_cell(s) for s in (card.get("sizes") or []) if _cell(s)]
        if len(variant_labels) > 1:
            lines.append("   Варианты: " + ", ".join(dict.fromkeys(variant_labels))[:300])
        description = _cell(card.get("description"))[:700]
        if description:
            lines.append("   " + description)
        return "\n".join(lines)

    def _step6_prompt(self, rows, cards_text, instr_numbered, *, want_grid=True, image_ids=None, batch_ids="") -> str:
        # Порядок: сначала ВСЁ статичное для позиции (задача, чек-лист ТЗ,
        # правила, замечания) — оно байт-в-байт одинаково во всех пачках, шлюз
        # кэширует префикс. Переменное (id пачки, карточки, фото) — в конце.
        row_count = len(rows) or 1
        tz_block = "\n".join(f"{i}. {label}: {value}" for i, (label, value) in enumerate(rows, 1)) or "1. (пунктов ТЗ нет)"
        instr_block = ""
        instr_schema = ""
        if instr_numbered:
            instr_schema = ',"instructions":[{"n":1,"type":"priority|keep_only|exclude|soften|ranking","criterion":"...","cards":["id"],"price":"asc|desc","applies_to":"item|any","applied":true}]'
            instr_block = (
                "\nЗамечания администратора:\n" + "\n".join(instr_numbered) + "\n"
                "По каждому: \"priority\" (подними/нужны X/нечёткое — cards где критерий ЯВНО выполнен); "
                "\"keep_only\" (оставь только X — cards ВСЕХ, кто ЯВНО подходит); "
                "\"exclude\" (убери/без X, без «только» — cards кто ЯВНО противоречит); "
                "\"soften\" (X это норм — cards где расхождение теперь ок); "
                "\"ranking\" (сначала дорогие/дешёвые → price asc|desc). "
                "Условное «если в ТЗ …»: не выполняется → applied:false, cards:[]. applies_to: item|any.\n"
            )
        if want_grid:
            head = (
                "Ты эксперт по подбору товара под тендер. Прочитай КАЖДУЮ карточку целиком "
                "(название, характеристики, материалы, цвет, список вариантов, описание) и оцени "
                "КАЖДЫЙ пункт чек-листа так, как это сделал бы человек.\n\n"
                f"Позиция: {_cell(self.line.get('name'))[:200]}\n\n"
                f"Чек-лист ТЗ (1..{row_count}):\n{tz_block}\n"
                f"{instr_block}\n"
                f"Для КАЖДОЙ карточки и КАЖДОГО пункта 1..{row_count} верни клетку "
                "{\"c\":номер карточки,\"r\":номер пункта,\"v\":\"y|n|m\",\"w\":\"до 6 слов, только для n и m\"}:\n"
                "- \"y\" — карточка (или её подходящий вариант) соответствует пункту;\n"
                "- \"n\" — в карточке есть данные по пункту и они НЕ совпадают;\n"
                "- \"m\" — в карточке про пункт ничего нет.\n"
                f"Ровно {row_count} клеток на карточку. Пропущенная = \"m\". \"m\" — только когда данных реально нет.\n"
                "Небольшое отклонение размера/веса → \"y\". Заметное, но возможно допустимое → \"m\" («X vs Y, проверить»). "
                "Явно не то → \"n\". «Не менее N»: меньше N — \"n\". «Не более N»: больше N — \"n\". "
                "Ёмкость/объём бери из названия варианта. «Флеш-карта USB 2.0» и «USB-флеш-накопитель» — одно и то же.\n"
                f"Ответ: {{\"grid\":[{{\"c\":1,\"r\":1,\"v\":\"y\"}},{{\"c\":1,\"r\":5,\"v\":\"n\",\"w\":\"8 ГБ, нужно ≥32\"}}]{instr_schema}}}\n"
            )
        else:
            head = (
                "Ты эксперт по подбору товара под тендер. Классифицируй КАЖДОЕ замечание "
                "администратора и укажи id карточек.\n\n"
                f"Позиция: {_cell(self.line.get('name'))[:200]}\n\n"
                f"Чек-лист ТЗ (1..{row_count}):\n{tz_block}\n"
                f"{instr_block}\n"
                "Ответ: {\"instructions\":[{\"n\":1,\"type\":\"priority|keep_only|exclude|soften|ranking\","
                "\"criterion\":\"...\",\"cards\":[\"id\"],\"price\":\"asc|desc\",\"applies_to\":\"item|any\",\"applied\":true}]}\n"
            )
        tail = ""
        if batch_ids:
            tail += f"\nОтвечай ТОЛЬКО по карточкам этой пачки ({batch_ids}).\n"
        tail += f"\nКарточки (номер | id | текст):\n{cards_text}\n"
        if image_ids:
            tail += "\nФото карточек по порядку: " + ", ".join(f"[{v}]" for v in image_ids) + ".\n"
        return head + tail

    def _apply_instructions(self, raw_instructions, by_id, some_failed) -> None:
        merged: dict[str, dict] = {}
        for item in raw_instructions:
            slot = merged.setdefault(str(item.get("n")), {"cards": [], "blocked": 0, "applied": 0})
            for field_name in ("type", "criterion", "price", "applies_to"):
                if item.get(field_name) and not slot.get(field_name):
                    slot[field_name] = item.get(field_name)
            for card_id in item.get("cards") or []:
                if str(card_id) not in {str(v) for v in slot["cards"]}:
                    slot["cards"].append(card_id)
            slot["blocked" if item.get("applied") is False else "applied"] += 1

        results = []
        for index, value in enumerate(self.feedback_instructions, 1):
            info = merged.get(str(index), {})
            itype = _cell(info.get("type")).lower()
            itype = itype if itype in {"priority", "keep_only", "exclude", "soften", "ranking"} else ""
            criterion = _cell(info.get("criterion"))[:120]
            ids = {str(v) for v in info.get("cards", [])}
            blocked = info.get("blocked", 0) and not info.get("applied", 0)
            applied = False
            if blocked:
                pass
            elif itype == "ranking" and _cell(info.get("price")).lower() in {"asc", "desc"}:
                self.ranking = {"price": _cell(info.get("price")).lower()}
                applied = True
            elif itype == "priority" and criterion:
                for card_id in ids:
                    if card_id in by_id:
                        by_id[card_id]["priority"] = 0
                applied = True
            elif itype == "keep_only" and criterion and ids and not some_failed:
                for card_id, card in by_id.items():
                    if card_id not in ids:
                        card["_removed"] = True
                        card["_removed_reason"] = f"не {criterion}"
                applied = True
            elif itype == "exclude" and criterion:
                for card_id in ids:
                    if card_id in by_id:
                        by_id[card_id]["_removed"] = True
                        by_id[card_id]["_removed_reason"] = criterion
                applied = True
            elif itype == "soften" and criterion:
                for card_id in ids:
                    card = by_id.get(card_id)
                    if card is not None:
                        self._soften(card, criterion)
                applied = True
            results.append({
                "text": _cell(value.get("text")),
                "origin": value.get("origin") or "session",
                "lesson_id": value.get("lesson_id"),
                "type": itype,
                "criterion": criterion,
                "applies_to": "any" if _cell(info.get("applies_to")).lower() == "any" else "item",
                "applied": applied,
                "condition_blocked": bool(blocked),
                "ranking_only": itype == "ranking",
                "summary": criterion or _cell(value.get("text"))[:120],
                "note": "",
            })
        self.feedback_instructions_result = results

    @staticmethod
    def _soften(card, criterion) -> None:
        stems = {w[:4] for w in _norm_label(criterion).split() if len(w) >= 4}
        for key in ("mismatches", "unknown"):
            kept = [
                entry for entry in (card.get(key) or [])
                if not (stems and stems & {w[:4] for w in _norm_label(entry).split() if len(w) >= 4})
            ]
            card[key] = kept
        card["mismatch_count"] = len(card.get("mismatches") or [])
        card["unknown_count"] = len(card.get("unknown") or [])
        card["match_count"] = len(card.get("matches") or [])
        card["fit"] = "exact" if not card["mismatch_count"] and not card["unknown_count"] else "partial"
        card["_ai_touched"] = True

    # -- шаг 7: схлопывание + сортировка ---------------------------- #
    def step_7_collapse_and_sort(self, cards) -> list[dict]:
        """Финальный фиксированный ключ. БЕЗ обрезки. Ручной приоритет (0)
        ставит ТОЛЬКО замечание «подними X» из фидбека — см. _apply_instructions."""
        live = [c for c in cards if not c.get("_removed")]
        price_desc = self.ranking.get("price") == "desc"

        def price_key(value):
            number = _decimal(value)
            if number is None:
                return Decimal("-Infinity") if price_desc else Decimal("Infinity")
            return -number if price_desc else number

        live.sort(key=lambda c: (
            0 if c.get("priority") == 0 else 1,
            c.get("mismatch_count", 0),
            -c.get("match_count", 0),
            c.get("unknown_count", 0),
            c.get("relevance", 1),
            0 if _decimal(c.get("price")) is not None else 1,
            price_key(c.get("price")),
            _norm_label(c.get("name")),
            _norm_label(c.get("article")),
        ))
        return live

    # -- шаг 8: цена + показ ~10 ------------------------------------ #
    def step_8_price_and_top(self, cards, top=None) -> list[dict]:
        shown = cards[: top or self.top]
        if self._oasis_mirror and shown:
            try:
                self.client = self.client or OasisClient()
                _refresh_live_oasis_prices(self.client, shown, quantity=self.quantity)
            except (CatalogSyncError, Exception):
                logger.exception("Cascade step 8 live price refresh skipped")
        return shown

    # -- вспомогательное ------------------------------------------------ #
    def _raw_requirement_rows(self) -> list[dict]:
        from .services import _collapse_requirements

        rows = self.line.get("requirements")
        rows = rows.get("requirements") if isinstance(rows, dict) else rows
        rows = _collapse_requirements(rows) if isinstance(rows, list) else []
        return [
            r for r in rows
            if isinstance(r, dict) and _cell(r.get("label")) and _cell(r.get("value"))
        ]

    def _pull_lessons(self) -> None:
        instructions = [
            {"text": _cell(v.get("text")), "origin": "session"}
            for v in self.session_feedback
        ]
        if self.lessons_provider is not None:
            labels = [_norm_label(c.label) for c in self.tz if c.checked and c.label]
            try:
                for lesson in self.lessons_provider(self.item, labels) or []:
                    instructions.append({
                        "text": _cell(lesson.get("instruction")),
                        "origin": "lesson",
                        "lesson_id": lesson.get("id"),
                    })
            except Exception:
                logger.exception("Cascade lesson retrieval failed")
        self.feedback_instructions = [v for v in instructions if _cell(v.get("text"))]

    def _catalog_intent(self) -> dict:
        return {
            "item": self.item,
            "categories": [self.item] if self.item else [],
            "synonyms": list(self.queries),
            "required": [
                {"label": c.label, "value": c.value or c.raw_value}
                for c in self.tz if c.checked and c.label
            ],
            "ranking_override": {"price": self.ranking["price"]} if self.ranking.get("price") in {"asc", "desc"} else {},
        }

    def _outcome(self, cards) -> dict:
        removed = [c for c in cards if c.get("_removed")]
        raised = [c for c in cards if c.get("priority") == 0]
        softened = [c for c in cards if c.get("_ai_touched") and not c.get("_removed") and c.get("priority") != 0]
        return {
            "removed": [
                {"article": _cell(c.get("article"))[:60], "name": _cell(c.get("name"))[:80], "reason": _cell(c.get("_removed_reason"))[:120]}
                for c in removed
            ][:15],
            "raised_count": len(raised),
            "raised": [_cell(c.get("name"))[:70] for c in raised[:4]],
            "softened_count": len(softened),
            "softened": [_cell(c.get("name"))[:70] for c in softened[:4]],
            "verdict_changes": sum(1 for c in cards if c.get("_ai_graded")),
        }

    def _add_usage(self, usage, model=None) -> None:
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        slot = self.usage_by_model.setdefault(model or "unknown", {"prompt_tokens": 0, "completion_tokens": 0})
        slot["prompt_tokens"] += prompt_tokens
        slot["completion_tokens"] += completion_tokens

    def _ping(self, stage) -> None:
        if self.progress:
            try:
                self.progress(stage)
            except Exception:
                pass


# feedback_instructions_result существует до шага 6 — заглушка для раннего доступа
Cascade.feedback_instructions_result = []
