"""Single-request Anthropic Messages SDK dispatch, never retries or tool loops."""

import time
from importlib.metadata import version

from .anthropic_contract import (
    BASE_URL,
    SDK_VERSION,
    _usage,
    native_request,
    visible_output,
)
from .pilot_policy import AnthropicPolicy


def preflight(transport):
    if version("anthropic") != SDK_VERSION:
        raise ValueError("anthropic_version_mismatch")
    if transport is None:
        from .sdk_logging import refuse_sdk_debug_logging

        refuse_sdk_debug_logging()
    else:
        import httpx

        if type(transport) is not httpx.MockTransport:
            raise ValueError("fixture_transport_required")


def invoke(policy, prompt, *, transport=None, authorization=None, api_key=None):
    policy = AnthropicPolicy.model_validate(policy)
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
        if policy.verified_tariff_digest is None:
            raise ValueError("verified_tariff_required")
    preflight(transport)
    import httpx
    from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError

    fixture = transport is not None
    result = dict(
        adapter="anthropic",
        sdk_version=SDK_VERSION,
        provenance="fixture" if fixture else "provider",
        native_request=request,
        requested_model=policy.model,
        served_model=None,
        request_id=None,
        completion_id=None,
        native_output=None,
        output=None,
        usage=None,
        provider_latency_seconds=None,
        cost_upper_estimate=None,
        currency=policy.currency,
        charge_status="unknown",
        status="uncertain",
    )
    start = time.monotonic()
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=policy.timeout_seconds,
        ) as http:
            with Anthropic(
                api_key="fixture-not-a-credential" if fixture else api_key,
                auth_token="",
                base_url=BASE_URL,
                max_retries=0,
                default_headers={"anthropic-version": "2023-06-01"},
                timeout=policy.timeout_seconds,
                http_client=http,
            ) as client:
                response = client.messages.with_raw_response.create(**request)
                native = response.http_response.json()
                result["native_output"] = native
                result["request_id"] = response.headers.get("request-id")
                response.parse()  # Exercise real SDK, but validate uncoerced wire.
        result.update(
            served_model=native.get("model"),
            completion_id=native.get("id"),
            stop_reason=native.get("stop_reason"),
        )
        result["output"] = visible_output(native, policy)
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
        result["status"] = (
            "success"
            if native["stop_reason"] == "end_turn"
            else "limited"
            if native["stop_reason"] == "max_tokens"
            else "invalid"
        )
    except Exception as exc:
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
        result.update(
            status="uncertain",
            charge_status="unknown",
            cost_upper_estimate=None,
            usage=None,
        )
    result["latency_seconds"] = time.monotonic() - start
    return result
