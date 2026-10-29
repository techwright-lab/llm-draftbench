"""Generative judges through the pinned OpenAI and Anthropic SDKs.

A fail-style verdict needs a verbatim quote from one of the two drafts; a verdict
whose evidence is not in either draft is recorded as unknown, never as a choice.
"""

import re
import time

from ..adapters import anthropic_contract, openai_contract
from .criteria import CHOICES, JUDGE_MANDATE

TIMEOUT_SECONDS = 300.0
SCHEMA_NAME = "PairwiseVerdictSchema"
_SPACE = re.compile(r"\s+")


def wire_schema(criterion_ids):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdicts"],
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "choice", "evidence", "reason"],
                    "properties": {
                        "criterion": {"type": "string", "enum": list(criterion_ids)},
                        "choice": {"type": "string", "enum": list(CHOICES)},
                        "evidence": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }


def user_message(panel, presentation):
    a, b = (
        panel["candidates"][presentation["a"]],
        panel["candidates"][presentation["b"]],
    )
    criteria = "\n".join(
        f"- {item['criterion_id']}: {item['question']}" for item in panel["criteria"]
    )
    return (
        "## The brief\n\n"
        + panel["brief"]
        + "\n\n## Draft A\n\n"
        + a["body"]
        + "\n\n## Draft B\n\n"
        + b["body"]
        + "\n\n## Criteria\n\n"
        + criteria
        + "\n\nAnswer every criterion above and only those."
    )


def native_request(judge, panel, presentation):
    schema = wire_schema([item["criterion_id"] for item in panel["criteria"]])
    user = user_message(panel, presentation)
    if judge["kind"] == "openai":
        return {
            "model": judge["model"],
            "instructions": JUDGE_MANDATE,
            "input": [{"role": "user", "content": user}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": SCHEMA_NAME,
                    "schema": schema,
                    "strict": True,
                }
            },
            "max_output_tokens": judge["max_output_tokens"],
            "reasoning": {"effort": judge["reasoning_effort"]},
            "stream": False,
            "store": False,
            "service_tier": "default",
        }
    return {
        "model": judge["model"],
        "system": [{"type": "text", "text": JUDGE_MANDATE}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": user}]}],
        "max_tokens": judge["max_tokens"],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": judge["effort"],
            "format": {"type": "json_schema", "schema": schema},
        },
        "stream": False,
        "service_tier": "standard_only",
        "inference_geo": "global",
    }


def invoke(judge, panel, presentation, *, transport=None, credentials=None):
    import httpx

    if transport is not None and type(transport) is not httpx.MockTransport:
        raise ValueError("fixture_transport_required")
    fixture = transport is not None
    credentials = credentials or {}
    request = native_request(judge, panel, presentation)
    result = {
        "adapter": judge["kind"],
        "provenance": "fixture" if fixture else "provider",
        "requested_model": judge["model"],
        "served_model": None,
        "request_id": None,
        "native_output": None,
        "verdicts": None,
        "usage": None,
        "estimated_usd": None,
        "provider_latency_seconds": None,
        "status": "uncertain",
    }
    start = time.monotonic()
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
        ) as http:
            if judge["kind"] == "openai":
                native, request_id, text = _openai(request, http, fixture, credentials)
            else:
                native, request_id, text = _anthropic(
                    request, http, fixture, credentials
                )
        result.update(native_output=native, request_id=request_id)
        result["served_model"] = native.get("model") if type(native) is dict else None
        result["usage"] = _usage(native, judge["kind"])
        result["verdicts"] = _verdicts(text, panel, presentation)
        result["status"] = "success"
    except _AuthRejected:
        result["error_code"] = "auth_rejected"
    except Exception as exc:
        result["error_code"] = _error_code(exc)
        result.update(verdicts=None, usage=None)
    finally:
        result["provider_latency_seconds"] = (
            None if fixture else round(time.monotonic() - start, 3)
        )
    return result


class _AuthRejected(Exception):
    pass


def _openai(request, http, fixture, credentials):
    from openai import OpenAI

    with OpenAI(
        api_key="fixture-not-a-credential"
        if fixture
        else credentials.get("DRAFTBENCH_OPENAI_API_KEY"),
        organization=None
        if fixture
        else credentials.get("DRAFTBENCH_OPENAI_ORGANIZATION"),
        project=None if fixture else credentials.get("DRAFTBENCH_OPENAI_PROJECT"),
        webhook_secret="",
        base_url=openai_contract.BASE_URL,
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
        http_client=http,
    ) as client:
        try:
            response = client.responses.with_raw_response.create(**request)
        except Exception as exc:
            _reject_auth(exc)
            raise
        native = response.http_response.json()
        request_id = response.headers.get("x-request-id")
        response.parse()
    if native.get("status") != "completed":
        raise ValueError("incomplete_native_response")
    text = "".join(
        part["text"]
        for item in native.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    return native, request_id, text


def _anthropic(request, http, fixture, credentials):
    from anthropic import Anthropic

    with Anthropic(
        api_key="fixture-not-a-credential"
        if fixture
        else credentials.get("DRAFTBENCH_ANTHROPIC_API_KEY"),
        auth_token="",
        base_url=anthropic_contract.BASE_URL,
        max_retries=0,
        default_headers={"anthropic-version": "2023-06-01"},
        timeout=TIMEOUT_SECONDS,
        http_client=http,
    ) as client:
        try:
            response = client.messages.with_raw_response.create(**request)
        except Exception as exc:
            _reject_auth(exc)
            raise
        native = response.http_response.json()
        request_id = response.headers.get("request-id")
        response.parse()
    if native.get("stop_reason") != "end_turn":
        raise ValueError("incomplete_native_response")
    text = "".join(
        block["text"]
        for block in native.get("content", [])
        if block.get("type") == "text"
    )
    return native, request_id, text


def _reject_auth(exc):
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        raise _AuthRejected from None


def _error_code(exc):
    name = type(exc).__name__
    if name in ("APITimeoutError", "TimeoutException"):
        return "provider_timeout"
    if name in ("APIConnectionError", "TransportError", "ConnectError"):
        return "provider_connection_error"
    if name == "APIStatusError" or hasattr(exc, "status_code"):
        return "provider_http_error"
    return "invalid_native_response"


def _usage(native, kind):
    usage = native.get("usage") if type(native) is dict else None
    if type(usage) is not dict:
        return None
    out = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if type(value) is not int or value < 0:
            return None
        out[key] = value
    if kind == "anthropic":
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key)
            out[key] = value if type(value) is int and value >= 0 else None
    return out


def _verdicts(text, panel, presentation):
    from ..identity import strict_json_loads

    parsed = strict_json_loads(text)
    rows = parsed.get("verdicts") if type(parsed) is dict else None
    if type(rows) is not list:
        raise ValueError("invalid_native_response")
    expected = [item["criterion_id"] for item in panel["criteria"]]
    bodies = {
        "a": _normal(panel["candidates"][presentation["a"]]["body"]),
        "b": _normal(panel["candidates"][presentation["b"]]["body"]),
    }
    verdicts = {}
    for row in rows:
        if (
            type(row) is not dict
            or row.get("criterion") not in expected
            or row["criterion"] in verdicts
            or row.get("choice") not in CHOICES
            or type(row.get("evidence")) is not str
            or type(row.get("reason")) is not str
        ):
            raise ValueError("invalid_native_response")
        quote = _normal(row["evidence"])
        located = [side for side, body in bodies.items() if quote and quote in body]
        verdicts[row["criterion"]] = {
            "choice": row["choice"] if located else "unknown",
            "evidence": row["evidence"],
            "evidence_in": located,
            "reason": row["reason"],
        }
    if set(verdicts) != set(expected):
        raise ValueError("invalid_native_response")
    return verdicts


def _normal(value):
    return _SPACE.sub(" ", value).strip()
