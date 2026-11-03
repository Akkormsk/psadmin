"""Shared typed values for source reconciliation and offer/requirement comparison.

Pure normalization/comparison takes an explicit alias snapshot. Database aliases
are loaded by application adapters, not by comparison or display code.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal
import re
import hashlib
import json
import unicodedata

from django.db.models.signals import post_delete, post_save
from .models import UnitAlias

VERSION = "requirement-values-v1"

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


# Base units and exact factors; different dimensions never compare as equal.
_SCALES = {
    "мм": ("length", Decimal("1")), "см": ("length", Decimal("10")), "м": ("length", Decimal("1000")),
    "мг": ("mass", Decimal("0.001")), "г": ("mass", Decimal("1")), "кг": ("mass", Decimal("1000")),
    "мл": ("volume", Decimal("1")), "л": ("volume", Decimal("1000")),
    "г/м²": ("area_density", Decimal("1")), "кг/м²": ("area_density", Decimal("1000")),
    "г/м³": ("volume_density", Decimal("1")), "кг/м³": ("volume_density", Decimal("1000")),
}
_UNIT_ALIAS_SEED.update({"m": "м", "mм": "мм", "г": "г", "g": "г", "м": "м", "л": "л", "l": "л", "кг/м2": "кг/м²", "kg/m2": "кг/м²", "kg/m²": "кг/м²", "кг/м3": "кг/м³", "kg/m3": "кг/м³"})

def canonical_unit(unit, aliases=None):
    raw = str(unit or "").strip().lower().replace(" ", "")
    known = {**_UNIT_ALIAS_SEED, **(aliases or {})}
    return known.get(raw, raw)

def unit_from_label(label):
    label = str(label or "").strip()
    match = re.search(r"(?:,|\()\s*([^(),]+)\)?$", label)
    return match.group(1).strip() if match else ""

def registry_signature(aliases=None):
    return hashlib.sha256(json.dumps({**_UNIT_ALIAS_SEED, **(aliases or {})}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

@dataclass(frozen=True)
class Value:
    kind: str
    unit: str = ""
    lower: Decimal | None = None
    upper: Decimal | None = None
    lower_closed: bool = True
    upper_closed: bool = True
    text: str = ""
    members: tuple = ()

    def snapshot(self, aliases=None):
        result = asdict(self)
        for key in ("lower", "upper"):
            result[key] = format(result[key].normalize(), 'f') if result[key] is not None else None
        result['members'] = [member.snapshot(aliases) for member in self.members]
        return {'version': VERSION, 'unit_registry': registry_signature(aliases), **result}

_NUMBER = r"[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
_OPS = {"не менее": ">=", "не более": "<=", "более": ">", "менее": "<", "≥": ">=", "≤": "<=", "=": "=", ">=": ">=", "<=": "<=", ">": ">", "<": "<"}

def _number(text):
    return Decimal(re.sub(r"[ \u00a0\u202f]", "", text).replace(",", "."))

def interval(lower=None, upper=None, *, unit="", lower_closed=True, upper_closed=True, aliases=None):
    unit = canonical_unit(unit, aliases)
    dimension, factor = _SCALES.get(unit, (unit, Decimal("1")))
    for bound in (lower, upper):
        if bound is not None and not Decimal(str(bound)).is_finite():
            return Value('unknown', text='non-finite bound')
    if lower is not None and upper is not None and (Decimal(str(lower)) > Decimal(str(upper)) or (Decimal(str(lower)) == Decimal(str(upper)) and not (lower_closed and upper_closed))):
        return Value('unknown', text='empty interval')
    return Value('numeric', dimension, Decimal(str(lower)) * factor if lower is not None else None,
                 Decimal(str(upper)) * factor if upper is not None else None, lower_closed, upper_closed)

def normalize(value, *, unit="", aliases=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        return Value("unknown")
    if isinstance(value, Value):
        return value
    if isinstance(value, bool):
        return Value('boolean', text=str(value))
    if isinstance(value, dict) and value.get('kind') == 'set' and isinstance(value.get('values'), list):
        return Value('set', members=tuple(sorted(set(normalize(x, unit=unit, aliases=aliases) for x in value['values']), key=repr)))
    if isinstance(value, dict):
        return Value('object', text=json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))
    if isinstance(value, (list, tuple)):
        return Value('sequence', members=tuple(normalize(x, unit=unit, aliases=aliases) for x in value))
    text = ' '.join(unicodedata.normalize('NFC', str(value)).casefold().split())
    if text in {'да', 'true'}:
        return Value('boolean', text='True')
    if text in {'нет', 'false'}:
        return Value('boolean', text='False')
    excluded = re.fullmatch(r'(?:!=|≠|не равно)\s*(.+)', text)
    if excluded:
        return Value('not_equal', members=(normalize(excluded.group(1), unit=unit, aliases=aliases),))
    tolerance = re.fullmatch(r'(' + _NUMBER + r')\s*([a-zа-яё/²³0-9]*)\s*±\s*(' + _NUMBER + r')\s*(%|[a-zа-яё/²³0-9]*)', text)
    if tolerance:
        number, first_unit, delta, last_unit = tolerance.groups()
        center, spread = _number(number), _number(delta)
        if spread >= 0 and not (first_unit and last_unit and last_unit != '%'):
            spread = abs(center) * spread / 100 if last_unit == '%' else spread
            return interval(center - spread, center + spread, unit=first_unit or (last_unit if last_unit != '%' else '') or unit, aliases=aliases)
    dimensions = re.fullmatch(r'(' + _NUMBER + r'(?:\s*[xх×*]\s*' + _NUMBER + r')+)\s*([a-zа-яё/²³0-9]*)', text)
    if dimensions:
        values, inline_unit = dimensions.groups()
        return Value('dimensions', members=tuple(normalize(n.strip(), unit=inline_unit or unit, aliases=aliases) for n in re.split(r'[xх×*]', values)))
    # Match complete expressions: unrelated numbers in prose are never extracted.
    expression = re.fullmatch(r'(?:(не менее|не более|более|менее|>=|<=|≥|≤|>|<|=)\s*)?(' + _NUMBER + r')\s*([^\d,;()]+(?:[²³23])?)?', text)
    if expression:
        op, number, inline_unit = expression.groups()
        inline_unit = (inline_unit or '').strip()
        # A unit is a compact token, not arbitrary trailing prose.
        if not inline_unit or re.fullmatch(r'[a-zа-яё%/²³0-9]+', inline_unit):
            n = _number(number)
            op = _OPS.get(op or '=', '=')
            return interval(n if op in ('=', '>=', '>') else None, n if op in ('=', '<=', '<') else None,
                unit=inline_unit or unit, lower_closed=op != '>', upper_closed=op != '<', aliases=aliases)
    both = re.fullmatch(r'(не менее|более|>=|≥|>)\s*(' + _NUMBER + r')\s*(?:;|и)\s*(не более|менее|<=|≤|<)\s*(' + _NUMBER + r')\s*([a-zа-яё%/²³0-9]*)', text)
    if both:
        low_op, low, high_op, high, inline_unit = both.groups()
        if _number(low) <= _number(high):
            return interval(_number(low), _number(high), unit=inline_unit or unit,
                lower_closed=_OPS[low_op] != '>', upper_closed=_OPS[high_op] != '<', aliases=aliases)
    span = re.fullmatch(r'(' + _NUMBER + r')\s*[–—-]\s*(' + _NUMBER + r')\s*([a-zа-яё%/²³0-9]*)', text)
    if span:
        low, high, inline_unit = span.groups()
        if _number(low) <= _number(high):
            return interval(_number(low), _number(high), unit=inline_unit or unit, aliases=aliases)
    bounds = re.fullmatch(r'от\s+(' + _NUMBER + r')\s+до\s+(' + _NUMBER + r')\s*([a-zа-яё%/²³0-9]*)', text)
    if bounds:
        low, high, inline_unit = bounds.groups()
        if _number(low) <= _number(high):
            return interval(_number(low), _number(high), unit=inline_unit or unit, aliases=aliases)
    return Value('text', text=text)

def equivalent(left, right, *, unit="", aliases=None):
    left_value = normalize(left, unit=unit, aliases=aliases)
    right_value = normalize(right, unit=unit, aliases=aliases)
    return left_value.kind != "unknown" and right_value.kind != "unknown" and left_value == right_value

def satisfies(offered, required):
    """True/False/None: proven inclusion, proven mismatch, insufficient evidence."""
    if required.kind == 'not_equal':
        excluded = required.members[0]
        if offered == excluded:
            return False
        if offered.kind == excluded.kind == 'numeric' and offered.unit == excluded.unit:
            return True if satisfies(offered, excluded) is False else None
        return None
    if offered.kind != required.kind:
        return None
    if offered.kind == 'numeric':
        if offered.unit != required.unit:
            return None
        lower_ok = required.lower is None or (offered.lower is not None and (offered.lower > required.lower or
            (offered.lower == required.lower and (required.lower_closed or not offered.lower_closed))))
        upper_ok = required.upper is None or (offered.upper is not None and (offered.upper < required.upper or
            (offered.upper == required.upper and (required.upper_closed or not offered.upper_closed))))
        if lower_ok and upper_ok:
            return True
        disjoint = (offered.upper is not None and required.lower is not None and (offered.upper < required.lower or
            (offered.upper == required.lower and not (offered.upper_closed and required.lower_closed)))) or (
            offered.lower is not None and required.upper is not None and (offered.lower > required.upper or
            (offered.lower == required.upper and not (offered.lower_closed and required.upper_closed))))
        return False if disjoint else None
    if offered.kind == 'dimensions':
        if len(offered.members) != len(required.members): return False
        results = [satisfies(a, b) for a, b in zip(offered.members, required.members)]
        return False if False in results else None if None in results else True
    if offered.kind == 'set':
        if not offered.members or not required.members:
            return None
        return all(member in required.members for member in offered.members)
    if offered.kind in {'text', 'boolean', 'sequence'}:
        return True if offered == required else None
    return None


def criterion_value(criterion, *, tolerance=Decimal("0"), aliases=None):
    """Adapter for existing cascade criteria; tolerance is an explicit search policy."""
    low, high = criterion.num_min, criterion.num_max
    op = criterion.operator
    if op == '!=':
        return Value('not_equal', members=(normalize(criterion.value, unit=criterion.unit, aliases=aliases),))
    if low is None and high is None:
        return normalize(criterion.value, unit=criterion.unit, aliases=aliases)
    if low is not None:
        low = low - abs(low) * tolerance
    if high is not None:
        high = high + abs(high) * tolerance
    return interval(low, high, unit=criterion.unit, lower_closed=getattr(criterion, 'lower_closed', None) if getattr(criterion, 'lower_closed', None) is not None else op != '>', upper_closed=getattr(criterion, 'upper_closed', None) if getattr(criterion, 'upper_closed', None) is not None else op != '<', aliases=aliases)


def normalized_fact(fact, *, aliases=None):
    """One persisted fact format, preserving original value, scope and provenance."""
    result = dict(fact)
    result['normalized_value'] = normalize(fact.get('value'), unit=unit_from_label(fact.get('name')), aliases=aliases).snapshot(aliases)
    return result


def options_satisfy(offered, required, *, mode="choose_one"):
    offered_values = set(normalize(value) for value in offered)
    required_values = set(normalize(value) for value in required)
    if not offered_values or not required_values:
        return None
    return required_values <= offered_values if mode == 'fulfill_set' else bool(offered_values & required_values)
