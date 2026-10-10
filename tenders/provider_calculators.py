"""Channel-independent provider calculation services.

No adapter may execute code supplied in provider knowledge.  Rules are data and
this module interprets a deliberately small, declarative subset.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import ProviderCalculationQuote, ProviderCalculatorBinding


class ProviderCalculatorError(Exception):
    pass


def _decimal(value, default="0"):
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError):
        raise ProviderCalculatorError("Некорректное числовое значение")


def get_provider_calculator_schema(binding):
    knowledge = binding.knowledge_version.canonical_data if binding.knowledge_version else {}
    inputs = list(knowledge.get("input_schema", []))
    quantity = next((item for item in inputs if isinstance(item, dict) and item.get("key") == "quantity"), None)
    inputs = [item for item in inputs if not (isinstance(item, dict) and item.get("key") == "quantity")]
    inputs.append(quantity or {"key": "quantity", "label": "Количество, шт.", "required": True, "type": "number"})
    return {"inputs": inputs, "calculator_type": binding.calculator_type, "capability": binding.link.stage.name}


def _structured_rules(binding, spec):
    data = binding.knowledge_version.canonical_data if binding.knowledge_version else binding.configuration
    rules = data.get("pricing", data)
    quantity = _decimal(spec.get("quantity"))
    if data.get("requires_confirmation") or data.get("formula_status") == "unresolved":
        raise ProviderCalculatorError("Правила прайс-листа требуют подтверждения валюты и формулы")
    variants = rules.get("variants", {})
    if variants:
        variant_key = str(spec.get("variant", "")).strip()
        variant = variants.get(variant_key)
        if not variant:
            raise ProviderCalculatorError("Выберите вариант пошива из подтверждённого прайс-листа")
        minimum_value = variant.get("minimum_quantity")
        if minimum_value not in (None, "") and quantity < _decimal(minimum_value):
            raise ProviderCalculatorError("Количество меньше минимального тиража для выбранного варианта")
        unit_price = _decimal(variant.get("unit_price"))
        total = quantity * unit_price
        currency = rules.get("currency")
        if not currency:
            raise ProviderCalculatorError("Для прайс-листа не подтверждена валюта")
        return {
            "status": ProviderCalculationQuote.STATUS_READY,
            "currency": currency,
            "total": str(total),
            "unit_price": str(unit_price),
            "breakdown": {
                "variant": variant_key,
                "source_row": variant.get("source_row"),
                "minimum_quantity": str(minimum_value or ""),
                "exchange_rate": rules.get("exchange_rate"),
                "exchange_rate_with_markup": rules.get("exchange_rate_with_markup"),
            },
        }
    tiers = rules.get("tiers", [])
    tier = next((row for row in tiers if _decimal(row.get("min", 0)) <= quantity and (row.get("max") in (None, "") or quantity <= _decimal(row["max"]))), None)
    if not tier:
        raise ProviderCalculatorError("Для указанного количества нет подтверждённой цены")
    unit_price = _decimal(tier.get("unit_price"))
    fixed_fee = _decimal(rules.get("fixed_fee"))
    subtotal = quantity * unit_price + fixed_fee
    coefficients = []
    multiplier = Decimal("1")
    for rule in rules.get("coefficients", []):
        key = rule.get("input_key")
        if key and spec.get(key):
            value = _decimal(rule.get("multiplier", 1))
            multiplier *= value
            coefficients.append({"label": rule.get("label", key), "multiplier": str(value)})
    total = subtotal * multiplier
    minimum = _decimal(rules.get("minimum_charge"))
    total = max(total, minimum)
    return {"status": ProviderCalculationQuote.STATUS_READY, "currency": rules.get("currency", "RUB"), "total": str(total), "unit_price": str(total / quantity if quantity else 0), "breakdown": {"tier": tier, "fixed_fee": str(fixed_fee), "coefficients": coefficients, "minimum_charge": str(minimum)}}


def _internal_calculator(binding, spec):
    from calculator.models import CalculatorSettings
    from calculator.services import calculate_sheet_estimate

    config = binding.configuration
    settings = CalculatorSettings.objects.order_by("id").first()
    if not settings:
        raise ProviderCalculatorError("Не настроен внутренний калькулятор")
    lines = config.get("lines", [])
    result = calculate_sheet_estimate(lines, _decimal(spec.get("quantity", 1)), _decimal(spec.get("work_hours", 0)), settings, config.get("calculator_type", "sheet"))
    return {"status": ProviderCalculationQuote.STATUS_READY, "currency": "RUB", "total": str(result["standard"]), "unit_price": str(result["unit_standard"]), "breakdown": {key: str(value) for key, value in result.items()}}


_EXTERNAL_ADAPTERS = {}


def register_external_adapter(key):
    def decorator(adapter):
        _EXTERNAL_ADAPTERS[key] = adapter
        return adapter
    return decorator


def _external_adapter(binding, spec):
    key = binding.configuration.get("adapter_key")
    adapter = _EXTERNAL_ADAPTERS.get(key)
    if not adapter:
        raise ProviderCalculatorError("Внешний калькулятор не настроен")
    return adapter(spec, binding.configuration)


def _catalog(binding, spec):
    from .models import CatalogProduct

    query = str(spec.get("query", "")).strip()
    if not query:
        raise ProviderCalculatorError("Укажите, что искать в каталоге")
    products = CatalogProduct.objects.filter(supplier=binding.link.counterparty.catalog_supplier, is_active=True, name__icontains=query).order_by("price")[:20]
    return {"status": ProviderCalculationQuote.STATUS_READY, "currency": "RUB", "total": None, "candidates": [{"external_id": item.external_id, "name": item.name, "price": str(item.price) if item.price is not None else None} for item in products], "breakdown": {"query": query}}


def _manual_quote(binding, spec):
    return {"status": ProviderCalculationQuote.STATUS_REQUIRES_QUOTE, "currency": None, "total": None, "breakdown": {"message": "Для этой услуги требуется запрос цены у исполнителя."}}


def calculate_provider(binding, calculation_spec, context=None, persist=True):
    if not binding.is_active or not binding.link.is_active or not binding.link.counterparty.is_active:
        raise ProviderCalculatorError("Калькулятор недоступен")
    handlers = {
        ProviderCalculatorBinding.TYPE_STRUCTURED_RULES: _structured_rules,
        ProviderCalculatorBinding.TYPE_INTERNAL_CALCULATOR: _internal_calculator,
        ProviderCalculatorBinding.TYPE_EXTERNAL_ADAPTER: _external_adapter,
        ProviderCalculatorBinding.TYPE_CATALOG: _catalog,
        ProviderCalculatorBinding.TYPE_MANUAL_QUOTE: _manual_quote,
    }
    try:
        result = handlers[binding.calculator_type](binding, calculation_spec)
    except ProviderCalculatorError:
        raise
    except Exception as exc:
        raise ProviderCalculatorError(str(exc)) from exc
    if persist:
        ProviderCalculationQuote.objects.create(binding=binding, knowledge_version=binding.knowledge_version, calculation_spec=calculation_spec, result=result, status=result["status"])
    return result
