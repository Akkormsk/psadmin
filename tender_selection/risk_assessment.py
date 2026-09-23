"""Оценка рисков тендера по вложенным документам: обязательный чек-лист (срок
исполнения, обеспечение заявки/контракта, освобождение по ст.96 44-ФЗ, штрафы,
национальный режим, требования к образцам) + свободный текст с общим выводом.

Запускается при синхронизации нового подходящего тендера или при открытии карточки
(см. services.risk_assessment_for). Результат кэшируется на FoundTender.

Проверено вживую 16.09.2026 на реальных документах (не мок): полный текст двух
документов (~130к символов, «Описание объекта закупки» + «Проект контракта») —
модель openai/gpt-4.1-mini, ~31к токенов, ~1,85 ₽ за прогон. Извлекла конкретные
цифры пени/штрафов из текста контракта, которых в структурных данных извещения
просто нет — ради этого и городим (см. docs/risk_assessment_findings.md).
"""
from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MODEL = os.getenv("RISK_ASSESSMENT_MODEL", "openai/gpt-4.1-mini")
MAX_CONTEXT_CHARS = 150_000  # с запасом выше реально протестированных ~130к символов
MAX_DOCUMENTS = 2  # ровно контракт + ООЗ/ТЗ — больше не читаем, это основные носители риска

# Ключевые слова в docKindInfo/имени файла. Две группы — контракт и описание/ТЗ
# (у закупки обычно есть один документ из каждой, не оба сразу из одной).
# "Требования к содержанию/составу заявки" сознательно не читаем — там нет ни
# штрафов, ни сроков, ни условий обеспечения, а лишний документ — это лишние
# десятки секунд похода за ним на ЕИС.
_RELEVANT_KIND_PATTERNS = [
    re.compile(r"проект\s*контракт", re.I),
    re.compile(r"описани[ея]\s*объект|техническ\w*\s*задани", re.I),
]
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".zip")


class RiskAssessmentError(RuntimeError):
    pass


def select_documents(documents: list[dict]) -> list[dict]:
    """Ровно по одному документу из каждой группы _RELEVANT_KIND_PATTERNS (контракт,
    описание/ТЗ) — не картинки и не обоснование цены."""
    def priority(doc):
        haystack = f"{(doc.get('kind') or '').lower()} {(doc.get('name') or '').lower()}"
        if (doc.get("name") or "").lower().endswith(_SKIP_EXT):
            return None
        for i, pattern in enumerate(_RELEVANT_KIND_PATTERNS):
            if pattern.search(haystack):
                return i
        return None

    picked = {}
    for doc in documents:
        i = priority(doc)
        if i is not None and i not in picked:
            picked[i] = doc
    return [picked[i] for i in sorted(picked)]


def _card_summary(card: dict) -> str:
    """Компактная сводка из уже разобранного извещения — числа отсюда надёжнее,
    чем выковыривать их заново из прозы контракта (хотя модель туда тоже заглянет)."""
    lines = []
    customer = (card or {}).get("customer") or {}
    if customer.get("name"):
        lines.append(f"Заказчик: {customer['name']}")
    dates = (card or {}).get("dates") or {}
    for key, label in [
        ("collect_end", "Окончание приёма заявок"), ("bidding", "Аукцион"),
        ("execution_end", "Срок исполнения контракта (по извещению)"),
    ]:
        if dates.get(key):
            lines.append(f"{label}: {dates[key]}")
    money = (card or {}).get("money") or {}
    for key, label in [
        ("max_price", "НМЦК"), ("app_guarantee_amount", "Обеспечение заявки, сумма"),
        ("app_guarantee_part", "Обеспечение заявки, %"), ("contract_guarantee_part", "Обеспечение контракта, %"),
    ]:
        if money.get(key) is not None:
            lines.append(f"{label}: {money[key]}")
    if money.get("treasury_support"):
        lines.append("Казначейское сопровождение: требуется")
    return "\n".join(lines)


def preliminary_summary(card: dict) -> dict:
    """Бесплатная предварительная сводка по уже разобранному извещению — без
    документов, без обращения к ИИ, той же формы, что и настоящая оценка
    (см. _SCHEMA), но заполнена только тем, что реально есть в структурных
    данных извещения. Чего там нет (пени, нацрежим, образцы, способ
    поставки — это всё только из текста контракта) — остаётся незаполненным
    до настоящей оценки. Нет risk_level/legal_risks — вывод не наш, ИИ ещё
    не смотрел; бейдж на карточке поэтому не показываем."""
    from django.utils import timezone as _timezone

    dates = (card or {}).get("dates") or {}
    money = (card or {}).get("money") or {}
    result = {"preliminary": True}
    if dates.get("execution_end"):
        # dates.execution_end — datetime (см. notification._dt), не готовая строка,
        # как у настоящей оценки — приводим к тому же виду «ДД.ММ.ГГГГ» для одной
        # и той же строки таблицы в _risk_block.html.
        result["execution_deadline"] = {"date": _timezone.localtime(dates["execution_end"]).strftime("%d.%m.%Y")}
    security = []
    if money.get("app_guarantee_amount"):
        security.append(f"{money['app_guarantee_amount']} ₽")
    if money.get("app_guarantee_part"):
        security.append(f"{money['app_guarantee_part']}%")
    if security:
        result["application_security"] = " / ".join(security)
    if money.get("contract_guarantee_part"):
        contract_security = f"{money['contract_guarantee_part']}%"
        if money.get("treasury_support"):
            contract_security += "; требуется казначейское сопровождение"
        result["contract_security"] = contract_security
    elif money.get("treasury_support"):
        result["contract_security"] = "требуется казначейское сопровождение"
    return result


def build_context(tender, card: dict, documents: list[dict], *, fetch) -> tuple[str, list[str]]:
    """fetch(url, name) -> bytes — внедряется извне (services._fetch_doc_bytes),
    чтобы этот модуль не тянул сетевые зависимости и легко тестировался моком.
    Возвращает (полный контекст для промпта, имена реально прочитанных документов).

    Документов мало (MAX_DOCUMENTS=2), но каждый — это поход на ЕИС (SOAP-архив, до
    20с, плюс прямая ссылка про запас, до 15с), а он у каждого документа свой. Качаем
    их параллельно потоками, а не по очереди — иначе ожидание складывается."""
    from concurrent.futures import ThreadPoolExecutor

    from .documents import extract_preview

    def fetch_one(doc):
        try:
            return doc, fetch(doc["url"], doc["name"])
        except Exception:
            return doc, None

    if len(documents) > 1:
        with ThreadPoolExecutor(max_workers=len(documents)) as pool:
            fetched = list(pool.map(fetch_one, documents))
    else:
        fetched = [fetch_one(doc) for doc in documents]

    parts = [_card_summary(card)] if card else []
    used_names = []
    for doc, data in fetched:
        if data is None:
            continue
        result = extract_preview(data, doc["name"])
        html = result.get("html") or ""
        if not html:
            continue
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        parts.append(f"=== {doc['name']} ===\n{text}")
        used_names.append(doc["name"])
    full = "\n\n".join(p for p in parts if p)
    if len(full) > MAX_CONTEXT_CHARS:
        full = full[:MAX_CONTEXT_CHARS] + "\n…(обрезано по объёму)"
    return full, used_names


SYSTEM_PROMPT = (
    "Ты — специалист по участию в госзакупках (44-ФЗ/223-ФЗ) для производственной компании "
    "(полиграфия, сувенирка, текстиль, металлоизделия). Знаешь: ст. 96 44-ФЗ — участник вправе "
    "не вносить обеспечение исполнения контракта при подтверждённой добросовестности (3+ "
    "контракта за последние 3 года на сумму не менее 20% НМЦК, исполненных без неустоек и "
    "расторжений); ПП РФ №1875 и аналогичные акты (ПП 878, ПП 616 и др.) — ограничение допуска "
    "иностранной промышленной продукции по отдельным ОКПД2, требует подтверждения российского "
    "происхождения. Отвечай ТОЛЬКО валидным JSON без markdown, по-русски, конкретно и по цифрам "
    "из текста — без общих слов. Обязательно заполни ВСЕ перечисленные ключи, даже если "
    "по ним 'нет данных в тексте' — не пропускай ключи."
)

_SCHEMA = """{
  "execution_deadline": {"date": "срок исполнения из текста в формате ДД.ММ.ГГГГ без времени, или null", "assessment": "1-2 предложения: реалистичен ли срок"},
  "application_security": "сумма и/или % обеспечения заявки, или 'не требуется'/'нет данных'",
  "contract_security": "сумма и/или % обеспечения контракта, условия изменения (демпинг и т.п.), если есть",
  "security_exemption": "1 предложение: применимо ли освобождение по ст.96 44-ФЗ и что для этого нужно",
  "penalties": "конкретные цифры пени/неустойки/штрафов из текста контракта, если есть — иначе 'нет данных, нужен текст контракта'",
  "national_regime": "применим ли нацрежим (ПП 1875 и аналогичные) по ОКПД2/тексту — что разрешено/запрещено, что подтвердить, или явно 'признаков не найдено'",
  "sample_requirements": "требуются ли образцы/испытания, в какой срок, за чей счёт — по тексту, или 'нет данных'",
  "delivery_mode": "по тексту контракта: поставка по заявкам заказчика (объём каждой партии определяется заявкой, заказчик не обязан выбрать весь объём) — опиши на каких условиях; или разовая поставка всего объёма единой партией; если в тексте нет явного указания — напиши 'не найдено явного указания — вероятно разовая поставка'",
  "legal_risks": "свободный текст 3-6 предложений: общая оценка юридических рисков по доступным данным",
  "risk_level": "одно слово: low (риски незначительны или их нет), medium (есть на что обратить внимание, но участвовать можно) или high (серьёзные риски, требуется отдельное решение перед участием) — твоя итоговая оценка по всем пунктам выше"
}"""

REQUIRED_KEYS = (
    "execution_deadline", "application_security", "contract_security", "security_exemption",
    "penalties", "national_regime", "sample_requirements", "delivery_mode", "legal_risks", "risk_level",
)


def _user_prompt(context: str) -> str:
    return f"Документы закупки:\n{context}\n\nВерни JSON строго такой структуры:\n{_SCHEMA}"


def _json_from_model(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    decoder = json.JSONDecoder(strict=False)
    starts = [i for i, v in enumerate(content) if v in "{["]
    last_error = None
    for start in starts or [0]:
        try:
            result, _ = decoder.raw_decode(content[start:])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as exc:
            last_error = exc
    raise RiskAssessmentError("Модель вернула ответ в неожиданном формате.") from last_error


def call_gateway(context: str, *, retry_hint: str = "") -> dict:
    """Один HTTP-вызов шлюза. Возвращает {'data': dict, 'usage': dict}."""
    api_key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    base_url = os.getenv("TIMEWEB_AI_BASE_URL", "https://api.timeweb.ai/v1").rstrip("/")
    if not api_key:
        raise RiskAssessmentError("AI Gateway не настроен (нет TIMEWEB_AI_API_KEY).")
    system = SYSTEM_PROMPT + (f" {retry_hint}" if retry_hint else "")
    body = {
        "model": MODEL, "temperature": 0, "max_tokens": 1800,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": _user_prompt(context)}],
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RiskAssessmentError(f"AI Gateway ответил HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RiskAssessmentError("AI Gateway недоступен.") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RiskAssessmentError("AI Gateway вернул ответ без содержимого.") from exc
    parsed = _json_from_model(content)
    return {"data": parsed, "usage": data.get("usage", {})}


def assess(context: str) -> dict:
    """Один запрос + один повтор, если модель не вернула все обязательные ключи —
    проверено вживую 16.09.2026: с первого раза бывает, что 2 из 8 полей пропущены,
    хотя лимит токенов не исчерпан (см. докстринг модуля)."""
    result = call_gateway(context)
    missing = [key for key in REQUIRED_KEYS if key not in result["data"]]
    if missing:
        hint = f"В прошлый раз не хватило ключей: {', '.join(missing)}. Обязательно включи их все в этот раз."
        retry = call_gateway(context, retry_hint=hint)
        retry_missing = [key for key in REQUIRED_KEYS if key not in retry["data"]]
        if len(retry_missing) <= len(missing):
            result = retry
    return result
