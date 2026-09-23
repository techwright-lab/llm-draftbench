"""Pure OpenAI request and wire-usage contract; no dispatch imports."""

import hashlib
from typing import Annotated

from pydantic import Field

from ..identity import canonical_bytes

SDK_VERSION = "2.29.0"
BASE_URL = "https://api.openai.com/v1"
Positive = Annotated[int, Field(strict=True, ge=1)]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$")]


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
        "reasoning_effort": policy.reasoning_effort,
    }
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
        if i > 272000:
            raise ValueError("short_context_required")
        estimate = policy.token_cost(i, o)
        return usage, str(estimate)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("invalid_usage") from None
