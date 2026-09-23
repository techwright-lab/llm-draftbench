"""Offline real-SDK contracts: no credentials, sockets or inference."""

import json
from pathlib import Path

import pytest

from draftbench.adapters.provider_contract import native_request, parse_policy

pytest.importorskip("openai")
pytest.importorskip("anthropic")

MODELS = (
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "claude-opus-5-5",
    "claude-sonnet-5",
)


def policy(model):
    values = dict(
        model=model,
        account_route="fixture-only",
        currency="USD",
        max_cost="50",
        max_output_tokens=100,
        max_requests=3,
        max_total_tokens=4000000,
        pricing_provenance="public-docs-2026-09-23-v1",
    )
    if model.startswith("gpt"):
        values.update(
            contract="openai-gpt6-text-v1",
            organization="org_fixture",
            project="proj_fixture",
            reasoning_effort="medium",
        )
    else:
        values.update(
            contract="anthropic-messages-text-v1",
            effort="medium" if "opus" in model else "high",
        )
    return parse_policy(values)


def native(model):
    if model.startswith("gpt"):
        return dict(
            id="fixture-completion",
            object="chat.completion",
            created=0,
            model=model,
            choices=[
                dict(
                    index=0,
                    finish_reason="stop",
                    message=dict(role="assistant", content="Synthetic fixture text."),
                )
            ],
            usage=dict(
                prompt_tokens=10,
                completion_tokens=8,
                total_tokens=18,
                completion_tokens_details=dict(reasoning_tokens=5),
            ),
        )
    return dict(
        id="fixture-message",
        type="message",
        role="assistant",
        model=model,
        content=[
            dict(type="redacted_thinking", data="opaque-fixture"),
            dict(type="text", text="Synthetic fixture text."),
        ],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=dict(
            input_tokens=10,
            output_tokens=8,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens_details=dict(thinking_tokens=5),
        ),
    )


def transport(model, calls, payload=None, status=200):
    import httpx

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json=native(model) if payload is None else payload,
            headers={
                "x-request-id": "fixture-request",
                "request-id": "fixture-request",
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("model", MODELS)
def test_real_sdk_contract(model):
    from draftbench.provider_workflow import invoke

    calls = []
    p = policy(model)
    result = invoke(p, "synthetic", transport=transport(model, calls))
    assert result["status"] == "success", result
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body == native_request(p, "synthetic")
    assert not set(body) & {
        "temperature",
        "top_p",
        "top_k",
        "top_logprobs",
        "logprobs",
        "tools",
        "cache_control",
    }
    assert result["native_output"] == native(model)
    assert result["output"] == "Synthetic fixture text."
    assert result["served_model"] == model
    assert result["request_id"] == "fixture-request"
    assert result["provider_latency_seconds"] is None
    assert result["provenance"] == "fixture"
    if model.startswith("claude"):
        assert calls[0].url == "https://api.anthropic.com/v1/messages"
        assert calls[0].headers["anthropic-version"] == "2023-06-01"
        assert body["thinking"] == {"type": "adaptive"}
        assert body["max_tokens"] == 100
    else:
        assert calls[0].url == "https://api.openai.com/v1/chat/completions"
        assert body["reasoning_effort"] == "medium"
        assert body["max_completion_tokens"] == 100


@pytest.mark.parametrize("model", MODELS)
def test_mandatory_shared_campaign(model, tmp_path):
    from draftbench.provider_workflow import run_provider

    calls = []
    with pytest.raises(ValueError, match="campaign_required"):
        run_provider(
            Path(__file__).parents[1] / "examples/smoke/suite.json",
            tmp_path / "run",
            policy(model),
            transport=transport(model, calls),
        )
    assert not calls and not (tmp_path / "run").exists()


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("bad", [False, 0.0, "0", -1, [], {}])
def test_malformed_reasoning_is_unknown(model, bad):
    from draftbench.provider_workflow import invoke

    data = native(model)
    key = (
        "completion_tokens_details"
        if model.startswith("gpt")
        else "output_tokens_details"
    )
    detail = "reasoning_tokens" if model.startswith("gpt") else "thinking_tokens"
    data["usage"][key][detail] = bad
    result = invoke(policy(model), "fixture", transport=transport(model, [], data))
    assert result["status"] == "uncertain"
    assert result["usage"] is None and result["cost_upper_estimate"] is None
    assert result["native_output"] == data


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "failure",
    ["timeout", "429", "500", "redirect", "model", "id", "limit", "missing_usage"],
)
def test_failure_and_reasoning_limits(model, failure):
    import httpx

    from draftbench.provider_workflow import invoke

    data = native(model)
    calls = []
    if failure == "model":
        data["model"] = "not-approved"
    if failure == "id":
        data["id"] = False
    if failure == "missing_usage":
        data["usage"] = None
    if failure == "limit":
        if model.startswith("gpt"):
            data["choices"][0]["finish_reason"] = "length"
            data["choices"][0]["message"]["content"] = ""
        else:
            data["stop_reason"] = "max_tokens"
            data["content"] = data["content"][:1]

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "redirect":
            return httpx.Response(
                307, headers={"location": "https://forbidden.example"}
            )
        if failure in ("429", "500"):
            return httpx.Response(
                int(failure),
                json={"error": {"message": "private", "type": "api_error"}},
                headers={"request-id": "err", "x-request-id": "err"},
            )
        return httpx.Response(200, json=data)

    result = invoke(policy(model), "fixture", transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result["status"] == (
        "limited"
        if failure == "limit"
        else "success"
        if failure == "missing_usage"
        else "uncertain"
    )
    if failure not in ("limit",):
        assert result["cost_upper_estimate"] is None
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("model", MODELS)
def test_campaign_pause_resume_custody(model, tmp_path):
    from draftbench.campaign import CampaignBudget
    from draftbench.provider_reporting import prepare_openai, report_openai
    from draftbench.provider_workflow import resume_provider, run_provider
    from draftbench.reporting import build_report

    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    root = tmp_path / "run"
    calls = []
    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        first = run_provider(
            suite,
            root,
            policy(model),
            transport=transport(model, calls),
            campaign=campaign,
            max_steps=1,
        )
        assert first["states"]["completed"] == 1
        with pytest.raises(ValueError, match="campaign_required"):
            resume_provider(root, transport=transport(model, calls))
        final = resume_provider(
            root, transport=transport(model, calls), campaign=campaign
        )
        # Astra's full-context conservative quote admits two, not three.
        expected = 2 if model == "gpt-6-astra" else 3
        assert len(calls) == expected
        assert final["states"]["completed"] == expected
        before = campaign.summary()
        assert (
            resume_provider(root, transport=transport(model, calls), campaign=campaign)
            == final
        )
        assert campaign.summary() == before
    assert report_openai(root) == final
    rights = {
        "license": "CC0-1.0",
        "usage": "private",
        "authorization": "Synthetic fixtures only.",
    }
    prep = tmp_path / "prepared"
    prepare_openai(root, prep, rights)
    report = build_report(
        provider_run_dir=root,
        bundle_path=prep / "bundle.json",
        bindings_path=prep / "provider-bindings.json",
    )
    assert report
    assert not final["model_execution_performed"]


@pytest.mark.parametrize("model", MODELS)
def test_no_ambient_credential_reads(model, monkeypatch):
    import os

    from draftbench.provider_workflow import invoke

    original = os.environ.get

    def deny(key, *args):
        if key.startswith(("ANTHROPIC_", "OPENAI_")):
            pytest.fail("ambient provider configuration")
        return original(key, *args)

    monkeypatch.setattr(os.environ, "get", deny)
    assert (
        invoke(policy(model), "fixture", transport=transport(model, []))["status"]
        == "success"
    )


@pytest.mark.parametrize("model", MODELS)
def test_reservation_independent_decimal_context(model):
    from decimal import Inexact, Rounded, localcontext

    p = policy(model)
    expected = p.reservation_total(3)
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = context.traps[Rounded] = True
        assert p.reservation_total(3) == expected


@pytest.mark.parametrize("model", MODELS)
def test_uncertain_campaign_never_refunded(model, tmp_path):
    from draftbench.campaign import CampaignBudget
    from draftbench.provider_workflow import resume_provider, run_provider

    calls = []
    data = native(model)
    data["usage"] = {}
    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        root = tmp_path / "run"
        run_provider(
            Path(__file__).parents[1] / "examples/smoke/suite.json",
            root,
            policy(model),
            transport=transport(model, calls, data),
            campaign=campaign,
        )
        before = campaign.summary()
        result = resume_provider(
            root, transport=transport(model, calls), campaign=campaign
        )
        assert result["states"]["uncertain"] == 1
        assert len(calls) == 1
        assert before == campaign.summary()


def test_mixed_provider_campaign_exhaustion(tmp_path):
    from decimal import Decimal

    from draftbench.campaign import CampaignBudget
    from draftbench.provider_workflow import run_provider

    calls = []
    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        for n, model in enumerate(
            ("gpt-6-astra", "claude-opus-5-5", "claude-sonnet-5")
        ):
            run_provider(
                Path(__file__).parents[1] / "examples/smoke/suite.json",
                tmp_path / f"run{n}",
                policy(model),
                transport=transport(model, calls),
                campaign=campaign,
            )
        assert campaign.summary()["reservation_count"] == 2
        assert len(calls) == 2
        assert Decimal(campaign.summary()["reserved_usd"]) <= 50


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("value", [False, "1", 1.5, -1, {}, [], 101])
def test_strict_output_counter(model, value):
    from draftbench.provider_workflow import invoke

    data = native(model)
    if model.startswith("gpt"):
        data["usage"]["completion_tokens"] = value
    else:
        data["usage"]["output_tokens"] = value
    result = invoke(policy(model), "fixture", transport=transport(model, [], data))
    assert result["status"] == "uncertain"
    assert result["cost_upper_estimate"] is None


@pytest.mark.parametrize("model", MODELS)
def test_unverified_tariff_denied_before_live_authorization(model, tmp_path):
    from draftbench.campaign import CampaignBudget
    from draftbench.provider_workflow import run_provider

    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        with pytest.raises(ValueError, match="verified_tariff_required"):
            run_provider(
                Path(__file__).parents[1] / "examples/smoke/suite.json",
                tmp_path / "run",
                policy(model),
                campaign=campaign,
                approve=lambda binding: pytest.fail("unverified live approval"),
            )
        assert campaign.summary()["reservation_count"] == 0
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("model", MODELS)
def test_policy_rejects_sampling_and_wrong_tariff(model):
    values = policy(model).model_dump(mode="json")
    for change in (
        {"temperature": 1},
        {"max_output_tokens": True},
        {"model": model + "-latest"},
        {"input_per_million": "0.01"},
    ):
        with pytest.raises(ValueError):
            parse_policy({**values, **change})


def test_thinking_effort_support():
    for model in MODELS:
        key = "reasoning_effort" if model.startswith("gpt") else "effort"
        for effort in ("low", "medium", "high", "xhigh", "max"):
            assert parse_policy({**policy(model).model_dump(), key: effort})
    with pytest.raises(ValueError, match="astra_requires_reasoning"):
        parse_policy({**policy("gpt-6-astra").model_dump(), "reasoning_effort": "none"})
    with pytest.raises(ValueError, match="opus_requires_adaptive"):
        parse_policy({**policy("claude-opus-5-5").model_dump(), "thinking": "disabled"})
    for model in ("gpt-6-sol", "gpt-6-luna"):
        assert parse_policy({**policy(model).model_dump(), "reasoning_effort": "none"})
    assert parse_policy(
        {**policy("claude-sonnet-5").model_dump(), "thinking": "disabled"}
    )


@pytest.mark.parametrize("model", MODELS[3:])
def test_anthropic_cache_and_thinking_not_double_counted(model):
    from draftbench.provider_workflow import invoke

    data = native(model)
    data["usage"].update(
        cache_creation_input_tokens=7,
        cache_read_input_tokens=3,
        cache_creation={"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 3},
    )
    result = invoke(policy(model), "fixture", transport=transport(model, [], data))
    assert result["status"] == "success"
    assert result["cost_upper_estimate"] == str(
        policy(model).token_cost(10 + 2 * 7 + 3, 8)
    )
    data["usage"]["cache_creation"]["ephemeral_5m_input_tokens"] = False
    assert (
        invoke(policy(model), "fixture", transport=transport(model, [], data))["status"]
        == "uncertain"
    )


def _mixed_sdk_worker(path, run, model, start, queue):
    from draftbench.campaign import CampaignBudget
    from draftbench.provider_workflow import run_provider

    start.wait()
    calls = []
    with CampaignBudget.open(path) as campaign:
        result = run_provider(
            Path(__file__).parents[1] / "examples/smoke/suite.json",
            run,
            policy(model),
            transport=transport(model, calls),
            campaign=campaign,
            max_steps=1,
        )
    queue.put((len(calls), result["states"]["completed"]))


def test_real_process_mixed_sdk_admission(tmp_path):
    import multiprocessing
    from decimal import Decimal

    from draftbench.campaign import CampaignBudget

    path = tmp_path / "campaign"
    CampaignBudget.create(path).close()
    context = multiprocessing.get_context("spawn")
    start, queue = context.Event(), context.Queue()
    models = ("gpt-6-astra", "gpt-6-astra", "claude-opus-5-5", "claude-opus-5-5")
    processes = [
        context.Process(
            target=_mixed_sdk_worker,
            args=(path, tmp_path / f"run{i}", model, start, queue),
        )
        for i, model in enumerate(models)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=40) for _ in processes]
    for process in processes:
        process.join(40)
        assert process.exitcode == 0
    calls = sum(item[0] for item in results)
    assert calls == sum(item[1] for item in results)
    assert 2 <= calls <= 3
    with CampaignBudget.open(path) as campaign:
        assert campaign.summary()["reservation_count"] == calls
        assert Decimal(campaign.summary()["reserved_usd"]) <= 50


@pytest.mark.parametrize("model", MODELS[3:])
def test_explicit_global_route_and_usage_metadata(model):
    from draftbench.provider_workflow import invoke

    data = native(model)
    data["usage"].update(inference_geo="global", service_tier="standard")
    result = invoke(policy(model), "fixture", transport=transport(model, [], data))
    assert result["status"] == "success"
    assert result["native_request"]["inference_geo"] == "global"
    data["usage"]["inference_geo"] = "us"
    result = invoke(policy(model), "fixture", transport=transport(model, [], data))
    assert result["status"] == "uncertain" and result["cost_upper_estimate"] is None
