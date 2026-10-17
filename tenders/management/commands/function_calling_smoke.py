import json
import os
import time
from urllib.request import Request, urlopen

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Runs one safe OpenAI function-calling round trip through AI Gateway."

    def handle(self, *args, **options):
        api_key = os.getenv("TIMEWEB_AI_API_KEY", "").strip()
        if not api_key:
            raise CommandError("TIMEWEB_AI_API_KEY is not configured")
        base_url = os.getenv("TIMEWEB_AI_BASE_URL", "https://api.timeweb.ai/v1").rstrip("/")
        model = os.getenv("TIMEWEB_AI_ASSISTANT_MODEL", "openai/gpt-4.1-mini")
        tool = {
            "type": "function",
            "function": {
                "name": "smoke_echo",
                "description": "Returns the provided test value without side effects.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }

        def call(payload):
            started = time.monotonic()
            request = Request(
                f"{base_url}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode()), round((time.monotonic() - started) * 1000)

        messages = [
            {"role": "system", "content": "Use a function when the user explicitly asks for it."},
            {"role": "user", "content": "Call smoke_echo with value 'ok'."},
        ]
        try:
            first, first_ms = call({
                "model": model, "messages": messages, "tools": [tool],
                "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 120,
            })
            assistant = first["choices"][0]["message"]
            calls = assistant.get("tool_calls") or []
            if len(calls) != 1 or calls[0].get("type") != "function":
                raise ValueError("Gateway returned no single function tool_call")
            tool_call = calls[0]
            arguments = json.loads(tool_call["function"]["arguments"])
            if tool_call["function"].get("name") != "smoke_echo" or arguments != {"value": "ok"}:
                raise ValueError("Gateway returned an invalid function tool_call")
            messages.extend([
                {"role": "assistant", "content": assistant.get("content"), "tool_calls": calls},
                {"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps({"value": "ok"})},
            ])
            second, second_ms = call({
                "model": model, "messages": messages, "tools": [tool],
                "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 120,
            })
        except Exception as exc:
            raise CommandError(f"Function-calling smoke test failed: {type(exc).__name__}: {exc}") from exc
        final = second["choices"][0]["message"]
        self.stdout.write(json.dumps({
            "model": model,
            "strict": True,
            "tool_call": {"id": tool_call["id"], "name": tool_call["function"]["name"], "arguments": arguments},
            "tool_result_accepted": isinstance(final.get("content"), str),
            "calls": 2,
            "latency_ms": [first_ms, second_ms],
            "usage": [first.get("usage", {}), second.get("usage", {})],
        }, ensure_ascii=False))
