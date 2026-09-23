# ruff: noqa: F811
import json

import pytest
from conftest import seal
from test_annotation import save
from test_offline_workflow import smoke_suite  # noqa: F401
from test_reporting import annotated, prepared, render_inputs  # noqa: F401

from draftbench.cli import main
from draftbench.release import check_release, export_release, projection
from draftbench.reporting import ReportError, build_report, read_report, write_report
from draftbench.workflow import run_synthetic


def approval_for(report_dir, selection, tmp_path):
    selected = save(tmp_path / "release-selection.json", selection)
    check = check_release(report_dir, selected)
    approval = {
        "format": "draftbench-release-approval-v1",
        "approved": True,
        "source_report_identity": check["source_report_identity"],
        "projection_digest": check["projection_digest"],
        "selection": selection,
        "reviewer_kind": "synthetic",
        "declared_reviewer": "FABRICATED-SYNTHETIC-REVIEWER",
        "rights_attestation": "SYNTHETIC test attestation only, not human approval.",
    }
    return save(tmp_path / "approval.json", approval), check


def test_full_cycle_annotation_release_and_reproduce(prepared, tmp_path):
    session, selection = annotated(prepared, tmp_path)
    report = build_report(
        **render_inputs(prepared), session=session, selection_path=selection
    )
    private = tmp_path / "private-report"
    write_report(report, private)
    chosen = report["evidence"][0]["evidence_id"]
    approval, check = approval_for(
        private,
        {
            "format": "draftbench-release-selection-v1",
            "panels": ["writer", "reviewer", "revision"],
            "evidence_ids": [chosen],
        },
        tmp_path,
    )
    public = tmp_path / "public-report"
    assert (
        main(
            [
                "release",
                "check",
                str(private),
                "--selection",
                str(tmp_path / "release-selection.json"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "release",
                "export",
                str(private),
                "--approval",
                str(approval),
                "--output",
                str(public),
            ]
        )
        == 0
    )
    exported = read_report(public)
    assert exported == check["projection"]
    assert len(exported["evidence"]) == 1
    assert (
        sum(sum(panel["operation_counts"].values()) for panel in exported["panels"])
        == 3
    )
    assert "SYNTHETIC" in exported["watermark"]
    text = "\n".join(path.read_text() for path in public.iterdir())
    for forbidden in (
        "PRIVATE-NAME",
        "private-fixture",
        "record_id",
        "case_id",
        'source_family"',
        "snapshot",
        "FABRICATED-SYNTHETIC-REVIEWER",
        "annotations.sqlite3",
        "request_digest",
    ):
        assert forbidden not in text
    assert read_report(private) == report
    replay = tmp_path / "replay"
    write_report(read_report(private), replay)
    assert check_release(replay, tmp_path / "release-selection.json") == check
    assert len(list(public.glob("evidence-*.json"))) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("projection_digest", "0" * 64),
        ("source_report_identity", "0" * 64),
        ("approved", False),
    ],
)
def test_mismatch_approval_fails(prepared, tmp_path, field, value):
    private = tmp_path / "private"
    write_report(build_report(**render_inputs(prepared)), private)
    approval, _ = approval_for(
        private,
        {
            "format": "draftbench-release-selection-v1",
            "panels": ["writer"],
            "evidence_ids": [],
        },
        tmp_path,
    )
    body = json.loads(approval.read_text())
    body[field] = value
    save(approval, body)
    with pytest.raises(ValueError):
        export_release(private, approval, tmp_path / "public")
    assert not (tmp_path / "public").exists()


def test_no_default_evidence_and_unknown_selector(prepared):
    report = build_report(**render_inputs(prepared))
    selection = {
        "format": "draftbench-release-selection-v1",
        "panels": [],
        "evidence_ids": [],
    }
    assert projection(report, selection)["evidence"] == []
    assert projection(report, selection)["panels"] == []
    with pytest.raises(ValueError):
        projection(report, {**selection, "panels": ["prompts"]})
    with pytest.raises(ValueError):
        projection(report, {**selection, "evidence_ids": ["unknown"]})


def test_private_component_rights_fail_closed(make_suite, case_data, tmp_path):
    case_data["rights"] = {
        "license": "private",
        "usage": "private",
        "authorization": "No redistribution",
    }
    case_data["source"]["artifact"]["rights"] = case_data["rights"]
    manifest = make_suite([seal(case_data)])
    suite = json.loads(manifest.read_text())
    suite["rights"] = case_data["rights"]
    save(manifest, seal(suite))
    run = tmp_path / "run"
    run_synthetic(manifest, run)
    report = build_report(run_dir=run)
    selection = {
        "format": "draftbench-release-selection-v1",
        "panels": ["writer"],
        "evidence_ids": [report["evidence"][0]["evidence_id"]],
    }
    with pytest.raises(ReportError, match="rights"):
        projection(report, selection)
    # Aggregate selection alone is still only a preview requiring exact approval.
    assert projection(report, {**selection, "evidence_ids": []})["evidence"] == []


def test_export_no_clobber_and_report_drift(prepared, tmp_path):
    private = tmp_path / "private"
    write_report(build_report(**render_inputs(prepared)), private)
    approval, _ = approval_for(
        private,
        {
            "format": "draftbench-release-selection-v1",
            "panels": ["writer"],
            "evidence_ids": [],
        },
        tmp_path,
    )
    public = tmp_path / "public"
    export_release(private, approval, public)
    with pytest.raises(ValueError):
        export_release(private, approval, public)
    with (private / "index.html").open("a") as stream:
        stream.write("drift")
    with pytest.raises(ValueError):
        export_release(private, approval, tmp_path / "drift")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("projection_digest", "0" * 64, "release_approval_mismatch"),
        ("approved", False, "report_or_release_invalid"),
    ],
)
def test_cli_release_surfaces_safe_error_code(
    prepared, tmp_path, capsys, field, value, code
):
    private = tmp_path / "private"
    write_report(build_report(**render_inputs(prepared)), private)
    approval, _ = approval_for(
        private,
        {
            "format": "draftbench-release-selection-v1",
            "panels": ["writer"],
            "evidence_ids": [],
        },
        tmp_path,
    )
    save(approval, {**json.loads(approval.read_text()), field: value})
    capsys.readouterr()
    status = main(
        [
            "release",
            "export",
            str(private),
            "--approval",
            str(approval),
            "--output",
            str(tmp_path / "public"),
        ]
    )
    assert status == 2
    assert json.loads(capsys.readouterr().err) == {"valid": False, "error": code}
