"""Conservative per-request reservations for an explicitly approved M10 run."""
import json
from decimal import Decimal


MAX_REQUEST_BYTES = 131072
TOKEN_OVERHEAD_RESERVE = 16384
MAX_OUTPUT_TOKENS = 1200
MAX_CALLS_PER_ATTEMPT = 8


def rubles(prompt, completion, input_rate, output_rate):
    return (Decimal(prompt) * Decimal(str(input_rate))
            + Decimal(completion) * Decimal(str(output_rate))) / Decimal(1000000)


class Budget:
    def __init__(self, limit_rub, rates):
        self.limit = Decimal(str(limit_rub))
        if not self.limit.is_finite() or self.limit <= 0:
            raise ValueError("Explicit positive approved budget required")
        self.rates = rates
        self.reservations = []
        self.attempt_calls = {}

    @property
    def charged_or_reserved(self):
        return sum(self.reservations, Decimal(0))

    def reserve(self, attempt, body):
        if self.attempt_calls.get(attempt, 0) >= MAX_CALLS_PER_ATTEMPT:
            raise RuntimeError("Attempt request limit reached")
        if body.get("max_tokens") != MAX_OUTPUT_TOKENS:
            raise ValueError("Baseline max_tokens changed")
        if body.get("tool_choice") != "required" or body.get("parallel_tool_calls") is not False:
            raise ValueError("Baseline tool settings changed")
        size = len(json.dumps(body).encode())  # Same serialization as baseline _gateway.
        if size > MAX_REQUEST_BYTES:
            raise RuntimeError("Request envelope exceeded; stop, do not truncate prompts")
        rate = self.rates[body["model"]]
        # Byte upper envelope for byte-level tokenization plus framing reserve.
        # Remains below the published 272k higher-price tier for GPT 4.1.
        reservation = rubles(size + TOKEN_OVERHEAD_RESERVE, MAX_OUTPUT_TOKENS,
            rate["input_rub_per_million"], rate["output_rub_per_million"])
        if self.charged_or_reserved + reservation > self.limit:
            raise RuntimeError("Approved budget would be exceeded")
        self.reservations.append(reservation)
        self.attempt_calls[attempt] = self.attempt_calls.get(attempt, 0) + 1
        return len(self.reservations) - 1

    def settle(self, index, model, usage):
        # Cache discounts never release additional spending capacity. Failed calls
        # and missing usage retain their reservation instead of becoming free.
        if not isinstance(usage, dict):
            return
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return
        rate = self.rates[model]
        cost = rubles(prompt, completion, rate["input_rub_per_million"], rate["output_rub_per_million"])
        previous = self.reservations[index]
        self.reservations[index] = cost
        if cost > previous:
            raise RuntimeError("Observed usage exceeded reservation; stop and reconcile billing")
