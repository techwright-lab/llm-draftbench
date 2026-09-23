"""Pure Anthropic wire contract; no SDK or dispatch dependency."""

from .pilot_policy import AnthropicPolicy as AnthropicPolicy

SDK_VERSION = "0.84.0"
BASE_URL = "https://api.anthropic.com"


def native_request(policy, prompt):
    if type(prompt) is not str or len(prompt.encode()) > policy.max_input_bytes:
        raise ValueError("input_limit")
    return {
        "model": policy.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": policy.max_output_tokens,
        "thinking": {"type": policy.thinking},
        "output_config": {"effort": policy.effort},
        "stream": False,
        "service_tier": "standard_only",
        "inference_geo": "global",
    }


def _counter(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid_usage")
    return value


def _details(value, maximum, allowed=()):
    if value is None:
        return
    if type(value) is not dict:
        raise ValueError("invalid_usage")
    for key, count in value.items():
        if count is None:
            continue
        _counter(count, maximum)
        if key not in allowed and count != 0:
            raise ValueError("invalid_usage")


def _usage(native, policy):
    usage = native.get("usage")
    if usage is None:
        return None, None
    if type(usage) is not dict:
        raise ValueError("invalid_usage")
    try:
        maximum = policy.context_window_tokens
        i = _counter(usage["input_tokens"], maximum)
        o = _counter(usage["output_tokens"], policy.max_output_tokens)
        w = _counter(usage.get("cache_creation_input_tokens", 0), maximum)
        r = _counter(usage.get("cache_read_input_tokens", 0), maximum)
        if i + w + r > maximum:
            raise ValueError("invalid_usage")
        _details(usage.get("output_tokens_details"), o, ("thinking_tokens",))
        creation = usage.get("cache_creation")
        _details(
            creation, w, ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
        if (
            creation is not None
            and sum(v for v in creation.values() if v is not None) > w
        ):
            raise ValueError("invalid_usage")
        _details(usage.get("server_tool_use"), 0)
        if usage.get("inference_geo") not in (None, "global"):
            raise ValueError("invalid_usage")
        if usage.get("service_tier") not in (None, "standard"):
            raise ValueError("invalid_usage")
        known = {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cache_creation",
            "output_tokens_details",
            "server_tool_use",
            "service_tier",
            "inference_geo",
        }
        for key in usage.keys() - known:
            if usage[key] is not None:
                _counter(usage[key], 0)
        # Conservative estimate: no read discount, all writes priced at 1h rate.
        # Children of cache_creation and output_tokens_details are not added.
        return usage, str(policy.token_cost(i + 2 * w + r, o))
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid_usage") from None


def visible_output(native, policy):
    if (
        type(native) is not dict
        or native.get("model") != policy.model
        or type(native.get("id")) is not str
        or not native["id"]
        or native.get("type") != "message"
        or native.get("role") != "assistant"
        or native.get("stop_sequence") is not None
    ):
        raise ValueError("invalid_native_output")
    blocks = native.get("content")
    if type(blocks) is not list:
        raise ValueError("invalid_native_output")
    text = []
    for block in blocks:
        if type(block) is not dict:
            raise ValueError("invalid_native_output")
        kind = block.get("type")
        if kind == "text" and type(block.get("text")) is str:
            text.append(block["text"])
        elif kind == "redacted_thinking" and type(block.get("data")) is str:
            pass  # opaque private native evidence, never visible answer text
        elif (
            kind == "thinking"
            and type(block.get("thinking")) is str
            and type(block.get("signature")) is str
        ):
            pass
        else:
            raise ValueError("invalid_native_output")
    return "".join(text)
