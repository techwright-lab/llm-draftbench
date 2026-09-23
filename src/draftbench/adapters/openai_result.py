"""Read-only verification of successful OpenAI artifacts (no SDK dependency)."""

from typing import Annotated, Literal

from pydantic import Field

from ..models import Contract, Digest
from .openai_contract import SDK_VERSION, _usage, native_request


class OpenAIResult(Contract):
    adapter: Literal["openai"]
    sdk_version: Literal["2.29.0"]
    provenance: Literal["fixture", "provider"]
    native_request: dict
    requested_model: str
    served_model: str
    request_id: str | None
    completion_id: str
    native_output: dict
    output: str
    usage: dict | None
    provider_latency_seconds: None
    latency_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    cost_upper_estimate: str | None
    currency: str
    charge_status: Literal["not_applicable_fixture", "unreconciled", "unknown"]
    status: Literal["success"]
    stop_reason: Literal["stop"]
    request_digest: Digest
    parent_digests: dict[str, Digest]


def verify_result(value, policy, prompt, provenance):
    result = OpenAIResult.model_validate(value)
    native = result.native_output
    usage, estimate = _usage(native, policy)
    choices = native.get("choices", [])
    if len(choices) != 1:
        raise ValueError("invalid_provider_result")
    message = choices[0]["message"]
    if (
        result.sdk_version != SDK_VERSION
        or result.provenance != provenance
        or result.native_request != native_request(policy, prompt)
        or result.requested_model != policy.model
        or result.served_model != policy.model
        or native.get("model") != result.served_model
        or native.get("id") != result.completion_id
        or not result.completion_id
        or message.get("content") != result.output
        or message.get("role") != "assistant"
        or any(
            message.get(key)
            for key in ("tool_calls", "function_call", "refusal", "audio")
        )
        or choices[0].get("finish_reason") != "stop"
        or native.get("service_tier") not in (None, "default")
        or result.usage != usage
        or result.cost_upper_estimate != estimate
        or result.currency != policy.currency
        or result.charge_status
        != (
            "not_applicable_fixture"
            if provenance == "fixture"
            else "unreconciled"
            if usage is not None
            else "unknown"
        )
    ):
        raise ValueError("invalid_provider_result")
    return result
