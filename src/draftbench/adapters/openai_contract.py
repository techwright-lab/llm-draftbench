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
    from .replay_contract import parse_prompt, wire_schema

    system, user, schema = parse_prompt(prompt, policy.max_input_bytes)
    # Mirrors TG's RubyLLM Responses render: system -> instructions, user as a
    # plain-string input item, strict json_schema named by the schema title.
    return {
        "model": policy.model,
        "instructions": system,
        "input": [{"role": "user", "content": user}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema,
                "schema": wire_schema(schema),
                "strict": True,
            }
        },
        "max_output_tokens": policy.max_output_tokens,
        "reasoning": {"effort": policy.reasoning_effort},
        "stream": False,
        "store": False,
        "service_tier": "default",
    }


def _usage(native, policy):
    usage = native.get("usage")
    if usage is None:
        return None, None
    try:
        i, o, t = (usage[k] for k in ("input_tokens", "output_tokens", "total_tokens"))
        if any(type(x) is not int or x < 0 for x in (i, o, t)) or t != i + o:
            raise ValueError()
        if i > policy.context_window_tokens or o > policy.max_output_tokens:
            raise ValueError()
        # Validate WIRE types before equality: False and 0.0 compare equal to 0.
        # Cached input gets no discount; reasoning is included in output tokens.
        for key, allowed, total in (
            ("input_tokens_details", "cached_tokens", i),
            ("output_tokens_details", "reasoning_tokens", o),
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


def check_identity(native, policy):
    from .pilot_policy import served_model_matches

    if (
        type(native) is not dict
        or native.get("object") != "response"
        or type(native.get("id")) is not str
        or not native["id"]
        or not served_model_matches(policy.model, native.get("model"))
        or native.get("service_tier") not in (None, "default")
        or native.get("error") is not None
        or type(native.get("output")) is not list
    ):
        raise ValueError("invalid_native_output")


def visible_output(native, policy):
    """Exactly one assistant message of output_text; reasoning items are private."""
    check_identity(native, policy)
    messages = []
    for item in native["output"]:
        if type(item) is not dict:
            raise ValueError("invalid_native_output")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ValueError("invalid_native_output")
        messages.append(item)
    if len(messages) != 1 or type(messages[0].get("content")) is not list:
        raise ValueError("invalid_native_output")
    text = []
    for part in messages[0]["content"]:
        if (
            type(part) is not dict
            or part.get("type") != "output_text"
            or type(part.get("text")) is not str
        ):
            raise ValueError("invalid_native_output")
        text.append(part["text"])
    return "".join(text)


def stop_status(native):
    status = native.get("status")
    if status == "completed":
        return "success"
    reason = (native.get("incomplete_details") or {}).get("reason")
    if status == "incomplete" and reason == "max_output_tokens":
        return "limited"
    return "invalid"
