"""Served-model identity and live-path SDK logging guards; offline only."""

import json
import logging
from pathlib import Path

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")

from test_five_model_contracts import native, policy, transport  # noqa: E402

from draftbench.adapters.pilot_policy import served_model_matches  # noqa: E402
from draftbench.adapters.provider_contract import verify_result  # noqa: E402
from draftbench.provider_workflow import invoke  # noqa: E402

SUITE = Path(__file__).parents[1] / "examples/smoke/suite.json"


@pytest.mark.parametrize(
    "requested,served,expected",
    [
        ("gpt-6-luna", "gpt-6-luna", True),
        ("gpt-6-luna", "gpt-6-luna-2026-09-01", True),
        ("claude-sonnet-5", "claude-sonnet-5-20260901", True),
        ("gpt-6-luna", "gpt-6-sol", False),
        ("gpt-6-luna", "gpt-6-luna-mini", False),
        ("gpt-6-luna", "gpt-6-luna-2026-09", False),
        ("gpt-6-luna", "gpt-6-luna-2026-09-01-preview", False),
        ("gpt-6-luna", "gpt-6-lunar", False),
        ("gpt-6-luna", None, False),
    ],
)
def test_served_model_rule(requested, served, expected):
    assert served_model_matches(requested, served) is expected


@pytest.mark.parametrize(
    "model,served",
    [
        ("gpt-6-luna", "gpt-6-luna-2026-09-01"),
        ("gpt-6-sol", "gpt-6-sol-20260901"),
        ("claude-sonnet-5", "claude-sonnet-5-20260901"),
    ],
)
def test_dated_snapshot_accepted_and_recorded_separately(model, served):
    payload = native(model)
    payload["model"] = served
    p = policy(model)
    result = invoke(p, "synthetic", transport=transport(model, [], payload))
    assert result["status"] == "success", result
    assert result["requested_model"] == model
    assert result["served_model"] == served
    result.update(request_digest="0" * 64, parent_digests={})
    verify_result(result, p, "synthetic", "fixture")


@pytest.mark.parametrize("model", ["gpt-6-luna", "claude-sonnet-5"])
@pytest.mark.parametrize("served", ["gpt-6-astra", "claude-opus-5-5", "other"])
def test_other_served_model_rejected(model, served):
    payload = native(model)
    payload["model"] = served
    result = invoke(policy(model), "synthetic", transport=transport(model, [], payload))
    assert result["status"] == "uncertain"
    assert result["charge_status"] == "unknown"


def test_saved_result_cannot_claim_other_served_model():
    model = "claude-sonnet-5"
    p = policy(model)
    result = invoke(p, "synthetic", transport=transport(model, []))
    result.update(
        request_digest="0" * 64,
        parent_digests={},
        served_model="claude-sonnet-5-20260901",
    )
    with pytest.raises(ValueError, match="invalid_provider_result"):
        verify_result(result, p, "synthetic", "fixture")


@pytest.mark.parametrize("geo,status", [("global", "success"), ("us", "uncertain")])
def test_anthropic_inference_geo(geo, status):
    model = "claude-sonnet-5"
    payload = native(model)
    payload["usage"]["inference_geo"] = geo
    result = invoke(policy(model), "synthetic", transport=transport(model, [], payload))
    assert result["status"] == status


@pytest.mark.parametrize("module", ["openai", "anthropic"])
@pytest.mark.parametrize("variable", ["OPENAI_LOG", "ANTHROPIC_LOG"])
def test_live_preflight_refuses_sdk_log_env(monkeypatch, module, variable):
    from importlib import import_module

    preflight = import_module(f"draftbench.adapters.{module}").preflight
    monkeypatch.setenv(variable, "debug")
    with pytest.raises(ValueError, match="^sdk_debug_logging_forbidden$"):
        preflight(None)
    monkeypatch.delenv(variable)
    preflight(None)


@pytest.mark.parametrize("name", ["openai", "anthropic", "httpx", "httpcore"])
def test_live_preflight_refuses_debug_logger(name):
    from draftbench.adapters.openai import preflight

    logger = logging.getLogger(name)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(ValueError, match="^sdk_debug_logging_forbidden$"):
            preflight(None)
    finally:
        logger.setLevel(level)


def test_fixture_path_ignores_log_env(monkeypatch):
    monkeypatch.setenv("OPENAI_LOG", "debug")
    model = "gpt-6-luna"
    result = invoke(policy(model), "synthetic", transport=transport(model, []))
    assert result["status"] == "success"


def test_live_run_refused_before_reservation(tmp_path, monkeypatch):
    from draftbench.campaign import CampaignBudget
    from draftbench.ledger import Ledger
    from draftbench.provider_workflow import run_provider

    p = policy("gpt-6-luna").model_copy(update={"verified_tariff_digest": "a" * 64})
    monkeypatch.setenv("OPENAI_LOG", "debug")
    monkeypatch.setattr(
        "draftbench.provider_workflow.invoke", lambda *a, **k: pytest.fail("dispatch")
    )
    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        with pytest.raises(ValueError, match="^sdk_debug_logging_forbidden$"):
            run_provider(
                SUITE,
                tmp_path / "run",
                p,
                approve=lambda binding: True,
                api_key="unit-test-not-a-credential",
                campaign=campaign,
            )
        assert campaign.summary()["reservation_count"] == 0
    with Ledger.open(tmp_path / "run" / "ledger.sqlite3", readonly=True) as ledger:
        assert ledger.summary()["attempt_count"] == 0


def test_pilot_cli_reports_logging_refusal(tmp_path, monkeypatch, capsys):
    from draftbench.cli import main

    monkeypatch.setenv("ANTHROPIC_LOG", "info")
    status = main(
        [
            "pilot",
            "run",
            "--plan",
            str(tmp_path / "plan.json"),
            "--env-file",
            str(tmp_path / ".env"),
            "--approve",
            "0" * 64,
        ]
    )
    assert status == 2
    assert json.loads(capsys.readouterr().err) == {
        "valid": False,
        "error": "sdk_debug_logging_forbidden",
    }


def test_provider_evidence_release_refused_explicitly(tmp_path):
    from draftbench.campaign import CampaignBudget
    from draftbench.provider_reporting import prepare_provider
    from draftbench.provider_workflow import run_provider
    from draftbench.release import projection
    from draftbench.reporting import ReportError, build_report

    model = "gpt-6-luna"
    run, prepared = tmp_path / "run", tmp_path / "prepared"
    with CampaignBudget.create(tmp_path / "campaign") as campaign:
        run_provider(
            SUITE, run, policy(model), transport=transport(model, []), campaign=campaign
        )
    prepare_provider(
        run,
        prepared,
        {
            "license": "CC0-1.0",
            "usage": "redistributable",
            "authorization": "Synthetic fixture output declared redistributable.",
        },
    )
    report = build_report(
        provider_run_dir=run,
        bundle_path=prepared / "bundle.json",
        bindings_path=prepared / "provider-bindings.json",
    )
    selection = {
        "format": "draftbench-release-selection-v1",
        "panels": ["writer"],
        "evidence_ids": [report["evidence"][0]["evidence_id"]],
    }
    with pytest.raises(ReportError, match="^provider_evidence_rights_unbound$"):
        projection(report, selection)
    aggregate = projection(report, {**selection, "evidence_ids": []})
    assert aggregate["evidence"] == [] and len(aggregate["panels"]) == 1
