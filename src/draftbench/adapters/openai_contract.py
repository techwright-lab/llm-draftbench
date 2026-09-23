"""Pure OpenAI policy, request and wire-usage contract; no dispatch imports."""

import hashlib
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..identity import canonical_bytes
from ..models import Contract
from .token_budget import TokenBudget

SDK_VERSION = "2.29.0"
BASE_URL = "https://api.openai.com/v1"
Positive = Annotated[int, Field(strict=True, ge=1)]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$")]


class OpenAIPolicy(TokenBudget, Contract):
    contract: Literal["openai-chat-text-v1"] = "openai-chat-text-v1"
    # No alias or inferred model: only dated GPT-4.1 family snapshots in this v1.
    model: Annotated[str, Field(pattern=r"^gpt-4\.1(?:-mini|-nano)?-2025-04-14$")]
    account_route: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
    organization: Annotated[str, Field(pattern=r"^org_[A-Za-z0-9_-]+$")]
    project: Annotated[str, Field(pattern=r"^proj_[A-Za-z0-9_-]+$")]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    price_unit: Literal["currency_per_million_tokens"] = "currency_per_million_tokens"
    input_per_million: Money
    output_per_million: Money
    max_cost: Money
    # Reserve the entire provider context window, not an invented tokenizer.
    context_window_tokens: Literal[1047576]
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=32768)]
    max_requests: Annotated[int, Field(strict=True, ge=1, le=30000)]
    max_total_tokens: Positive
    max_input_bytes: Annotated[int, Field(strict=True, ge=1, le=1000000)] = 100000
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30

    @model_validator(mode="after")
    def prices(self):
        if any(
            Decimal(v) <= 0
            for v in (self.input_per_million, self.output_per_million, self.max_cost)
        ):
            raise ValueError("positive_explicit_pricing_required")
        return self


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def native_request(policy, prompt):
    if type(prompt) is not str or len(prompt.encode()) > policy.max_input_bytes:
        raise ValueError("input_limit")
    request = {
        "model": policy.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": policy.max_output_tokens,
        "n": 1,
        "stream": False,
        "store": False,
        "service_tier": "default",
    }
    if policy.contract == "openai-gpt6-text-v1":
        request["reasoning_effort"] = policy.reasoning_effort
    return request


def _usage(native, policy):
    usage = native.get("usage")
    if usage is None:
        return None, None
    try:
        i, o, t = (
            usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        if any(type(x) is not int or x < 0 for x in (i, o, t)) or t != i + o:
            raise ValueError()
        if i > policy.context_window_tokens or o > policy.max_output_tokens:
            raise ValueError()
        # Validate WIRE types before equality: False and 0.0 compare equal to 0.
        # Cached input gets no discount; reasoning is included in output tokens.
        for key, allowed, total in (
            ("prompt_tokens_details", "cached_tokens", i),
            ("completion_tokens_details", "reasoning_tokens", o),
        ):
            details = usage.get(key)
            if details is None:
                continue
            if type(details) is not dict:
                raise ValueError()
            for field, value in details.items():
                if value is None:
                    continue
                if type(value) is not int or not 0 <= value <= total:
                    raise ValueError()
                if field != allowed and value != 0:
                    raise ValueError()
        if policy.contract == "openai-gpt6-text-v1" and i > 272000:
            raise ValueError("short_context_required")
        estimate = policy.token_cost(i, o)
        return usage, str(estimate)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("invalid_usage") from None
