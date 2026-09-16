"""Каскад подбора товара под строку тендера — 8 шагов, один класс, чёткие границы.

    Cascade(line, ...).run() -> CascadeResult

Каждый шаг — отдельный метод ``step_N_*``. Между шагами данные идут только через
типизированные структуры (:class:`Criterion` / карточка-``dict`` /
:class:`CascadeResult`). Ни один шаг не парсит сырой текст ТЗ — шаг 1 превращает
его в критерии, дальше работают только они.

Шаг 6 ограничивает новые проверки и переиспользует полный кэш. Расход зависит
от токенов и числа вызовов; без живого замера рублёвую стоимость не гарантируем.
Контракты шагов, диагностика и границы изменений: docs/assistant_protocol.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.db.models.signals import post_delete, post_save
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
    _SIZE_SUFFIX_RE,
    _text,
    _text_search_pool,
    _variant_size,
)
from .models import CascadeCache, CatalogProduct, CatalogSupplier, UnitAlias
from .cascade_settings import text_search_settings

logger = logging.getLogger(__name__)

_STRONG_MODEL = os.getenv("TIMEWEB_AI_MODEL_SEARCH_PLAN", "").strip() or "anthropic/claude-sonnet-4-5"
_AGENT_MODEL = os.getenv("TIMEWEB_AI_MODEL_SHORTLIST", "").strip() or "anthropic/claude-sonnet-4-5"
_FAST_MODEL = os.getenv("TIMEWEB_AI_MODEL_NAME_FILTER", "").strip() or "openai/gpt-4.1-mini"
_TITLE_MODEL = os.getenv("TIMEWEB_AI_MODEL_TITLE", "").strip() or "openai/gpt-4.1-mini"


def _selected_model(value, default):
    if value == "fast":
        return _FAST_MODEL
    if value == "strong":
        return _STRONG_MODEL
    from .gateway_budget import available_models

    return value if value in available_models() else default


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
    axis: str = ""        # свободное слово-код оси варианта («capacity», «power», …)
                           # или "" — не ограничено списком, решает модель шага 1
    num_min: Decimal | None = None   # нижняя граница; для axis capacity/volume — в МБ/мл,
    num_max: Decimal | None = None   # для остальных — в исходной единице (unit)
    options: list[str] = field(default_factory=list)  # набор допустимых значений
    axis_mode: str = "choose_one"  # choose_one | fulfill_set
    importance: int = 50
    importance_reason: str = ""
    maps_to: str = ""     # "color" | "material" | "" — критерий про фиксированное
                           # поле карточки (цвет/материал), не про атрибут-строку.
                           # Решает модель шага 1, не подстрока в названии критерия.

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


_NUMBER_RE = _re.compile(r"-?\d+(?:[.,]\d+)?")
# Цифры нужны в хвосте единицы измерения («г/м2», «m3»), не только буквы и
# верхние индексы («г/м²») — раньше без них «г/м2» обрезался до «г/м» ещё на
# этапе извлечения, и словарь единиц (_UNIT_ALIASES) не мог его распознать.
_UNIT_TAIL_RE = _re.compile(r"[a-zа-я0-9²³%/]+", _re.I)


def _numeric_from_text(text: str):
    """Первое число в строке + короткий текстовый хвост как единица измерения
    (без перевода — единица берётся как есть, сравнивается через
    _units_compatible). Хвост возвращается «сырым» (только приведён к
    нижнему регистру) — верхние индексы ²/³ и разделитель «/» должны
    дожить до сравнения единиц, _norm_label их бы вырезал раньше времени."""
    match = _NUMBER_RE.search(_cell(text))
    if not match:
        return None, ""
    number = _decimal(match.group(0))
    if number is None:
        return None, ""
    tail = _cell(text)[match.end():match.end() + 12]
    unit_match = _UNIT_TAIL_RE.search(tail)
    return number, unit_match.group(0).lower().strip() if unit_match else ""


# Единицы измерения, реально встречающиеся в каталоге. Один физический
# смысл — один канонический ключ; варианты написания (кириллица/латиница,
# «²»/«2», с пробелом/слитно) сводятся к нему заранее, до сравнения. Это
# СИД по умолчанию — работает даже без единой строки в базе (свежая
# установка, тесты); таблица UnitAlias (админка, см. tenders/admin.py)
# добавляет новые написания поверх этого без деплоя кода, см.
# _unit_aliases() ниже. Единица не из объединённого словаря сравнивается
# как раньше — обычным текстом через _norm_label, без попытки угадать.
#
# Плотность площади (г/м²) и плотность объёма (г/м³) — РАЗНЫЕ величины,
# нарочно разные ключи: раньше _norm_label вырезал «²»/«³» как «непонятные»
# символы, и обе схлопывались в одну и ту же строку «г м» — критерий про
# площадную плотность ткани мог совпасть с атрибутом про объёмную
# плотность совершенно другого физического смысла.
_UNIT_ALIAS_SEED = {
    "г/м2": "г/м²", "г/м²": "г/м²", "гм2": "г/м²", "гм²": "г/м²", "г м2": "г/м²", "г м²": "г/м²",
    "g/m2": "г/м²", "g/m²": "г/м²", "gsm": "г/м²",
    "г/м3": "г/м³", "г/м³": "г/м³", "гм3": "г/м³", "гм³": "г/м³", "г м3": "г/м³", "г м³": "г/м³",
    "g/m3": "г/м³", "g/m³": "г/м³",
    "мм": "мм", "mm": "мм",
    "см": "см", "cm": "см",
    "мг": "мг", "mg": "мг",
    "кг": "кг", "kg": "кг",
    "мл": "мл", "ml": "мл",
    "лм": "лм", "lm": "лм",
    "кд": "кд", "cd": "кд",
}

# Кэш объединённого словаря (сид + таблица) в памяти процесса — не тот же
# класс риска, что смысловой индекс каталога (docs, §10 п.7): здесь десятки
# коротких строк на весь каталог, а не сотни МБ на 71 тыс. товаров, и кэш
# сбрасывается сразу при изменении таблицы (сигнал ниже), а не по времени и
# не растёт сам по себе — тот же паттерн, что уже используется в
# gateway_budget (баланс/список моделей шлюза).
_unit_alias_cache: dict = {"value": None}


def _unit_aliases() -> dict:
    if _unit_alias_cache["value"] is None:
        merged = dict(_UNIT_ALIAS_SEED)
        merged.update({row.spelling: row.canonical for row in UnitAlias.objects.all()})
        _unit_alias_cache["value"] = merged
    return _unit_alias_cache["value"]


def _reset_unit_alias_cache(**kwargs) -> None:
    _unit_alias_cache["value"] = None


post_save.connect(_reset_unit_alias_cache, sender=UnitAlias, dispatch_uid="reset_unit_alias_cache_on_save")
post_delete.connect(_reset_unit_alias_cache, sender=UnitAlias, dispatch_uid="reset_unit_alias_cache_on_delete")


def _canonical_unit(unit: str) -> str:
    """Единица к каноническому виду: сначала точный словарь известных
    вариантов написания (сид + таблица UnitAlias, без потери «²»/«³»),
    иначе — обычная текстовая нормализация как раньше (не пытаемся
    угадывать то, чего нет в словаре)."""
    raw = _cell(unit).strip().lower().replace(" ", "")
    aliases = _unit_aliases()
    if raw in aliases:
        return aliases[raw]
    return _norm_label(unit)


def _units_compatible(required_unit: str, offered_unit: str) -> bool:
    """Пусто с любой стороны — считаем совместимым (не гадаем перевод единиц).
    Обе заданы — должны совпасть после приведения к каноническому виду:
    перевода между РАЗНЫМИ единицами по-прежнему нет, только распознавание
    разных написаний одной и той же (см. _UNIT_ALIASES)."""
    a, b = _canonical_unit(required_unit), _canonical_unit(offered_unit)
    return not a or not b or a == b


def _attribute_numeric_value(attributes, concept_tokens: set, required_unit: str, *, discovered_name: str = ""):
    """Число, отвечающее на критерий, среди характеристик карточки/товара —
    без гадания единиц измерения и без домысливания одного случайного
    совпадения. `discovered_name` — атрибут, который агент уже сопоставил
    этому критерию на предыдущей волне (шаг 6) — сверяется первым и без
    требования пересечения слов, раз агент уже прочитал карточку и решил.

    Возвращает (число, имя_атрибута) или (None, "") — молчание оставляет
    строку агенту, никогда не подставляет то, чего агент бы не подтвердил."""
    matches = []
    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        name = _cell(attribute.get("name"))
        if not name:
            continue
        hinted = bool(discovered_name) and _norm_label(name) == _norm_label(discovered_name)
        if not hinted and not (concept_tokens & _meaningful_tokens(name)):
            continue
        number, unit = _numeric_from_text(attribute.get("value"))
        if number is None or not _units_compatible(required_unit, unit):
            continue
        matches.append((number, name, hinted))
    if not matches:
        return None, ""
    hinted_matches = [entry for entry in matches if entry[2]]
    if hinted_matches:
        return hinted_matches[0][0], hinted_matches[0][1]
    distinct_values = {entry[0] for entry in matches}
    if len(distinct_values) > 1:
        return None, ""  # несколько атрибутов с разными числами — неоднозначно, агенту
    return matches[0][0], matches[0][1]


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
        step_settings: dict | None = None,
        max_cost_rub: float = 0,
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
        self.step_settings = text_search_settings({"steps": step_settings if isinstance(step_settings, dict) else {}})["steps"]
        self.max_cost_rub = max(0, float(max_cost_rub or 0))

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
        self.deadline = None
        # Атрибут карточки, который агент сам сопоставил критерию без
        # совпадения слов (шаг 6) — используется детерминированной проверкой
        # (шаг 5) для последующих волн ТОЙ ЖЕ карточки в этом прогоне.
        # Не сохраняется между прогонами и позициями (см. docs).
        self._discovered_attrs: dict[int, str] = {}
        self._row_of: dict[int, int] = {}

    def _checked_rows(self):
        """Отмеченные критерии + текст строк чек-листа, единая нумерация 1..N
        для всего прогона (шаги 5, 6 и кэш вердикта используют одну и ту же)."""
        checked = [c for c in self.tz if c.checked]
        rows = [c.as_row() for c in checked]
        self._row_of = {id(c): i for i, c in enumerate(checked, 1)}
        return checked, rows

    def _remaining_timeout(self, default):
        if not self.deadline:
            return default
        remaining = self.deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("Достигнут лимит времени прогона")
        return max(1, min(default, remaining))

    def _call_ai(self, prompt, *, max_tokens, timeout, model, images=None):
        if self.max_cost_rub:
            from .gateway_budget import spend_rub
            spent = sum(spend_rub(usage, used_model) or 0 for used_model, usage in self.usage_by_model.items())
            estimate = spend_rub({"prompt_tokens": max(1, len(prompt) // 3), "completion_tokens": max_tokens}, model) or 0
            if spent + estimate > self.max_cost_rub:
                raise RuntimeError(f"Следующий вызов может превысить лимит {self.max_cost_rub:g} ₽")
        try:
            return _ai_json(
                prompt, max_tokens=max_tokens, timeout=self._remaining_timeout(timeout), model=model, images=images,
            )
        except Exception as exc:
            # Даже проваленный вызов мог реально потратить токены (JSON не
            # распарсился обеими попытками, например) — иначе шаг уходит на
            # fallback, а лаборатория показывает 0 ₽ вместо реального
            # расхода (см. _ai_error в services.py).
            usage = getattr(exc, "usage", None)
            if usage:
                self._add_usage(usage, model)
            raise

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
        """Сырые строки ТЗ → нормализованные критерии. Название товара и
        поисковые фразы принадлежат шагу 2 и здесь не вычисляются."""
        rows = self._raw_requirement_rows()
        self._tz_hash = hashlib.sha1(
            json.dumps(
                [_cell(self.line.get("name"))] + [[_cell(r.get("label")), _cell(r.get("value"))] for r in rows],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        settings = self.step_settings.get("1", {})
        model = _selected_model(settings.get("model"), _STRONG_MODEL)
        use_cache = settings.get("cache", "yes") != "no"
        # Версия в ключе кэша — не косметика: 16.09.2026 два прогонных фикса
        # подряд казались «без изменений», хотя код реально менялся — старая
        # запись под тем же tz_hash+моделью отдавалась вечно, ни разу не
        # вызывая модель заново. Любая правка промпта/разбора шага 1 ОБЯЗАНА
        # поднимать эту версию, иначе результат невозможно будет проверить
        # без ручной чистки CascadeCache.
        criteria_cache_key = f"criteria-v3|{self._tz_hash}|{model}"
        cached = _cache_get("criteria", criteria_cache_key) if use_cache else None
        if cached:
            self._load_step1(cached, rows)
            self.diagnostics["tz_cache_hit"] = True
            return self.tz

        if not rows:
            self.tz = []
            return self.tz

        numbered = "\n".join(
            f"{i}. {_cell(r.get('label'))}: {_cell(r.get('value'))}" for i, r in enumerate(rows, 1)
        )
        prompt = f"""Ты нормализуешь критерии ТЗ тендера для последующей проверки товара.

Позиция: {_cell(self.line.get('name'))[:300]}
Строки ТЗ:
{numbered}

Верни только JSON. criteria — ОБЪЕКТ, а не массив: ключ — номер строки СТРОГО из списка выше ("1", "2", ...), значение — разбор этой строки:
{{"criteria":{{"1":{{"concept":"о чём строка, своими словами","operator":">=|<=|=|!=|~|in","value":"...","unit":"...","keep":true,"importance":80,"importance_reason":"почему важно","axis":"","axis_mode":"choose_one|fulfill_set","num_min":null,"num_max":null,"options":[],"maps_to":""}}}}}}

Правила:
- criteria: РОВНО один ключ на каждую пронумерованную строку ТЗ, без пропусков. Ключ — это номер строки, к которой относится разбор именно её содержимого, а не порядковый номер по счёту заполнения. Даже если две строки означают одно и то же по смыслу (например «синий» и «цвет: синий») — НЕ объединяй их в одну запись, верни для каждой свой ключ с одинаковым разобранным значением. Каждое значение должно описывать ИМЕННО ту строку, чей номер стоит ключом — не соседнюю. Характеристикой может быть ЛЮБОЕ измеримое или описываемое свойство товара — не ограничивайся заранее известным списком (плотность, яркость, мощность, температура, вязкость — что угодно).
- keep=false для строк, которые НЕ признак готового товара: маркировка (Честный Знак, ЦРПТ), требования к пошиву и швам, макет и расположение логотипа, бумажные документы, сроки, гарантия. Физические свойства (материал, размер, цвет, конструкция, интерфейс, любые технические параметры) — keep=true.
- axis: короткое слово-код латиницей, если это то, чем отличаются варианты ОДНОГО товара — то есть по этому признаку у одного и того же артикула бывает несколько версий на выбор (ёмкость памяти, объём, размер одежды, мощность инструмента — что угодно, список не фиксирован). Иначе "".
- axis_mode: "choose_one", когда для заказа выбирается одно значение оси; "fulfill_set", когда заказ собирается из нескольких вариантов (например, размерный ряд).
- importance: 0..100 — насколько критерий влияет на пригодность товара. importance_reason — короткое объяснение.
- num_min/num_max: для ЛЮБОГО критерия с числовой границей. Для оси "capacity" (объём памяти/накопителя) переведи в МБ (32 ГБ → 32768). Для оси "volume" (объём жидкости/тары) переведи в мл (0,5 л → 500). Для остальных числовых критериев — оставь число как есть, единицу измерения запиши в unit, ничего не пересчитывай (плотность «140 г/м²» → num_min:140, unit:"г/м²"). «Не менее/от/минимум» → num_min, «не более/до/максимум» → num_max, диапазон → оба, конкретное число без оговорок → оба равны значению. Нечисловой критерий или число не назвали — null.
- options: список допустимых значений, если требование перечислением (размеры «M, L, XL»; несколько цветов). Иначе [].
- maps_to: "color", если критерий про цвет товара целиком (не про цвет логотипа/принта/упаковки); "material", если критерий про материал/состав товара целиком. Иначе "".
"""
        try:
            # Каждый критерий — это ~14 полей JSON (concept/operator/value/
            # unit/keep/importance/importance_reason/axis/axis_mode/num_min/
            # num_max/options/maps_to + n) — старый бюджет (250 + 150×строк)
            # был рассчитан по дефолтной модели, которая отвечает лаконично
            # и строго по формату. Модели, которые добавляют рассуждение
            # перед JSON или менее компактны, упирались в лимит на середине
            # ответа — JSON обрывался и не парсился (см. разбор 14.09.2026).
            result, usage = self._call_ai(
                prompt, max_tokens=max(1500, min(8000, 400 + len(rows) * 300)), timeout=50, model=model,
            )
            self._add_usage(usage, model)
            if not isinstance(result, dict) or not isinstance(result.get("criteria"), (dict, list)):
                raise ValueError("Агент не вернул критерии ТЗ")
        except Exception as exc:
            logger.exception("Cascade step 1 failed")
            self.error = _cell(exc)[:200]
            self.tz = [self._fallback_criterion(r) for r in rows]
            return self.tz

        payload = self._parse_step1(result, rows)
        if use_cache:
            _cache_put("criteria", criteria_cache_key, payload)
        self._load_step1(payload, rows)
        return self.tz

    def _parse_step1(self, result, rows) -> dict:
        result = result if isinstance(result, dict) else {}
        raw_criteria = result.get("criteria")
        if isinstance(raw_criteria, dict):
            # Основной формат: объект, ключ — номер строки ТЗ. Раньше просили
            # массив + отдельное поле "n" (порядковый номер по счёту модели)
            # или доверяли порядку эмиссии — на реальных прогонах оба сигнала
            # регулярно расходились с реальным содержимым: на длинном списке
            # модель то теряет счёт в "n", то (даже когда количество
            # элементов совпадает один в один) переставляет сами записи
            # местами по смыслу — доказано на живом кэше 14.09.2026, где
            # запись №2 содержала разбор совсем другой строки (плотности
            # вместо метода нанесения) при полном совпадении длины. Когда
            # номер строки — это ключ объекта, а не отдельный счётчик или
            # позиция, модели физически нечего перепутать между "куда писать"
            # и "что писать".
            ordered = [raw_criteria.get(str(i)) if isinstance(raw_criteria.get(str(i)), dict) else {}
                       for i in range(1, len(rows) + 1)]
            matched = sum(1 for entry in ordered if entry)
            if matched != len(rows):
                self.diagnostics["step1_count_mismatch"] = {"rows": len(rows), "criteria": matched}
        elif isinstance(raw_criteria, list):
            # Модель проигнорировала формат объекта и всё равно вернула
            # массив — резерв на основе тех же эвристик, что были основными
            # до перехода на объект: порядок при точном совпадении длины,
            # иначе поле "n" (см. историю в git).
            valid_entries = [entry for entry in raw_criteria if isinstance(entry, dict)]
            if len(valid_entries) == len(rows):
                ordered = valid_entries
            else:
                self.diagnostics["step1_count_mismatch"] = {"rows": len(rows), "criteria": len(valid_entries)}
                by_n = {}
                for entry in valid_entries:
                    try:
                        n = int(entry.get("n"))
                    except (TypeError, ValueError):
                        continue
                    by_n[n] = entry
                ordered = [by_n.get(i, {}) for i in range(1, len(rows) + 1)]
        else:
            ordered = [{} for _ in rows]
            if rows:
                self.diagnostics["step1_count_mismatch"] = {"rows": len(rows), "criteria": 0}
        criteria = []
        for row, entry in zip(rows, ordered):
            # axis — свободный код оси, не закрытый список: модель сама решает,
            # чем отличаются варианты ЭТОГО товара, а не только капасити/объём/размер.
            axis = _norm_label(entry.get("axis"))[:24]
            options = [
                _cell(v)[:60] for v in (entry.get("options") if isinstance(entry.get("options"), list) else [])
                if _cell(v)
            ][:12]
            maps_to = _cell(entry.get("maps_to")).lower()
            maps_to = maps_to if maps_to in {"color", "material"} else ""
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
                "axis_mode": "fulfill_set" if _cell(entry.get("axis_mode")) == "fulfill_set" else "choose_one",
                "importance": max(0, min(100, int(entry.get("importance") or 50))),
                "importance_reason": _cell(entry.get("importance_reason"))[:160],
                "maps_to": maps_to,
            })
        return {"criteria": criteria}

    def _load_step1(self, payload, rows) -> None:
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
                axis_mode="fulfill_set" if entry.get("axis_mode") == "fulfill_set" else "choose_one",
                importance=max(0, min(100, int(entry.get("importance") or 50))),
                importance_reason=_cell(entry.get("importance_reason"))[:160],
                maps_to=entry.get("maps_to") if entry.get("maps_to") in {"color", "material"} else "",
            ))
        limit = max(0, min(100, int(self.step_settings.get("1", {}).get("max_active_requirements", 0) or 0)))
        if limit:
            explicit_labels = {
                _norm_label(row.get("label")) for row in rows
                if isinstance(row, dict) and row.get("selected") is True
            }
            explicit_count = sum(c.checked and _norm_label(c.label) in explicit_labels for c in self.tz)
            candidates = [c for c in self.tz if c.checked and _norm_label(c.label) not in explicit_labels]
            selected = {id(c) for c in sorted(candidates, key=lambda c: -c.importance)[:max(0, limit - explicit_count)]}
            for criterion in candidates:
                if id(criterion) not in selected:
                    criterion.checked = False
                    criterion.importance_reason = criterion.importance_reason or f"Не вошло в лимит {limit}"
            self.diagnostics["active_requirement_limit"] = limit

    def _fallback_criterion(self, row) -> Criterion:
        return Criterion(
            label=_cell(row.get("label"))[:200], raw_value=_cell(row.get("value"))[:500],
            concept=_cell(row.get("label"))[:120], operator="~", value=_cell(row.get("value"))[:200],
            checked=_norm_label(row.get("label")) not in self.skip_labels and row.get("selected") is not False,
        )

    # -- шаг 2: план поиска --------------------------------------------- #
    def step_2_search_plan(self) -> list[str]:
        """Сырое название позиции → чистый вид товара и поисковые фразы."""
        settings = self.step_settings.get("2", {})
        maximum = max(1, min(40, int(settings.get("max_phrases", 24))))
        minimum = max(1, min(maximum, int(settings.get("min_phrases", 12))))
        model = _selected_model(settings.get("model"), _TITLE_MODEL)
        use_cache = settings.get("cache", "yes") != "no"
        raw_name = _cell(self.line.get("name"))[:300]
        cache_key = hashlib.sha1(f"title-v1|{model}|{raw_name}".encode("utf-8")).hexdigest()
        payload = _cache_get("searchplan", cache_key) if use_cache else None

        if not self._valid_search_plan(payload):
            prompt = f"""Ты готовишь поиск одного товара в каталогах поставщиков.

Исходное название позиции: {raw_name}

Верни только JSON:
{{"item":"чистое конкретное название товара, 1-3 слова","queries":["от {minimum} до {maximum} вариантов названия этого же товара"]}}

Убери канцелярские и закупочные слова, упоминания заказчика, символики, логотипа, изготовления и поставки.
В queries дай синонимы, разговорные названия, английские варианты и альтернативные написания только этого вида товара.
Не добавляй характеристики: цвет, материал, объём, размер и способ нанесения.
Не переходи к соседнему товару: флешка не карта памяти, шопер не рюкзак.
Не создавай бессмысленные дубли только ради количества.
"""
            try:
                raw, usage = self._call_ai(
                    prompt, max_tokens=max(300, min(1000, 120 + maximum * 28)), timeout=35, model=model,
                )
                self._add_usage(usage, model)
                if not self._valid_search_plan(raw):
                    raise ValueError("Агент не вернул чистое название и поисковые фразы")
                payload = {"item": _cell(raw["item"])[:120], "queries": list(raw["queries"])}
                if use_cache:
                    _cache_put("searchplan", cache_key, payload)
            except Exception as exc:
                logger.exception("Cascade step 2 failed")
                self.error = _cell(exc)[:200]
                self.diagnostics["search_plan_error"] = self.error
                payload = {"item": raw_name[:120], "queries": [raw_name] if raw_name else []}
        else:
            self.diagnostics["search_plan_cache_hit"] = True

        self.item = _cell(payload.get("item"))[:120]
        self.queries = list(payload.get("queries", []))
        phrases, seen = [], set()
        for value in [self.item, *self.queries]:
            text = _text(value, 150)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                phrases.append(text)
        phrases = phrases[:maximum]
        self.queries = list(phrases)
        self.diagnostics["query_phrases"] = phrases
        self.diagnostics["query_phrase_limits"] = {
            "minimum": minimum,
            "maximum": maximum,
            "actual": len(phrases),
            "minimum_met": len(phrases) >= minimum,
        }
        self._pull_lessons()
        return phrases

    @staticmethod
    def _valid_search_plan(payload) -> bool:
        if not isinstance(payload, dict):
            return False
        item = _cell(payload.get("item"))
        queries = payload.get("queries")
        return bool(item and len(item.split()) <= 5 and isinstance(queries, list) and any(_cell(v) for v in queries))

    # -- шаг 3: поиск по названиям ------------------------------------- #
    def step_3_search_by_name(self, phrases) -> list:
        pool = []
        sources = self.step_settings.get("3", {}).get("sources", "all")
        if sources in {"all", "oasis"} and CatalogProduct.objects.filter(supplier__code="oasis", is_active=True).exists():
            self._oasis_mirror = True
            oasis = _aggregate_color_variants(_text_search_pool("oasis", phrases), "oasis")
            pool.extend(oasis)
            self.sources["oasis"] = {"status": "success", "received": len(oasis)}
        else:
            self.sources["oasis"] = {"status": "disabled" if sources == "gifts" else "not_configured"}
        if sources in {"all", "gifts"} and CatalogSupplier.objects.filter(code="gifts", is_active=True).exists():
            gifts = _aggregate_color_variants(_text_search_pool("gifts", phrases), "gifts")
            pool.extend(gifts)
            self.sources["gifts"] = {"status": "success", "received": len(gifts)}
        else:
            self.sources["gifts"] = {"status": "disabled" if sources == "oasis" else "not_configured"}
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

        settings = self.step_settings.get("4", {})
        intensity = settings.get("intensity", "cautious")
        if intensity == "off":
            self.diagnostics["name_filter"] = "disabled"
            self.diagnostics["name_filter_removed"] = 0
            return pool
        model = _selected_model(settings.get("model"), _FAST_MODEL)
        use_cache = settings.get("cache", "yes") != "no"

        ids = sorted(str(p.external_id) for p in pool)
        cache_variant = "" if model == _FAST_MODEL and intensity == "cautious" else f"|{model}|{intensity}"
        key = hashlib.sha1(
            (_norm_label(self.item) + cache_variant + "|" + "|".join(ids)).encode("utf-8")
        ).hexdigest()
        cached = _cache_get("namefilter", key) if use_cache else None
        if cached and isinstance(cached.get("keep"), list):
            keep = {str(v) for v in cached["keep"]}
            self.diagnostics["name_filter"] = "cache"
        else:
            id_names = [(p.external_id, p.full_name or p.name) for p in pool]
            nf_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            keep = _run_name_filter(
                self.item or _cell(self.line.get("name")), id_names,
                usage=nf_usage, model=model, intensity=intensity,
            )
            self._add_usage(nf_usage, model)
            if keep is None:
                self.diagnostics["name_filter"] = "skipped"
                return pool
            keep = {str(v) for v in keep}
            if use_cache:
                _cache_put("namefilter", key, {"keep": sorted(keep)})
        kept = [p for p in pool if str(p.external_id) in keep]
        self.diagnostics["name_filter_removed"] = len(pool) - len(kept)
        return kept

    # -- шаг 5: цвет + остаток + схлопывание + проверка критериев ----- #
    def step_5_hard_gates_and_collapse(self, pool) -> list[dict]:
        """Жёсткие фильтры (цвет/остаток) и схлопывание по семьям — как
        было; плюс детерминированная проверка каждого отмеченного критерия:
        то, что код может решить сам (числа, цвет, материал, размерный
        ряд), решается здесь. Что не решилось — остаётся строкой
        `matrix[].source == "not_checked"` и уходит на шаг 6, никогда не
        подставляется вердикт, которого код не может обосновать."""
        settings = self.step_settings.get("5", {})
        checked, rows = self._checked_rows()
        colour = next((c for c in checked if c.maps_to == "color"), None)
        survivors = []
        expanded = []
        for representative in pool:
            variants = getattr(representative, "_variant_products", None) or [representative]
            for product in variants:
                product._relevance = getattr(representative, "_relevance", getattr(product, "_relevance", 1))
                expanded.append(product)
        for product in expanded:
            if settings.get("color_filter", "family") != "off" and colour and self._colour_conflict(product, colour.value):
                continue
            transit = max(0, int(getattr(product, "stock_transit", 0) or 0))
            stock_policy = settings.get("stock_policy", "available")
            if stock_policy == "available" and self.quantity > 0 and product.total_stock <= 0 and transit <= 0 and not product.is_on_order:
                continue
            if stock_policy == "enough" and self.quantity > 0 and product.total_stock + transit < self.quantity and not product.is_on_order:
                continue
            survivors.append(product)

        # Ось варианта — любой критерий, который шаг 1 назвал осью; список
        # не ограничен заранее (ёмкость/объём — по названию, остальное — по
        # характеристикам SKU, см. _axis_value).
        axis_criteria = [c for c in checked if c.axis]
        groups: dict[str, list] = {}
        for product in survivors:
            groups.setdefault(product.family_key or product.group_id or product.external_id, []).append(product)

        # Требование к размерному ряду — это ось с перечислимыми значениями
        # (options), а не числовая (num_min/num_max — та про ёмкость/объём/
        # прочие числа). Если такого критерия среди отмеченных нет, размер
        # никто не спрашивал — показывать конкретный «размер S» как ответ
        # вводит в заблуждение (это не выбранный вариант, а случайно первый
        # по цене/совпадению слов среди всей группы).
        size_required = any(c.options and c.num_min is None and c.num_max is None for c in checked)

        tolerance = Decimal(str(max(0, min(50, int(settings.get("tolerance_percent", 0)))))) / 100
        prefill_on = settings.get("numeric_prefill", "yes") != "no"
        cards = []
        for skus in groups.values():
            fitting = self._variants_fitting_axes(skus, axis_criteria, tolerance)
            face = max(
                fitting or skus,
                key=lambda p: (getattr(p, "_name_hits", 0), -(p.effective_price or Decimal("Infinity"))),
            )
            card = self._serialize(face, skus)
            if len(skus) > 1 and not size_required:
                # Семья из нескольких размеров, но размер не был требованием —
                # не выдаём конкретный размер представителя за ответ (тот же
                # приём, что уже применяется при схлопывании по цвету на шаге 3).
                card["name"] = _SIZE_SUFFIX_RE.sub("", card["name"]).rstrip(" ,")
            card["eligible_variant_ids"] = [product.external_id for product in fitting] if fitting else []
            self._init_unknown(card, rows)
            if prefill_on:
                self._prefill_card(card, checked, rows, tolerance)
                # _prefill_card заполняет клетки через _apply_cell напрямую —
                # matrix_status/match_count и т.п., выставленные _init_unknown
                # ДО префилла, иначе остаются как «ничего не проверено» даже
                # если код только что закрыл все строки. Без этого пересчёта
                # шаг 6 не видит, что карточка уже complete, и тратит вызов
                # агента на то, что код уже полностью решил.
                self._recompute_card_summary(card)
            cards.append(card)
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

    def _variants_fitting_axes(self, skus, criteria, tolerance) -> list:
        """SKU группы, которые НЕ нарушают ни один критерий-ось (молчащие по
        оси — не нарушают). Пусто → вызывающий берёт всю группу."""
        if not criteria:
            return []
        fitting = []
        for product in skus:
            ok = True
            for crit in criteria:
                value = self._axis_value(product, crit)
                if value is not None:
                    if crit.num_min is not None and value < crit.num_min * (1 - tolerance):
                        ok = False
                    if crit.num_max is not None and value > crit.num_max * (1 + tolerance):
                        ok = False
                if crit.options and crit.num_min is None and crit.num_max is None:
                    label = _variant_size(product)
                    if label:
                        offered, required = {_normalized(label)}, {_normalized(o) for o in crit.options}
                        satisfied = (offered >= required) if crit.axis_mode == "fulfill_set" else bool(offered & required)
                        if not satisfied:
                            ok = False
            if ok:
                fitting.append(product)
        return fitting

    def _axis_value(self, product, crit: Criterion):
        """Значение оси у конкретного SKU: ёмкость/объём — по названию
        (быстрый и точный способ для этих двух, см. docs); любая другая
        числовая ось — по совпадающей характеристике самого SKU. Молчит
        товар — None, вызывающий это не считает нарушением."""
        if crit.axis == "capacity":
            return _capacity_mb(f"{product.name} {product.full_name or ''} {product.size or ''}")
        if crit.axis == "volume":
            return _volume_ml(f"{product.name} {product.full_name or ''} {product.size or ''}")
        if crit.num_min is None and crit.num_max is None:
            return None
        number, _name = _attribute_numeric_value(
            product.attributes if isinstance(product.attributes, list) else [],
            _meaningful_tokens(f"{crit.concept} {crit.label}"), crit.unit,
            discovered_name=self._discovered_attrs.get(self._row_of.get(id(crit)), ""),
        )
        return number

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

    # -- шаг 6: умный агент — только по строкам, не закрытым шагом 5 --- #
    def step_6_agent_matrix(self, cards) -> list[dict]:
        """Шаг 5 уже решил кодом то, что мог; сюда попадают только строки с
        `matrix[].source == "not_checked"`. Карточка, полностью решённая
        шагом 5, в агент вообще не идёт — сразу в `settled`."""
        settings = self.step_settings.get("6", {})
        use_cache = settings.get("cache", "yes") != "no"
        checked, rows = self._checked_rows()
        self.feedback_instructions_result = []

        todo, settled = [], []
        for card in cards:
            if rows and card.get("matrix_status") != "complete" and use_cache:
                cached = _cache_get("verdict", self._verdict_cache_key(card["id"]))
                raw_grid = cached.get("grid", {}) if isinstance(cached, dict) else {}
                cells = {
                    index: tuple(raw_grid[str(index)])
                    for index in range(1, len(rows) + 1)
                    if isinstance(raw_grid, dict) and isinstance(raw_grid.get(str(index)), (list, tuple))
                }
                if self._complete_grid(cells, rows):
                    for index, (verdict, reason) in cells.items():
                        self._apply_cell(card, rows, index, verdict, reason, "cache")
                    self._recompute_card_summary(card)
                    self.diagnostics["verdict_cache_hits"] += 1
            (settled if card.get("matrix_status") == "complete" else todo).append(card)

        if todo:
            self._grade_bounded(todo, checked, rows, settled=settled, use_cache=use_cache)

        if self.feedback_instructions:
            self._classify_feedback(cards, rows)
        return cards

    def _verdict_cache_key(self, card_id):
        settings = self.step_settings.get("6", {})
        model = _selected_model(settings.get("model"), _AGENT_MODEL)
        parts = []
        if model != _AGENT_MODEL:
            parts.append(model)
        if self.step_settings.get("5", {}).get("numeric_prefill", "yes") == "no":
            parts.append("no-prefill")
        suffix = "|" + "|".join(parts) if parts else ""
        return f"{self._tz_hash}|{card_id}{suffix}"

    def _init_unknown(self, card, rows) -> None:
        card["matrix"] = [
            {"criterion": label, "required": value, "verdict": "not_checked", "reason": "", "source": "not_checked"}
            for label, value in rows
        ]
        card.pop("_ai_graded", None)
        self._recompute_card_summary(card)

    @staticmethod
    def _apply_cell(card, rows, row_idx, verdict, reason, source) -> None:
        """Одна клетка. Код никогда не переписывает уже решённую им клетку —
        ни агент, ни кэш её не перебивают (см. docs, «последнее слово за
        кодом»). Итоги (`matches`/счётчики/`matrix_status`) не пересчитывает —
        это делает `_recompute_card_summary` один раз после пачки правок."""
        if not (1 <= row_idx <= len(rows)):
            return
        entry = card["matrix"][row_idx - 1]
        if entry["source"] == "code":
            return
        entry["verdict"] = {"y": "yes", "n": "no", "m": "unknown"}.get(verdict, "not_checked")
        entry["reason"] = reason or ("Нет данных в карточке" if verdict == "m" else "")
        entry["source"] = source

    @staticmethod
    def _recompute_card_summary(card) -> None:
        """`unknown`/`unknown_count` считают и «проверили — данных нет» (m),
        и «ещё не проверяли» (not_checked) одинаково — с точки зрения того,
        насколько можно доверять карточке, разницы для читателя нет, обе
        значат «неизвестно». `matrix_status`/`pending` — отдельно, честно
        отличают «не пытались» от «не полностью ответили»; `fit` не станет
        "exact" ни для одной из них."""
        matches, mismatches, unknown, pending = [], [], [], 0
        for entry in card["matrix"]:
            tail = f" — {entry['reason']}" if entry["reason"] else ""
            if entry["verdict"] == "yes":
                matches.append(f"{entry['criterion']}: {entry['required']}{tail}")
            elif entry["verdict"] == "no":
                mismatches.append(f"{entry['criterion']}: требуется {entry['required']}{tail}")
            elif entry["verdict"] == "unknown":
                unknown.append(f"{entry['criterion']}{tail or ' — нет данных в карточке'}")
            else:
                unknown.append(entry["criterion"])
                pending += 1
        card["matches"], card["mismatches"], card["unknown"] = matches, mismatches, unknown
        card["match_count"], card["mismatch_count"], card["unknown_count"] = len(matches), len(mismatches), len(unknown)
        card["matrix_status"] = "complete" if pending == 0 else ("pending" if pending == len(card["matrix"]) else "incomplete")
        card["fit"] = "exact" if card["matrix_status"] == "complete" and not mismatches and not unknown else "partial"

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
    def _preagent_key(card):
        price = _decimal(card.get("price"))
        mismatches = sum(1 for entry in card["matrix"] if entry["verdict"] == "no")
        matches = sum(1 for entry in card["matrix"] if entry["verdict"] == "yes")
        return (card.get("relevance", 1), mismatches, -matches, price if price is not None else Decimal("Infinity"))

    def _grade_bounded(self, todo, checked, rows, *, settled=(), use_cache=True) -> None:
        """Ограничивает новые проверки; уже решённые шагом 5/кэшем карточки
        участвуют в условии остановки. matrix_status — контракт с шагом 7.
        Полный ответ «m» отличается от пропущенной клетки и может кэшироваться.
        """
        settings = self.step_settings.get("6", {})
        ranked = sorted(todo, key=self._preagent_key)
        first = max(1, min(75, int(settings.get("first_batch", os.getenv("CASCADE_STEP6_FIRST", "25")))))
        ceiling = max(0, min(100, int(settings.get("ceiling", os.getenv("CASCADE_STEP6_CEILING", "75")))))
        model = _selected_model(settings.get("model"), _AGENT_MODEL)
        suitable = sum(self._suitable_for_stop(card) for card in settled)
        verified = list(settled)
        diagnostics = {
            "pool": len(todo), "graded": 0, "batches": 0, "cached": len(settled),
            "complete": len(settled), "suitable": suitable,
            "code_prefilled_cells": sum(
                1 for card in (*todo, *settled) for entry in card["matrix"] if entry["source"] == "code"
            ),
            "wave_size": first, "ai_ceiling": ceiling,
        }
        self.diagnostics["step6"] = diagnostics
        stop_reason = "exhausted"
        for offset in range(0, min(len(ranked), ceiling), first):
            if self._safe_early_stop(verified, ranked[offset:]):
                break
            batch = ranked[offset:min(offset + first, ceiling)]
            # Открытие атрибута с прошлой волны (см. _apply_discovered) может
            # закрыть карточку кодом ещё ДО того, как до неё дошла очередь —
            # тогда агента по ней уже не зовём вовсе.
            need_agent = [card for card in batch if card.get("matrix_status") != "complete"]
            grids = self._grade_grid(need_agent, rows, model=model, batch_size=3) if need_agent else {}
            diagnostics["graded"] += len(batch)
            if need_agent:
                diagnostics["batches"] += 1
            for card in batch:
                cid = str(card["id"])
                for row_idx, (verdict, reason) in grids.get(cid, {}).items():
                    self._apply_cell(card, rows, row_idx, verdict, reason, "agent")
                self._recompute_card_summary(card)
                if card["matrix_status"] == "complete":
                    diagnostics["complete"] += 1
                    suitable += self._suitable_for_stop(card)
                    verified.append(card)
                    if use_cache:
                        _cache_put("verdict", self._verdict_cache_key(cid), {
                            "grid": {
                                str(i): [{"yes": "y", "no": "n", "unknown": "m"}.get(entry["verdict"], "m"), entry["reason"]]
                                for i, entry in enumerate(card["matrix"], 1)
                            },
                        })
            # То, что агент по ходу сопоставил критерию сам (поле "a" в
            # клетке) — сразу пробуем на карточках следующей волны кодом,
            # без нового вызова (см. docs, «открытие атрибута»).
            remaining = ranked[offset + len(batch):]
            if remaining and self._discovered_attrs:
                self._apply_discovered(remaining, checked, rows)
            if need_agent and not grids:
                stop_reason = "empty_response"
                break
        diagnostics["suitable"] = suitable
        diagnostics["pending"] = len(todo) - diagnostics["graded"]
        if self._safe_early_stop(verified, ranked[diagnostics["graded"]:]):
            stop_reason = "enough_suitable"
        elif stop_reason != "empty_response" and diagnostics["pending"]:
            stop_reason = "ceiling"
        diagnostics["stop_reason"] = stop_reason

    def _apply_discovered(self, cards, checked, rows) -> None:
        """Атрибут, который агент назвал для критерия на прошлой волне —
        пробуем повторно кодом на карточках, до которых агент ещё не дошёл.
        Ничего не решает, если атрибута с таким именем на карточке нет."""
        tolerance = Decimal(str(max(0, min(50, int(self.step_settings.get("5", {}).get("tolerance_percent", 0)))))) / 100
        for card in cards:
            changed = False
            for row_idx, attr_name in self._discovered_attrs.items():
                if not (1 <= row_idx <= len(checked)) or card["matrix"][row_idx - 1]["source"] != "not_checked":
                    continue
                crit = checked[row_idx - 1]
                number, found_name = _attribute_numeric_value(
                    card.get("attributes"), set(), crit.unit, discovered_name=attr_name,
                )
                if number is None:
                    continue
                ok = (
                    (crit.num_min is None or number >= crit.num_min * (1 - tolerance))
                    and (crit.num_max is None or number <= crit.num_max * (1 + tolerance))
                )
                self._apply_cell(card, rows, row_idx, "y" if ok else "n", f"{found_name}: {number}", "code")
                changed = True
            if changed:
                self._recompute_card_summary(card)

    def _safe_early_stop(self, verified, remaining):
        """Останавливает ИИ, только если оставшиеся не смогут обойти топ-10."""
        if self.feedback_instructions:
            return False
        exact = [
            card for card in verified
            if card.get("matrix_status") == "complete"
            and card.get("mismatch_count", 0) == 0 and card.get("unknown_count", 0) == 0
        ]
        if len(exact) < 10:
            return False

        def key(card):
            price = _decimal(card.get("price"))
            return card.get("relevance", 1), price if price is not None else Decimal("Infinity")

        boundary = sorted(key(card) for card in exact)[9]
        return all(key(card) >= boundary for card in remaining)

    def _prefill_card(self, card, checked, rows, tolerance) -> None:
        """Шаг 5: то, что код может решить сам, — цвет/материал по спискам
        поставщика, размерный ряд, ось из названия, любое другое число по
        совпадающей характеристике карточки. Сомневается — оставляет строку
        шагу 6, никогда не подставляет вердикт, которого агент бы не дал."""
        for row_idx, crit in enumerate(checked, 1):
            if crit.maps_to == "color":
                offered = ", ".join(_cell(v) for v in (card.get("colors") or []) if _cell(v))
                if offered:
                    verdict = "y" if _colors_compatible(crit.value, offered)[0] else "n"
                    self._apply_cell(card, rows, row_idx, verdict, offered[:90], "code")
                continue
            if crit.maps_to == "material":
                offered = ", ".join(_cell(v) for v in (card.get("materials") or []) if _cell(v))
                required_tokens = _meaningful_tokens(crit.value)
                if offered and required_tokens and required_tokens <= _meaningful_tokens(offered):
                    self._apply_cell(card, rows, row_idx, "y", offered[:90], "code")
                continue
            if crit.options and crit.num_min is None and crit.num_max is None:
                offered = {_normalized(v) for v in (card.get("sizes") or []) if _cell(v)}
                required = {_normalized(v) for v in crit.options if _cell(v)}
                if offered and required:
                    fulfilled = offered >= required
                    matches = fulfilled if crit.axis_mode == "fulfill_set" else bool(offered & required)
                    reason = (
                        "есть весь размерный ряд" if matches and crit.axis_mode == "fulfill_set"
                        else ", ".join(sorted(offered & required)) if matches
                        else "нет: " + ", ".join(sorted(required - offered))
                    )
                    self._apply_cell(card, rows, row_idx, "y" if matches else "n", reason[:90], "code")
                continue
            if crit.num_min is None and crit.num_max is None:
                continue  # не число и не цвет/материал/размер — решает агент
            if crit.axis in {"capacity", "volume"}:
                values = self._card_axis_values(card).get(crit.axis, [])
                if not values:
                    continue  # карточка молчит про ось — пусть судит агент
                fits = [
                    v for v in values
                    if (crit.num_min is None or v >= crit.num_min * (1 - tolerance))
                    and (crit.num_max is None or v <= crit.num_max * (1 + tolerance))
                ]
                if fits:
                    self._apply_cell(card, rows, row_idx, "y", "по варианту", "code")
                else:
                    best = max(values) if crit.num_min is not None else min(values)
                    self._apply_cell(card, rows, row_idx, "n", f"{_axis_short(best, crit.axis)}, нужно {crit.value}", "code")
                continue
            number, attr_name = _attribute_numeric_value(
                card.get("attributes"), _meaningful_tokens(f"{crit.concept} {crit.label}"), crit.unit,
                discovered_name=self._discovered_attrs.get(row_idx, ""),
            )
            if number is None:
                continue  # нет совпадающей характеристики — агенту, без гаданий
            ok = (
                (crit.num_min is None or number >= crit.num_min * (1 - tolerance))
                and (crit.num_max is None or number <= crit.num_max * (1 + tolerance))
            )
            reason = f"{attr_name}: {number}" if attr_name else str(number)
            self._apply_cell(card, rows, row_idx, "y" if ok else "n", reason[:90], "code")

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

    @staticmethod
    def _prefilled_hint(card) -> str:
        done = [str(i) for i, entry in enumerate(card["matrix"], 1) if entry["source"] != "not_checked"]
        return f"\n   Код уже проверил пункты: {', '.join(done)}" if done else ""

    def _grade_grid(self, cards, rows, *, model, batch_size) -> dict:
        """Возвращает {id карточки: {номер строки: (v, w)}}. Не применяет и не
        кэширует — это делает вызывающий. Какие пункты карточки уже закрыты
        шагом 5 — читает из её собственного `matrix`, отдельного списка не
        передают: у разных карточек одной пачки открытые строки различаются."""
        if not cards:
            return {}
        batches = [cards[i:i + batch_size] for i in range(0, len(cards), batch_size)] or [cards]
        cells_by_card: dict[str, dict[int, tuple]] = {}
        errors = []

        def run_batch(indexed):
            _index, batch = indexed
            local = {pos: str(card["id"]) for pos, card in enumerate(batch, 1)}
            cards_text = "\n\n".join(
                f"КАРТОЧКА {pos} | id {card['id']}\n{self._card_brief(card)}{self._prefilled_hint(card)}"
                for pos, card in enumerate(batch, 1)
            )
            prompt = self._step6_prompt(
                rows, cards_text, [],
                batch_ids=", ".join(f"{p}={c['id']}" for p, c in enumerate(batch, 1)) if len(batches) > 1 else "",
            )
            try:
                raw, usage = self._call_ai(prompt, max_tokens=700 + len(batch) * (len(rows) + 2) * 24,
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
                    attr_name = _cell(cell.get("a"))[:60]
                    if attr_name and row not in self._discovered_attrs:
                        self._discovered_attrs[row] = attr_name

        if errors and not cells_by_card:
            self.error = self.error or errors[0]
        return cells_by_card

    def _classify_feedback(self, cards, rows) -> None:
        """Один проход: классифицирует замечания администратора и уроки в
        priority / exclude / keep_only / soften / ranking и раздаёт по
        карточкам. Матрицу ТЗ не трогает."""
        from .services import _shortlist_card_images

        model = _selected_model(self.step_settings.get("6", {}).get("model"), _AGENT_MODEL)
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
                raw, usage = self._call_ai(prompt, max_tokens=400 + len(self.feedback_instructions) * 120,
                                           timeout=60, model=model,
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
            self._add_usage(usage, model)
            for item in raw.get("instructions") if isinstance(raw.get("instructions"), list) else []:
                if isinstance(item, dict):
                    item["cards"] = [local.get(c, c) if isinstance(c, int) else c for c in (item.get("cards") or [])]
                    raw_instructions.append(item)

        self._apply_instructions(raw_instructions, {str(c["id"]): c for c in cards}, bool(errors))

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
                "{\"c\":номер карточки,\"r\":номер пункта,\"v\":\"y|n|m\",\"w\":\"до 6 слов, только для n и m\",\"a\":\"необязательно\"}:\n"
                "- \"y\" — карточка (или её подходящий вариант) соответствует пункту;\n"
                "- \"n\" — в карточке есть данные по пункту и они НЕ совпадают;\n"
                "- \"m\" — в карточке про пункт ничего нет.\n"
                f"Верни все клетки, кроме помеченных «Код уже проверил»: их не повторяй. "
                "Пропущенная непроверенная клетка означает неполный ответ, а не НЗ. \"m\" — только когда данных реально нет.\n"
                "Небольшое отклонение от требуемого числа → \"y\", если по смыслу пункта это разумно (относится к ЛЮБОЙ "
                "числовой характеристике, не только размеру/весу). Заметное, но возможно допустимое → \"m\" («X vs Y, проверить»). "
                "Явно не то → \"n\". Пункт про нижнюю границу («не менее», «от», «минимум») — меньше границы это \"n\", "
                "больше или равно — \"y\". Пункт про верхнюю границу («не более», «до», «максимум») — больше границы это \"n\". "
                "Ёмкость/объём бери из названия варианта. «Флеш-карта USB 2.0» и «USB-флеш-накопитель» — одно и то же. "
                "Если для \"y\"/\"n\" ты нашёл число в КОНКРЕТНОЙ характеристике карточки (не в названии/описании общими "
                "словами) — укажи её точное имя в необязательном поле \"a\": ускорит проверку остальных карточек.\n"
                f"Ответ: {{\"grid\":[{{\"c\":1,\"r\":1,\"v\":\"y\"}},{{\"c\":1,\"r\":5,\"v\":\"n\",\"w\":\"8 ГБ, нужно ≥32\"}},"
                f"{{\"c\":1,\"r\":6,\"v\":\"y\",\"w\":\"150 г/м²\",\"a\":\"Граммаж\"}}]{instr_schema}}}\n"
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
        live = list(cards)
        settings = self.step_settings.get("7", {})
        price_order = settings.get("price_order") or self.ranking.get("price") or "asc"
        price_desc = price_order == "desc"
        if settings.get("price_order") or self.ranking.get("price"):
            self.ranking = {"price": price_order}
        matrix_order = settings.get("matrix_order", "no_then_yes")

        def price_key(value):
            number = _decimal(value)
            if number is None:
                return Decimal("-Infinity") if price_desc else Decimal("Infinity")
            return -number if price_desc else number

        def matrix_key(card):
            if matrix_order == "yes_then_no":
                return (-card.get("match_count", 0), card.get("mismatch_count", 0), card.get("unknown_count", 0))
            return (card.get("mismatch_count", 0), -card.get("match_count", 0), card.get("unknown_count", 0))

        live.sort(key=lambda c: (
            1 if c.get("_removed") else 0,
            0 if c.get("priority") == 0 else 1,
            0 if c.get("matrix_status", "complete") == "complete" else 1,
            *matrix_key(c),
            c.get("relevance", 1),
            0 if _decimal(c.get("price")) is not None else 1,
            price_key(c.get("price")),
            _norm_label(c.get("name")),
            _norm_label(c.get("article")),
        ))
        return live

    # -- шаг 8: цена + показ ~10 ------------------------------------ #
    def step_8_price_and_top(self, cards, top=None) -> list[dict]:
        shown = [card for card in cards if not card.get("_removed")][: top or self.top]
        if self.step_settings.get("8", {}).get("live_prices", "yes") != "no" and self._oasis_mirror and shown:
            try:
                self.client = self.client or OasisClient(
                    timeout=self._remaining_timeout(8), min_interval=0, max_attempts=1,
                )
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
