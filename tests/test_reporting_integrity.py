# ruff: noqa: F811
import copy
import json
from pathlib import Path

import pytest
from conftest import seal
from test_annotation import save
from test_offline_workflow import smoke_suite  # noqa: F401
from test_release import approval_for
from test_reporting import annotated, file_hashes, prepared, render_inputs  # noqa: F401

from draftbench.adapters.fake import AdapterLimited, FakeAdapter
from draftbench.annotation_store import AnnotationStore
from draftbench.release import ReleaseApproval, ReleaseSelection, export_release
from draftbench.reporting import (
    ReportError,
    build_report,
    prepare_report,
    read_report,
    write_report,
)
from draftbench.workflow import run_synthetic


@pytest.mark.parametrize("state", ["failed", "limited"])
def test_operational_failure_is_not_content_defect(
    smoke_suite, tmp_path, monkeypatch, state
):
    def fail(*args):
        raise (
            AdapterLimited()
            if state == "limited"
            else RuntimeError("synthetic failure")
        )

    monkeypatch.setattr(FakeAdapter, "invoke", fail)
    run = tmp_path / "run"
    run_synthetic(smoke_suite, run)
    report = build_report(run_dir=run)
    assert report["panels"][0]["operation_counts"] == {state: 1}
    assert report["panels"][0]["scoring"] is None
    assert report["panels"][1]["operation_counts"] == {"blocked": 1}
    assert report["evidence"] == []


def test_missing_outputs_are_status_only(make_suite, tmp_path):
    run = tmp_path / "run"
    run_synthetic(make_suite(), run)
    report = build_report(run_dir=run)
    assert report["panels"][1]["operation_counts"] == {"unavailable": 1}
    assert report["panels"][2]["operation_counts"] == {"unavailable": 1}


def test_family_count_not_observation_count(make_suite, case_data, tmp_path):
    second = copy.deepcopy(case_data)
    second["case_id"] = "second-case"
    run = tmp_path / "run"
    run_synthetic(make_suite([case_data, seal(second)]), run)
    prepare = tmp_path / "prepare"
    prepare_report(run, prepare)
    report = build_report(
        run_dir=run,
        bundle_path=prepare / "bundle.json",
        bindings_path=prepare / "bindings.json",
    )
    assert report["panels"][0]["source_family_count"] == 1
    assert report["panels"][0]["coverage"] == {"numerator": 2, "denominator": 2}
    assert report["panels"][0]["scoring"]["case_count"] == 2
    assert len(report["evidence"]) == 2


def test_report_reads_saved_files_not_original_sources(prepared, tmp_path):
    expected = build_report(**render_inputs(prepared))
    for name in ("suite.json", "cases.jsonl", "source.txt"):
        (tmp_path / name).unlink()
    assert build_report(**render_inputs(prepared)) == expected
    output = tmp_path / "report"
    write_report(expected, output)
    # Replay depends only on the frozen report, not a live run.
    (prepared[0] / "run.json").unlink()
    assert read_report(output) == expected


def test_tampered_normalized_record_rejected(prepared, tmp_path, monkeypatch):
    session, selection = annotated(prepared, tmp_path)
    original = AnnotationStore.records

    def corrupt(self, *args, **kwargs):
        rows = original(self, *args, **kwargs)
        rows[0]["body"]["basis"] = "historical_exact"
        return rows

    monkeypatch.setattr(AnnotationStore, "records", corrupt)
    with pytest.raises(ReportError, match="annotation_record_mismatch"):
        build_report(
            **render_inputs(prepared), session=session, selection_path=selection
        )


def test_corrupted_saved_run_is_rejected(prepared):
    report = build_report(**render_inputs(prepared))
    result_hash = report["evidence"][0]["result_digest"]
    # Locate the exact stored object without assuming store layout.
    objects = [p for p in (prepared[0] / "objects").rglob("*") if p.is_file()]
    target = next(p for p in objects if result_hash in str(p).replace("/", ""))
    target.write_bytes(b"{}")
    with pytest.raises(ValueError):
        build_report(**render_inputs(prepared))


@pytest.mark.parametrize(
    "field,value",
    [("approved", 1), ("declared_reviewer", "   "), ("rights_attestation", "\t")],
)
def test_attestation_must_be_explicit(prepared, tmp_path, field, value):
    private = tmp_path / "private"
    write_report(build_report(**render_inputs(prepared)), private)
    approval, _ = approval_for(
        private,
        {"format": "draftbench-release-selection-v1", "panels": [], "evidence_ids": []},
        tmp_path,
    )
    body = json.loads(approval.read_text())
    body[field] = value
    save(approval, body)
    with pytest.raises(ValueError):
        export_release(private, approval, tmp_path / "public")


def test_no_human_approval_fabricated_for_real_reference_report(tmp_path):
    source = Path(__file__).parents[1] / "examples/scoring/scoring.json"
    value = json.loads(source.read_text())
    value["purpose"] = "evaluation"
    bundle = save(tmp_path / "bundle.json", seal(value))
    report = build_report(bundle_path=bundle)
    private = tmp_path / "private"
    write_report(report, private)
    approval, _ = approval_for(
        private,
        {"format": "draftbench-release-selection-v1", "panels": [], "evidence_ids": []},
        tmp_path,
    )
    with pytest.raises(ReportError, match="synthetic_approval_requires"):
        export_release(private, approval, tmp_path / "public")


def test_planned_run_is_not_run(smoke_suite, tmp_path):
    run = tmp_path / "run"
    run_synthetic(smoke_suite, run, max_steps=0)
    report = build_report(run_dir=run)
    assert report["execution_status"] == "not_run"
    assert all(panel["status"] == "not_run" for panel in report["panels"])


def test_raw_evidence_html_and_csv_injection(smoke_suite, tmp_path, monkeypatch):
    original = FakeAdapter.invoke
    attack = ' \t=HYPERLINK("javascript:alert(1)")<script>alert(1)</script>'

    def injected(self, request):
        result = original(self, request)
        if result["units"]:
            result["units"][0]["content"] = attack
        return result

    monkeypatch.setattr(FakeAdapter, "invoke", injected)
    run = tmp_path / "run"
    run_synthetic(smoke_suite, run)
    report = build_report(run_dir=run)
    output = tmp_path / "report"
    write_report(report, output)
    assert "<script>" not in (output / "index.html").read_text()
    assert "&lt;script&gt;" in (output / "index.html").read_text()
    import csv

    with (output / "report.csv").open(newline="") as stream:
        rows = list(csv.reader(stream))
    assert any(value == "'" + attack for _, value in rows)
    assert all(
        'href="javascript:' not in path.read_text() for path in output.glob("*.html")
    )


def test_release_schemas_match_models():
    root = Path(__file__).parents[1] / "schemas"
    for name, model in [
        ("release-selection", ReleaseSelection),
        ("release-approval", ReleaseApproval),
    ]:
        assert (
            json.loads((root / f"{name}.schema.json").read_text())
            == model.model_json_schema()
        )
