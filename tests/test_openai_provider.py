import json

import pytest

from draftbench.adapters.openai import invoke
from draftbench.adapters.pilot_policy import GPT6Policy
from draftbench.adapters.provider_contract import parse_policy


def policy(**changes):
    return GPT6Policy.model_validate(
        dict(
            model="gpt-6-luna",
            account_route="lab-test",
            project="proj_test",
            organization="org_test",
            currency="USD",
            max_cost="50",
            max_output_tokens=100,
            max_requests=3,
            max_total_tokens=4000000,
            pricing_provenance="public-docs-2026-09-23-v1",
            reasoning_effort="low",
            verified_tariff_digest="a" * 64,
        )
        | changes
    )


def response():
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fixture text"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


pytest.importorskip("openai")


def test_real_sdk_wire():
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=response(), headers={"x-request-id": "req_test"}
        )

    result = invoke(policy(), "exact prompt", transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["max_completion_tokens"] == 100
    assert body["n"] == 1 and body["store"] is False and body["stream"] is False
    assert body["reasoning_effort"] == "low"
    assert result["provenance"] == "fixture"
    assert result["request_id"] == "req_test"
    assert result["output"] == "fixture text"
    assert result["provider_latency_seconds"] is None


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "currency",
        "max_cost",
        "project",
        "account_route",
        "reasoning_effort",
        "pricing_provenance",
    ],
)
def test_required_configuration(field):
    data = policy().model_dump()
    del data[field]
    with pytest.raises(ValueError):
        GPT6Policy.model_validate(data)


@pytest.mark.parametrize("contract", ["openai-chat-text-v1", None])
def test_legacy_or_missing_contract_rejected(contract):
    data = policy().model_dump(mode="json")
    if contract is None:
        del data["contract"]
    else:
        data["contract"] = contract
    with pytest.raises(ValueError, match="unsupported_provider_contract"):
        parse_policy(data)


@pytest.mark.parametrize(
    "usage,status",
    [
        (None, "success"),
        ({}, "uncertain"),
        (
            {"prompt_tokens": 10, "completion_tokens": 101, "total_tokens": 111},
            "uncertain",
        ),
        (
            {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
            "uncertain",
        ),
    ],
)
def test_usage(usage, status):
    import httpx

    native = response()
    native["usage"] = usage
    result = invoke(
        policy(),
        "prompt",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native)),
    )
    assert result["status"] == status
    if usage is None:
        assert result["usage"] is None and result["cost_upper_estimate"] is None


def test_cache_and_reasoning_not_double_counted():
    from decimal import Decimal

    import httpx

    result = invoke(
        policy(),
        "prompt",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response())),
    )
    assert Decimal(result["cost_upper_estimate"]) == Decimal("0.000003")


def test_denied_before_network():
    with pytest.raises(ValueError, match="live_authorization_required"):
        invoke(policy(), "prompt")


@pytest.mark.parametrize("failure", ["429", "timeout"])
def test_no_retry(failure):
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private text", request=request)
        return httpx.Response(
            429,
            json={"error": {"message": "private text"}},
            headers={"x-request-id": "req_error"},
        )

    result = invoke(policy(), "prompt", transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result["status"] == "uncertain"
    assert result["charge_status"] == "unknown"
    assert "private text" not in json.dumps(result)
    if failure == "429":
        assert result["request_id"] == "req_error"
        assert result["http_status"] == 429
    else:
        assert result["error_code"] == "provider_timeout"


def transport():
    from draftbench.adapters.openai_fixture import fixture_transport

    return fixture_transport(policy().model)


def test_workflow_replay_and_no_redispatch(tmp_path, monkeypatch, campaign):
    from pathlib import Path

    from draftbench.provider_workflow import report_openai, resume_openai, run_openai

    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    root = tmp_path / "run"
    first = run_openai(
        suite, root, policy(), transport=transport(), max_steps=1, campaign=campaign
    )
    assert first["attempt_count"] == 1
    final = resume_openai(root, transport=transport(), campaign=campaign)
    assert final["complete"] and final["attempt_count"] == 3
    monkeypatch.setattr(
        "draftbench.provider_workflow.invoke", lambda *a, **k: pytest.fail("redispatch")
    )
    assert resume_openai(root, transport=transport(), campaign=campaign) == final
    assert report_openai(root) == final
    assert final["provenance"] == "fixture" and not final["model_execution_performed"]


@pytest.mark.parametrize(
    "stage,expected",
    [("reserved", 3), ("in_flight", 1), ("artifact_saved", 1), ("result_saved", 3)],
)
def test_crash_retains_reservation(tmp_path, stage, expected, campaign):
    from pathlib import Path

    from draftbench.provider_workflow import resume_openai, run_openai

    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    root = tmp_path / "run"

    def crash(name, work):
        if name == stage:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_openai(
            suite,
            root,
            policy(),
            transport=transport(),
            checkpoint=crash,
            campaign=campaign,
        )
    result = resume_openai(root, transport=transport(), campaign=campaign)
    assert result["attempt_count"] == expected
    if expected == 1:
        assert result["states"]["uncertain"] == 1
        assert result["reserved_cost"] == str(policy().reservation_cost)


@pytest.mark.parametrize(
    "changes", [{"max_requests": 1}, {"max_total_tokens": 922100}, {"max_cost": "0.3"}]
)
def test_caps_never_refunded(tmp_path, changes, campaign):
    from pathlib import Path

    from draftbench.provider_workflow import resume_openai, run_openai

    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    p = policy(**changes)
    first = run_openai(
        suite, tmp_path / "run", p, transport=transport(), campaign=campaign
    )
    assert first["attempt_count"] == 1
    assert (
        resume_openai(tmp_path / "run", transport=transport(), campaign=campaign)
        == first
    )


def test_live_approval_denied_before_creation(tmp_path, campaign):
    from pathlib import Path

    from draftbench.provider_workflow import run_openai

    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    seen = []

    def reject(binding):
        seen.append(binding)
        return False

    with pytest.raises(ValueError, match="live_authorization_required"):
        run_openai(suite, tmp_path / "run", policy(), approve=reject, campaign=campaign)
    assert len(seen) == 1 and not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "gpt-4.1-2025-04-14"},
        {"model": "gpt-5"},
        {"contract": "openai-chat-text-v1"},
        {"max_output_tokens": 32769},
        {"max_output_tokens": True},
        {"context_window_tokens": 1047576},
        {"price_unit": "per_token"},
        {"input_per_million": "NaN"},
        {"max_cost": "0"},
    ],
)
def test_unsupported_policy(changes):
    with pytest.raises(ValueError):
        policy(**changes)


@pytest.mark.parametrize(
    "change,status",
    [
        ("length", "limited"),
        ("content_filter", "invalid"),
        ("served", "uncertain"),
        ("id", "uncertain"),
        ("tool", "uncertain"),
    ],
)
def test_native_identity_and_stops(change, status):
    import httpx

    native = response()
    if change == "served":
        native["model"] = "other-model"
    elif change == "id":
        native["id"] = None
    elif change == "tool":
        native["choices"][0]["message"]["tool_calls"] = [
            {
                "id": "test",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]
    else:
        native["choices"][0]["finish_reason"] = change
    result = invoke(
        policy(),
        "p",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native)),
    )
    assert result["status"] == status
    assert result["native_output"] == native


def test_input_limit_before_dispatch():
    import httpx

    p = policy(max_input_bytes=1)
    with pytest.raises(ValueError, match="input_limit"):
        invoke(p, "é", transport=httpx.MockTransport(lambda r: pytest.fail("dispatch")))


def test_timeout_receipt_retained_and_no_resume_calls(tmp_path, campaign):
    from pathlib import Path

    import httpx

    from draftbench.provider_workflow import report_openai, resume_openai, run_openai

    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("hidden", request=request)

    root = tmp_path / "run"
    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    first = run_openai(
        suite,
        root,
        policy(),
        transport=httpx.MockTransport(timeout),
        campaign=campaign,
    )
    assert first["states"]["uncertain"] == 1
    assert len(list(root.glob("attempt-*.json"))) == 1
    assert (
        resume_openai(root, transport=httpx.MockTransport(timeout), campaign=campaign)
        == first
    )
    assert report_openai(root) == first
    assert len(calls) == 1


def test_no_openai_environment_discovery(monkeypatch):
    import os

    original = os.environ.get

    def deny(key, *args):
        if key.startswith("OPENAI_"):
            pytest.fail("credential discovery")
        return original(key, *args)

    monkeypatch.setattr(os.environ, "get", deny)
    assert invoke(policy(), "prompt", transport=transport())["status"] == "success"
