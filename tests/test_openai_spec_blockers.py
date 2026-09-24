"""Offline wire and saved-preparation custody regressions."""

import json

import pytest
from replay_fixtures import (
    PROMPT,
    SIDECAR_CANARY,
    SUITE,
    openai_native,
    openai_usage,
    replay_for_run,
)

from draftbench.adapters.openai import invoke
from draftbench.adapters.pilot_policy import GPT6Policy
from draftbench.provider_workflow import prepare_openai, resume_openai, run_openai
from draftbench.reporting import build_report, read_report, write_report

httpx = pytest.importorskip("httpx")
pytest.importorskip("openai")


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
            max_requests=4,
            max_total_tokens=4000000,
            pricing_provenance="public-docs-2026-09-23-v1",
            reasoning_effort="low",
            verified_tariff_digest="a" * 64,
        )
        | changes
    )


def response():
    return openai_native("gpt-6-luna", usage=openai_usage(10, 4, 5, 2))


def transport():
    from draftbench.adapters.openai_fixture import fixture_transport

    return fixture_transport(policy().model)


def complete_run(run, p, campaign):
    run_openai(SUITE, run, p, transport=transport(), campaign=campaign)
    return resume_openai(
        run,
        transport=transport(),
        campaign=campaign,
        revision_input=replay_for_run(run),
    )


@pytest.mark.parametrize(
    "details,field",
    [
        ("input_tokens_details", "cached_tokens"),
        ("input_tokens_details", "audio_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "audio_tokens"),
        ("input_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "cached_tokens"),
    ],
)
@pytest.mark.parametrize("value", [False, True, 0.0, "0", -1])
def test_wire_detail_counters_are_exact_integers(details, field, value):
    native = response()
    native["usage"][details][field] = value
    result = invoke(
        policy(),
        PROMPT,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=native)),
    )
    assert result["status"] == "uncertain"
    assert result["native_output"] == native
    assert type(result["native_output"]["usage"][details][field]) is type(value)
    assert result["cost_upper_estimate"] is None
    assert result["usage"] is None
    assert result["charge_status"] == "unknown"


@pytest.fixture
def prepared(tmp_path, campaign):
    run = tmp_path / "run"
    target = tmp_path / "prepared"
    assert complete_run(run, policy(), campaign)["complete"]
    rights = json.loads(SUITE.read_text())["rights"]
    prepare_openai(run, target, rights)
    return run, target


@pytest.mark.parametrize("sidecar", ["missing.json", "provider-bindings.json"])
def test_supplied_bindings_cannot_be_ignored_without_run(prepared, sidecar):
    _, target = prepared
    with pytest.raises((ValueError, OSError)):
        build_report(bundle_path=target / "bundle.json", bindings_path=target / sidecar)


def test_provider_prepared_report_roundtrip(prepared, tmp_path):
    run, target = prepared
    before = {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    report = build_report(
        provider_run_dir=run,
        bundle_path=target / "bundle.json",
        bindings_path=target / "provider-bindings.json",
    )
    assert report["custody"] == "verified_provider_preparation"
    assert report["provenance"] == "fixture"
    assert report["model_execution_performed"] is False
    write_report(report, tmp_path / "report")
    assert read_report(tmp_path / "report") == report
    assert before == {
        p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()
    }
    standalone = build_report(bundle_path=target / "bundle.json")
    assert standalone["custody"] == "standalone"
    assert standalone["provenance"] == "supplied_artifact"
    assert report["execution_status"] == "complete"
    assert all(p["configuration"] == "openai-v1" for p in report["panels"])


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_run",
        "missing_sidecar",
        "invalid_sidecar",
        "tampered_sidecar",
        "swapped_run",
        "swapped_sidecar",
        "swapped_bundle",
        "tampered_artifact",
        "missing_artifact",
        "tampered_bundle",
        "changed_policy",
        "changed_context",
    ],
)
def test_preparation_custody_fails_closed(prepared, tmp_path, mutation, campaign):
    from draftbench.provider_reporting import snapshot_provider
    from draftbench.reporting import identified
    from draftbench.scoring.models import artifact_digest

    run, target = prepared
    bundle_path, bindings_path = (
        target / "bundle.json",
        target / "provider-bindings.json",
    )
    snapshot = snapshot_provider(run)
    if mutation == "missing_run":
        run = tmp_path / "missing"
    elif mutation == "missing_sidecar":
        bindings_path.unlink()
    elif mutation == "invalid_sidecar":
        bindings_path.write_text("[]")
    elif mutation == "tampered_sidecar":
        sidecar = json.loads(bindings_path.read_text())
        sidecar["provenance"] = "provider"
        bindings_path.write_text(json.dumps(sidecar))
    elif mutation.startswith("swapped"):
        other = tmp_path / "other"
        changed = policy().model_copy(update={"account_route": "other-fixture"})
        complete_run(other, changed, campaign)
        prepare_openai(
            other, tmp_path / "other-prepared", json.loads(SUITE.read_text())["rights"]
        )
        if mutation == "swapped_run":
            run = other
        elif mutation == "swapped_sidecar":
            bindings_path = tmp_path / "other-prepared/provider-bindings.json"
        else:
            # Different text, but internally valid and re-sealed bundle.
            bundle = json.loads(bundle_path.read_text())
            case = bundle["cases"][0]
            case["artifact"]["units"][0]["content"] = "different text"
            sha = artifact_digest(case["artifact"]["units"])
            case["artifact"]["sha256"] = sha
            case["reference"]["binding"]["artifact_sha256"] = sha
            bundle["cases"][0] = identified(case)
            bundle_path.write_text(json.dumps(identified(bundle)))
    elif mutation in ("tampered_artifact", "missing_artifact"):
        digest = snapshot["work"][0]["result_digest"]
        artifact = next(
            p
            for p in (run / "objects").rglob("*")
            if p.is_file() and digest in str(p).replace("/", "")
        )
        if mutation == "missing_artifact":
            artifact.unlink()
        else:
            artifact.write_text("{}")
    elif mutation == "tampered_bundle":
        bundle = json.loads(bundle_path.read_text())
        bundle["purpose"] = "evaluation"
        bundle_path.write_text(json.dumps(identified(bundle)))
    else:
        # Replace the saved manifest AND its ledger pointer with a valid hash.
        # Old sidecar/output/context bindings must still reject the substituted run.
        import sqlite3

        from draftbench.store import ArtifactStore

        manifest = snapshot["manifest"]
        if mutation == "changed_policy":
            manifest["policy"]["max_cost"] = "3"
        else:
            case = next(iter(manifest["inputs"]))
            manifest["inputs"][case]["writer"]["tampered"] = True
        new_digest = ArtifactStore(run / "objects").put_json(manifest)
        with sqlite3.connect(run / "ledger.sqlite3") as db:
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='metadata_no_update'"
            ).fetchone()[0]
            db.execute("DROP TRIGGER metadata_no_update")
            db.execute(
                "UPDATE metadata SET manifest_digest=? WHERE singleton=1", (new_digest,)
            )
            db.execute(trigger)
    with pytest.raises((ValueError, OSError)):
        build_report(
            provider_run_dir=run, bundle_path=bundle_path, bindings_path=bindings_path
        )


@pytest.mark.parametrize("field", ["policy", "context", "output", "binding"])
def test_replay_revalidates_frozen_provider_custody(prepared, field):
    from copy import deepcopy

    from draftbench.reporting import normalize_report

    run, target = prepared
    report = build_report(
        provider_run_dir=run,
        bundle_path=target / "bundle.json",
        bindings_path=target / "provider-bindings.json",
    )
    frozen = deepcopy(report["snapshot"])
    provider = frozen["provider_run"]
    if field == "policy":
        provider["manifest"]["policy"]["currency"] = "EUR"
    elif field == "context":
        provider["work"][0]["request"]["input"] = {}
    elif field == "output":
        provider["work"][0]["result"]["output"] = "substitution"
    else:
        frozen["bindings"]["snapshot_digest"] = "0" * 64
    with pytest.raises(ValueError):
        normalize_report(frozen)


def test_provider_provenance_is_not_relabelled_synthetic(prepared):
    # Construct saved-provider contract data locally, NOT a provider call.
    from draftbench.adapters.openai_contract import native_request
    from draftbench.provider_reporting import (
        provider_bundle,
        snapshot_provider,
        work_prompt,
    )
    from draftbench.reporting import digest, normalize_report

    run, _ = prepared
    snapshot = snapshot_provider(run)
    snapshot["manifest"]["provenance"] = "provider"
    snapshot["manifest_digest"] = digest(snapshot["manifest"])
    by_id = {}
    for row in snapshot["work"]:
        request = row["request"]
        request["parent_digests"] = {
            dep: by_id[dep]["result_digest"] for dep in row["dependencies"]
        }
        request["parent_outputs"] = {
            dep: by_id[dep]["result"]["output"] for dep in row["dependencies"]
        }
        row["request_digest"] = digest(request)
        result = row["result"]
        result.update(
            provenance="provider",
            charge_status="unreconciled" if result["usage"] is not None else "unknown",
            request_digest=row["request_digest"],
            parent_digests=request["parent_digests"],
            native_request=native_request(policy(), work_prompt(row, request)),
        )
        row["result_digest"] = digest(result)
        by_id[row["work_id"]] = row
    bundle, sidecar = provider_bundle(snapshot, json.loads(SUITE.read_text())["rights"])
    assert bundle.purpose == "evaluation"
    report = normalize_report(
        {
            "run": None,
            "provider_run": snapshot,
            "source_bundle": bundle.model_dump(mode="json"),
            "bindings": sidecar,
            "scoring_bundle": bundle.model_dump(mode="json"),
            "annotation_selection": [],
        }
    )
    assert report["provenance"] == "provider"
    assert report["model_execution_performed"] is True
    assert report["execution_status"] == "complete"
    assert "SYNTHETIC" not in report["watermark"]
    assert report["snapshot"]["source_bundle"]["purpose"] == "evaluation"


def test_malformed_usage_retains_reservation_and_native_receipt(tmp_path, campaign):
    from draftbench.provider_workflow import resume_openai
    from draftbench.store import ArtifactStore

    native = response()
    native["usage"]["output_tokens_details"]["audio_tokens"] = False
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=native)

    run = tmp_path / "run"
    first = run_openai(
        SUITE, run, policy(), transport=httpx.MockTransport(handler), campaign=campaign
    )
    assert first["states"]["uncertain"] == 1
    assert first["reserved_cost"] == str(policy().reservation_cost)
    assert first["reserved_token_units"] == policy().reserved_tokens
    assert (
        resume_openai(run, transport=httpx.MockTransport(handler), campaign=campaign)
        == first
    )
    assert len(calls) == 1
    receipt = json.loads(next(run.glob("attempt-*.json")).read_text())
    result = ArtifactStore(run / "objects").get_json(receipt["native_digest"])
    assert result["native_output"] == native
    assert result["cost_upper_estimate"] is None
    assert result["charge_status"] == "unknown"


def test_source_sidecar_never_in_envelope_or_wire(prepared):
    from draftbench.provider_reporting import snapshot_provider

    run, _ = prepared
    snapshot = snapshot_provider(run)
    assert len(snapshot["work"]) == 4
    assert SIDECAR_CANARY not in json.dumps(snapshot["manifest"]["inputs"])
    for row in snapshot["work"]:
        assert SIDECAR_CANARY not in json.dumps(row["request"])
        assert SIDECAR_CANARY not in json.dumps(row["result"]["native_request"])


def test_reporting_import_boundary_in_fresh_interpreter(prepared, tmp_path):
    import subprocess
    import sys

    run, target = prepared
    script = r"""
import sys, importlib.abc
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('openai', 'httpx', 'draftbench.adapters.openai', 'draftbench.provider_workflow'):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Deny())
from draftbench.cli import main
run, prepared, output, replay = sys.argv[1:]
assert main(['openai', 'report', run]) == 0
assert main(['report', 'render', '--provider-run', run, '--bundle', prepared+'/bundle.json',
             '--bindings', prepared+'/provider-bindings.json', '--output', output]) == 0
assert main(['report', 'replay', output, '--output', replay]) == 0
from pathlib import Path
assert Path(output, 'report.json').read_bytes() == Path(replay, 'report.json').read_bytes()
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(run),
            str(target),
            str(tmp_path / "report"),
            str(tmp_path / "replay"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
