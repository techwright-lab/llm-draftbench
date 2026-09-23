"""Authorization/projection tests; host transport is explicitly replaced offline."""

import builtins
from pathlib import Path

import pytest
from test_openai_provider import policy, transport

from draftbench.adapters.openai import digest, invoke
from draftbench.provider_workflow import (
    DispatchAuthorization,
    prepare_openai,
    report_openai,
    resume_openai,
    run_openai,
)

SUITE = Path(__file__).parents[1] / "examples/smoke/suite.json"
RIGHTS = {
    "license": "test-only",
    "usage": "private",
    "authorization": "Unit test synthetic material only.",
}


def test_cannot_construct_live_permit():
    p = policy()
    permit = DispatchAuthorization(
        digest({"policy": p.model_dump(mode="json"), "prompt": "p"})
    )
    with pytest.raises(ValueError, match="live_authorization_required"):
        invoke(p, "p", authorization=permit, api_key="not-a-credential")


def test_denial_binds_policy_data_and_run_path(tmp_path):
    seen = []

    def reject(binding):
        seen.append(binding)
        return False

    for field, value in [
        (None, None),
        ("model", "gpt-4.1-mini-2025-04-14"),
        ("currency", "EUR"),
        ("max_cost", "51"),
        ("project", "proj_other"),
        ("max_requests", 2),
    ]:
        p = policy().model_dump()
        if field:
            p[field] = value
        with pytest.raises(ValueError, match="live_authorization_required"):
            run_openai(SUITE, tmp_path / "same-run", p, approve=reject)
    with pytest.raises(ValueError, match="live_authorization_required"):
        run_openai(SUITE, tmp_path / "another-run", policy(), approve=reject)
    assert len(set(seen)) == len(seen)
    assert not list(tmp_path.iterdir())


def test_native_provider_provenance_projection_with_stub(tmp_path, monkeypatch):
    """This is a host stub test, NOT evidence of provider execution/compatibility."""
    from draftbench.ledger import Ledger

    root = tmp_path / "run"
    calls = []

    def simulated_host(p, prompt, *, transport, authorization, api_key):
        assert transport is None and api_key == "unit-test-not-a-credential"
        assert authorization.consume(p, prompt)
        assert not authorization.consume(p, prompt)
        with Ledger.open(root / "ledger.sqlite3", readonly=True) as ledger:
            assert ledger.summary()["states"]["in_flight"] == 1
        calls.append(prompt)
        # Simulate metadata from the live-mode boundary, never present this unit
        # test's files as observed provider output.
        from draftbench.adapters.openai_fixture import fixture_transport

        result = invoke(p, prompt, transport=fixture_transport(p.model))
        result.update(provenance="provider", charge_status="unknown")
        return result

    monkeypatch.setattr("draftbench.provider_workflow.invoke", simulated_host)
    approved = []

    def approve(binding):
        approved.append(binding)
        return True

    report = run_openai(
        SUITE, root, policy(), approve=approve, api_key="unit-test-not-a-credential"
    )
    assert report["provenance"] == "provider" and report["complete"]
    assert len(calls) == 3
    assert "Synthetic" in calls[0]
    assert '"parent_outputs"' in calls[1]
    assert '"evaluator"' not in calls[0] and '"rights"' not in calls[0]
    original_import = builtins.__import__

    def deny_sdk(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai.") or name == "httpx":
            pytest.fail("SDK import during saved artifact operations")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_sdk)
    assert report_openai(root) == report
    prepare_openai(root, tmp_path / "prepared", RIGHTS)
    from draftbench.scoring.reporting import load_scoring_bundle, score_bundle

    bundle = load_scoring_bundle(tmp_path / "prepared/bundle.json")
    assert bundle.purpose == "evaluation"
    assert score_bundle(bundle)["model_execution_performed"] is False
    from draftbench.reporting import build_report

    rendered = build_report(bundle_path=tmp_path / "prepared/bundle.json")
    assert rendered
    with pytest.raises(ValueError, match="transport_provenance_mismatch"):
        # No fixture transport import under the SDK guard.
        resume_openai(root, transport=object())
    with pytest.raises(ValueError, match="live_authorization_required"):
        resume_openai(
            root, approve=lambda binding: False, api_key="unit-test-not-a-credential"
        )
    assert len(calls) == 3


@pytest.mark.parametrize(
    "stage,expected",
    [("reserved", 3), ("in_flight", 1), ("artifact_saved", 1), ("result_saved", 3)],
)
def test_actual_process_death(tmp_path, stage, expected):
    import json
    import os
    import subprocess
    import sys

    script = """
import json, os, socket, ssl, sys
from draftbench.provider_workflow import run_openai
from draftbench.adapters.openai_fixture import fixture_transport
def denied(*a, **k):
    raise AssertionError('network forbidden')
socket.socket = denied
socket.getaddrinfo = denied
socket.create_connection = denied
policy = json.loads(sys.argv[4])
def die(name, work):
    if name == sys.argv[3]:
        os._exit(77)
run_openai(sys.argv[1], sys.argv[2], policy, transport=fixture_transport(policy['model']), checkpoint=die)
"""
    root = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(SUITE),
            str(root),
            stage,
            json.dumps(policy().model_dump()),
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 77, result.stderr
    recovered = resume_openai(root, transport=transport())
    assert recovered["attempt_count"] == expected
    if expected == 1:
        assert recovered["states"]["uncertain"] == 1
    assert resume_openai(root, transport=transport()) == recovered


def test_saved_fixture_score_no_sdk(tmp_path, monkeypatch):
    run_openai(SUITE, tmp_path / "run", policy(), transport=transport())
    original_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name == "openai" or name == "httpx":
            pytest.fail("SDK import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    prepare_openai(tmp_path / "run", tmp_path / "prepared", RIGHTS)
    from draftbench.scoring.reporting import load_scoring_bundle, score_bundle

    bundle = load_scoring_bundle(tmp_path / "prepared/bundle.json")
    assert bundle.purpose == "synthetic_infrastructure"
    assert score_bundle(bundle)["case_count"] == 2
