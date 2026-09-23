"""SDK-free verification of saved Anthropic successes."""

from typing import Literal

from .anthropic_contract import SDK_VERSION, _usage, native_request, visible_output
from .openai_result import OpenAIResult
from .pilot_policy import served_model_matches


class AnthropicResult(OpenAIResult):
    adapter: Literal["anthropic"]
    sdk_version: Literal["0.84.0"]
    stop_reason: Literal["end_turn"]


def verify_result(value, policy, prompt, provenance):
    result = AnthropicResult.model_validate(value)
    native = result.native_output
    usage, estimate = _usage(native, policy)
    if (
        result.sdk_version != SDK_VERSION
        or result.provenance != provenance
        or result.native_request != native_request(policy, prompt)
        or result.requested_model != policy.model
        or not served_model_matches(policy.model, result.served_model)
        or native.get("model") != result.served_model
        or native.get("id") != result.completion_id
        or native.get("stop_reason") != "end_turn"
        or result.output != visible_output(native, policy)
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
