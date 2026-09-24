"""OpenAI Responses dispatch; SDK imported only at dispatch."""

import time
from importlib.metadata import version

from .openai_contract import (  # compatibility exports
    BASE_URL as BASE_URL,
)
from .openai_contract import (
    SDK_VERSION as SDK_VERSION,
)
from .openai_contract import (
    _usage as _usage,
)
from .openai_contract import check_identity, stop_status, visible_output
from .openai_contract import (
    digest as digest,
)
from .openai_contract import (
    native_request as native_request,
)


def preflight(transport):
    if version("openai") != SDK_VERSION:
        raise ValueError("openai_version_mismatch")
    if transport is None:
        from .sdk_logging import refuse_sdk_debug_logging

        refuse_sdk_debug_logging()
    else:
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
    if not isinstance(policy, GPT6Policy):
        raise ValueError("openai_policy_required")
    if transport is None and policy.verified_tariff_digest is None:
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
                response = client.responses.with_raw_response.create(**request)
                # Preserve wire types even if SDK model parsing fails or coerces.
                native = response.http_response.json()
                result["native_output"] = native
                result["request_id"] = response.headers.get("x-request-id")
                if type(native) is not dict:
                    raise ValueError("invalid_native_output")
                result.update(
                    served_model=native.get("model"), completion_id=native.get("id")
                )
                response.parse()
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
        result["stop_reason"] = native.get("status")
        check_identity(native, policy)
        status = stop_status(native)
        if status == "success":
            result["output"] = visible_output(native, policy)
        result["status"] = status
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
