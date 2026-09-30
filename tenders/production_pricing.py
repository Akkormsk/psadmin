"""Мост между «Базой производства» (StageCounterpartyLink) и реальным
расчётом стоимости production-этапа маршрута.

До этого модуля Counterparty/StageCounterpartyLink/адаптеры (FSPrint,
fsprint_rizograf) существовали как данные и отдельно вызываемые функции —
ничто в построении маршрута их не читало. `price_stage` — единственная
точка, где это меняется: по стадии и уже известным ответам сессии
подбирает активного контрагента (по приоритету) и получает цену через
зарегистрированный обработчик для его `price_source_type`.

Обработчик регистрируется декоратором `register`, а не веткой if/elif —
добавление нового способа получения цены (ещё один внешний калькулятор,
API поставщика) не требует правки диспетчера, только новый обработчик
в своём модуле. Один провал контрагента не останавливает подбор — пробуем
следующего по приоритету, копим причины отказа для честного сообщения,
если не получилось ни у кого (см. §29 промпта: сравнить и вернуть
детерминированный выбор, а не гадать)."""

import importlib
import inspect
from dataclasses import dataclass, field
from decimal import Decimal


class ProductionPricingError(Exception):
    """Ни один контрагент этапа не смог посчитать цену — причины внутри
    сообщения. Не выдумывает цифры и не выбирает наугад."""


@dataclass
class ProductionPriceResult:
    counterparty_name: str
    price_source_type: str
    unit_cost: Decimal
    total_cost: Decimal
    detail: dict = field(default_factory=dict)


_PRICERS = {}


def register(price_source_type):
    def decorator(fn):
        _PRICERS[price_source_type] = fn
        return fn
    return decorator


def _answers_for_stage(stage_id, hypothesis):
    """Ответы на уточняющие вопросы ЭТОГО этапа (см. routes._missing_parameter_questions
    — id вида "req-<stage_id>-<текст параметра>"), по тексту требуемого
    параметра. Session-scoped вводимые администратором данные, не Lesson."""
    prefix = f"req-{stage_id}-"
    answers = hypothesis.get("question_answers", {}) if isinstance(hypothesis, dict) else {}
    result = {}
    for question in hypothesis.get("questions", []) if isinstance(hypothesis, dict) else []:
        question_id = question.get("id", "")
        if question_id.startswith(prefix) and answers.get(question_id):
            result[question.get("text", "")] = answers[question_id]
    return result


@register("internal_calculator")
def _price_via_internal_calculator(link, quantity, answers):
    """`link.settings["pricing_module"]` — модуль с функцией
    `calculate_price(quantity, **kwargs) -> {"price_per_copy"|"unit_cost":…, "total":…}`
    (см. tenders/integrations/fsprint_rizograf.py). `link.settings["answer_mapping"]`
    переводит текст уточняющего вопроса в имя именованного параметра этой
    функции — явное сопоставление, не угадывание по смыслу."""
    module_path = link.settings.get("pricing_module")
    if not module_path:
        raise ProductionPricingError("не указан pricing_module в настройках связи")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ProductionPricingError(f"модуль расчёта «{module_path}» не найден: {exc}") from exc
    mapping = link.settings.get("answer_mapping", {})
    kwargs = {mapping[text]: value for text, value in answers.items() if text in mapping}
    quantity = int(quantity)
    try:
        inspect.signature(module.calculate_price).bind(quantity, **kwargs)
    except TypeError as exc:
        raise ProductionPricingError(f"не хватает параметров для расчёта: {exc}") from exc
    try:
        result = module.calculate_price(quantity, **kwargs)
    except Exception as exc:
        raise ProductionPricingError(str(exc)) from exc
    unit = result.get("price_per_copy", result.get("unit_cost"))
    total = result.get("total", result.get("total_cost"))
    if unit is None or total is None:
        raise ProductionPricingError(f"модуль «{module_path}» вернул неожиданный формат: {result}")
    return ProductionPriceResult(
        counterparty_name=link.counterparty.name, price_source_type=link.price_source_type,
        unit_cost=Decimal(str(unit)), total_cost=Decimal(str(total)), detail=result,
    )


def price_stage(stage_id, quantity, hypothesis):
    from .models import StageCounterpartyLink

    answers = _answers_for_stage(stage_id, hypothesis)
    links = list(
        StageCounterpartyLink.objects.filter(stage_id=stage_id, is_active=True, counterparty__is_active=True)
        .select_related("counterparty").order_by("priority", "counterparty__name")
    )
    if not links:
        raise ProductionPricingError("К этому этапу пока не привязан ни один контрагент — добавьте его в «Базе производства».")
    errors = []
    for link in links:
        pricer = _PRICERS.get(link.price_source_type)
        if pricer is None:
            errors.append(f"{link.counterparty.name}: способ «{link.get_price_source_type_display()}» ещё не реализован")
            continue
        try:
            return pricer(link, quantity, answers)
        except ProductionPricingError as exc:
            errors.append(f"{link.counterparty.name}: {exc}")
    raise ProductionPricingError("Не удалось получить цену ни от одного контрагента этапа — " + "; ".join(errors))
