"""Консервативная проверка структурированных полей без словарей товаров."""

import re
from decimal import Decimal


TOLERANCE = Decimal("0.05")
_UNITS = {}
for dimension, groups in {
    "length": [("мм mm", "1"), ("см cm", "10"), ("м m", "1000")],
    "mass": [("мг mg", "0.001"), ("г g", "1"), ("кг kg", "1000")],
    "volume": [("мл ml", "1"), ("л l", "1000")],
    "memory": [("мб mb", "1"), ("гб gb", "1024"), ("тб tb", "1048576")],
    "time": [("с s", "1"), ("мин min", "60"), ("ч h", "3600")],
}.items():
    for aliases, factor in groups:
        for alias in aliases.split():
            _UNITS[alias] = (dimension, Decimal(factor))


def _normal(value):
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def _field(name):
    return _normal(re.sub(r"\(([^()]*)\)", lambda match: "" if _normal(match[1]) in _UNITS else match[0], str(name)))


def _measurement(value, unit=""):
    match = re.fullmatch(r"([+-]?\d+(?:[.,]\d+)?)\s*([^\d\s]*)", _normal(value))
    if not match:
        return None
    suffix = match[2] or _normal(unit)
    if suffix not in _UNITS:
        return None
    dimension, factor = _UNITS[suffix]
    return dimension, Decimal(match[1].replace(",", ".")) * factor


def deterministic_cells(card, criteria, tolerance=TOLERANCE):
    cells = {}
    attributes = [a for a in card.get("attributes", []) if isinstance(a, dict)]
    for index, criterion in enumerate((c for c in criteria if c.checked), 1):
        names = {_field(criterion.label), _field(criterion.concept)} - {""}
        facts = [a for a in attributes if _field(a.get("name", "")) in names]
        if not facts:
            continue
        target = _measurement(criterion.value, criterion.unit)
        measurements = []
        for fact in facts:
            bracket = re.search(r"\(([^()]*)\)\s*$", str(fact.get("name", "")))
            unit = fact.get("dim") or (bracket[1] if bracket else "")
            measurements.append(_measurement(fact.get("value"), unit))
        verdict = None
        if target and all(m is not None and m[0] == target[0] for m in measurements):
            values = {m[1] for m in measurements}
            if len(values) != 1:
                continue
            actual = values.pop()
            required = target[1]
            delta = abs(required) * tolerance
            checks = {"=": required - delta <= actual <= required + delta,
                      ">=": actual >= required - delta, "<=": actual <= required + delta}
            if criterion.operator in checks:
                verdict = "y" if checks[criterion.operator] else "n"
        elif not target:
            values = {_normal(a.get("value")) for a in facts}
            if len(values) != 1:
                continue
            actual = values.pop()
            options = {_normal(v) for v in criterion.options}
            if criterion.operator in {"in", "any"} and actual in options:
                verdict = "y"
            elif criterion.operator == "all" and options and options <= {actual}:
                verdict = "y"
            elif criterion.operator in {"=", "~"} and not options and actual == _normal(criterion.value) and actual:
                verdict = "y"
        if verdict:
            cells[index] = (verdict, f"{facts[0]['name']}: {facts[0]['value']}")
    return cells
