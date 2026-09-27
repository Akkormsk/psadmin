"""Один JSON-запрос к Timeweb AI Gateway (OpenAI-совместимый chat/completions)."""
from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AIGatewayError(RuntimeError):
    pass


def json_from_model(content: str) -> dict:
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
    raise AIGatewayError("Модель вернула ответ в неожиданном формате.") from last_error


def chat_json(system: str, user: str, *, model: str, max_tokens: int, timeout: int = 90) -> dict:
    """Возвращает {'data': dict, 'usage': dict}."""
    api_key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    base_url = os.getenv("TIMEWEB_AI_BASE_URL", "https://api.timeweb.ai/v1").rstrip("/")
    if not api_key:
        raise AIGatewayError("AI Gateway не настроен (нет TIMEWEB_AI_API_KEY).")
    body = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    request = Request(
        f"{base_url}/chat/completions", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise AIGatewayError(f"AI Gateway ответил HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AIGatewayError("AI Gateway недоступен.") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIGatewayError("AI Gateway вернул ответ без содержимого.") from exc
    return {"data": json_from_model(content), "usage": data.get("usage", {})}
