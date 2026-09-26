"""Клиент Jev System One для узких решений каскада."""

import json
import os
import urllib.error
import urllib.request

from .services import TenderAIError


def decide_matrix(state, questions, *, timeout=30):
    key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
    if not key:
        raise TenderAIError("Не настроен ключ AI Gateway для Jev.")
    payload = json.dumps({"model": "jev-1.13.0", "state": state, "questions": questions}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        "https://api.timeweb.ai/v1/systemone", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise TenderAIError("Jev не ответил. Оставляю ячейки непроверенными.") from exc
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise TenderAIError("Jev вернул ответ без решений.")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return answers, {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
