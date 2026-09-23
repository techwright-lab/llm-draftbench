"""Text-only OpenAI dispatch; SDK imported only at dispatch."""

import time
from importlib.metadata import version

from .openai_contract import (  # compatibility exports
    BASE_URL as BASE_URL,
)
from .openai_contract import (
    SDK_VERSION as SDK_VERSION,
)
from .openai_contract import (
    OpenAIPolicy as OpenAIPolicy,
)
from .openai_contract import (
    _usage as _usage,
)
from .openai_contract import (
    digest as digest,
)
from .openai_contract import (
    native_request as native_request,
)


def preflight(transport):
    if version("openai") != SDK_VERSION:
        raise ValueError("openai_version_mismatch")
    if transport is not None:
        import httpx

        if type(transport) is not httpx.MockTransport:
            raise ValueError("fixture_transport_required")


def invoke(policy, prompt, *, transport=None, authorization=None, api_key=None):
    """Low-level fixture entry point; live requires workflow-issued capability.

    Exception messages/bodies/headers are never recorded. Native success bodies
    are private artifacts, not public reports. Missing usage is unknown, not zero.
    """
    from .pilot_policy import GPT6Policy
    from .provider_contract import parse_policy

    policy = parse_policy(policy)
    if not isinstance(policy, (OpenAIPolicy, GPT6Policy)):
        raise ValueError("openai_policy_required")
    if (
        transport is None
        and isinstance(policy, GPT6Policy)
        and policy.verified_tariff_digest is None
    ):
        raise ValueError("verified_tariff_required")
    request = native_request(policy, prompt)
    if transport is None:
        from ..provider_workflow import DispatchAuthorization

        if (
            not isinstance(authorization, DispatchAuthorization)
            or not authorization.consume(policy, prompt)
            or type(api_key) is not str
            or not api_key
        ):
            raise ValueError("live_authorization_required")
    preflight(transport)
    import httpx
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

    if transport is not None and type(transport) is not httpx.MockTransport:
        raise ValueError("fixture_transport_required")
    fixture = transport is not None
    result = {
        "adapter": "openai",
        "sdk_version": SDK_VERSION,
        "provenance": "fixture" if fixture else "provider",
        "native_request": request,
        "requested_model": policy.model,
        "served_model": None,
        "request_id": None,
        "completion_id": None,
        "native_output": None,
        "output": None,
        "usage": None,
        "provider_latency_seconds": None,
        "cost_upper_estimate": None,
        "currency": policy.currency,
        "charge_status": "unknown",
        "status": "uncertain",
    }
    start = time.monotonic()
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=policy.timeout_seconds,
        ) as http:
            with OpenAI(
                api_key="fixture-not-a-credential" if fixture else api_key,
                organization=policy.organization,
                project=policy.project,
                webhook_secret="",
                base_url=BASE_URL,
                max_retries=0,
                timeout=policy.timeout_seconds,
                http_client=http,
            ) as client:
                response = client.chat.completions.with_raw_response.create(**request)
                # Preserve wire types even if SDK model parsing fails or coerces.
                native = response.http_response.json()
                result["native_output"] = native
                result["request_id"] = response.headers.get("x-request-id")
                if type(native) is not dict:
                    raise ValueError("invalid_native_output")
                result.update(
                    served_model=native.get("model"), completion_id=native.get("id")
                )
                output = response.parse()
                result.update(
                    native_output=native,
                    served_model=output.model,
                    request_id=output._request_id,
                    completion_id=output.id,
                )
        usage, estimate = _usage(native, policy)
        result.update(
            usage=usage,
            cost_upper_estimate=estimate,
            charge_status="not_applicable_fixture"
            if fixture
            else "unreconciled"
            if usage is not None
            else "unknown",
        )
        choices = native.get("choices", [])
        if (
            output.model != policy.model
            or len(choices) != 1
            or type(native.get("id")) is not str
            or not native["id"]
            or native.get("model") != policy.model
            or native.get("service_tier") not in (None, "default")
        ):
            raise ValueError("invalid_native_output")
        choice = choices[0]
        message = choice["message"]
        reason = choice.get("finish_reason")
        result["stop_reason"] = reason
        result["output"] = message.get("content")
        if (
            message.get("tool_calls")
            or message.get("function_call")
            or message.get("refusal")
            or message.get("audio")
            or message.get("role") != "assistant"
            or type(result["output"]) is not str
        ):
            raise ValueError("invalid_native_output")
        result["status"] = (
            "success"
            if reason == "stop"
            else "limited"
            if reason == "length"
            else "invalid"
        )
    except Exception as exc:
        # Preserve safe provider identity/status, never error text/body/headers.
        if isinstance(exc, APIStatusError):
            result.update(
                request_id=exc.request_id,
                http_status=exc.status_code,
                error_code="provider_http_error",
            )
        elif isinstance(exc, APITimeoutError):
            result["error_code"] = "provider_timeout"
        elif isinstance(exc, APIConnectionError):
            result["error_code"] = "provider_connection_error"
        else:
            result["error_code"] = "invalid_native_response"
        # Even malformed post-response data may be billed. Never refund/retry.
        result.update(
            status="uncertain",
            charge_status="unknown",
            cost_upper_estimate=None,
            usage=None,
        )
    result["latency_seconds"] = time.monotonic() - start
    return result
