# ruff: noqa: F811
import hashlib
import json

import pytest
from conftest import seal
from scoring_cases import producer
from test_annotation import answer_one, records, save, templates
from test_offline_workflow import smoke_suite  # noqa: F401

from draftbench.adapters.fake import FakeAdapter
from draftbench.annotation import create_annotation_session, import_annotation_responses
from draftbench.cli import main
from draftbench.reporting import (
    ReportError,
    build_report,
    csv_cell,
    prepare_report,
    read_report,
    write_report,
)
from draftbench.workflow import resume_synthetic, run_synthetic


@pytest.fixture
def prepared(smoke_suite, tmp_path):
    run = tmp_path / "run"
    run_synthetic(smoke_suite, run, max_steps=1)
    resume_synthetic(run)
    out = tmp_path / "prepared"
    prepare_report(run, out)
    return run, out


def render_inputs(prepared):
    run, out = prepared
    return dict(
        run_dir=run,
        bundle_path=out / "bundle.json",
        bindings_path=out / "bindings.json",
    )


def file_hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_integrated_readonly_report_replay(prepared, tmp_path, monkeypatch):
    before = file_hashes(prepared[0])

    def denied(*args):
        raise AssertionError("report must not invoke adapter")

    monkeypatch.setattr(FakeAdapter, "invoke", denied)
    report = build_report(**render_inputs(prepared))
    assert [panel["coverage"]["numerator"] for panel in report["panels"]] == [1, 0, 1]
    assert [panel["source_family_count"] for panel in report["panels"]] == [1, 1, 1]
    assert report["panels"][1]["quality"] == "not_assessed"
    assert report["annotation_coverage"] == {"numerator": 0, "denominator": 2}
    assert len(report["evidence"]) == 3
    assert (
        sum(sum(panel["operation_counts"].values()) for panel in report["panels"]) == 3
    )
    output = tmp_path / "report"
    write_report(report, output)
    assert read_report(output) == report
    write_report(read_report(output), tmp_path / "replay")
    assert file_hashes(output) == file_hashes(tmp_path / "replay")
    assert file_hashes(prepared[0]) == before
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


def annotated(prepared, tmp_path):
    _, out = prepared
    roster = save(
        tmp_path / "raters.json",
        seal(
            {
                "schema_version": "1",
                "format": "draftbench-raters-v1",
                "raters": [
                    {
                        "rater_key": "private-fixture",
                        "producer": producer(kind="synthetic", name="PRIVATE-NAME"),
                        "independence": "independent",
                    }
                ],
            }
        ),
    )
    session = tmp_path / "annotations"
    create_annotation_session(out / "bundle.json", out / "rubric.json", roster, session)
    response, _ = answer_one(templates(session)[0])
    import_annotation_responses(session, save(tmp_path / "response.json", response))
    record = records(session, tmp_path)[0]
    selection = save(
        tmp_path / "selection.json",
        {
            "format": "draftbench-annotation-selection-v1",
            "records": {record["body"]["case_id"]: record["record_id"]},
        },
    )
    return session, selection


def test_annotation_reference_is_explicit_and_not_human(prepared, tmp_path):
    session, selection = annotated(prepared, tmp_path)
    before = file_hashes(session)
    report = build_report(
        **render_inputs(prepared), session=session, selection_path=selection
    )
    assert report["annotation_coverage"] == {"numerator": 1, "denominator": 2}
    chosen = report["snapshot"]["annotation_selection"][0]
    assert chosen["condition"] == "explicit_rater_reference"
    assert any(
        row["producer_kind"] == "synthetic" and row["scope"] == "synthetic_reference"
        for row in report["reference_conditions"]
    )
    assert any(
        case["reference"]["acceptability"] == "acceptable"
        for case in report["snapshot"]["scoring_bundle"]["cases"]
    )
    assert file_hashes(session) == before


@pytest.mark.parametrize("stage", ["reserved", "in_flight", "result_saved"])
def test_unfinished_results_never_promoted(smoke_suite, tmp_path, stage):
    def stop(name, work):
        if name == stage:
            raise KeyboardInterrupt

    run = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run_synthetic(smoke_suite, run, checkpoint=stop)
    before = file_hashes(run)
    report = build_report(run_dir=run)
    assert report["execution_status"] == "incomplete"
    assert report["panels"][0]["operation_counts"] == {stage: 1}
    assert report["evidence"] == []
    with pytest.raises(ReportError, match="no_completed"):
        prepare_report(run, tmp_path / "prepared")
    assert file_hashes(run) == before


def test_scoring_only_not_run(prepared):
    report = build_report(bundle_path=prepared[1] / "bundle.json")
    assert report["execution_status"] == "not_run"
    assert all(panel["status"] == "not_run" for panel in report["panels"])
    assert report["scoring_only"]["case_count"] == 2
    assert len(report["evidence"]) == 2
    assert all(item["request_digest"] is None for item in report["evidence"])


def test_wrong_bindings_fail(prepared, tmp_path):
    values = render_inputs(prepared)
    with pytest.raises(ReportError):
        build_report(**{**values, "bindings_path": None})
    data = json.loads(values["bindings_path"].read_text())
    data["run_snapshot_digest"] = "0" * 64
    with pytest.raises(ReportError):
        build_report(**{**values, "bindings_path": save(tmp_path / "wrong.json", data)})


@pytest.mark.parametrize(
    "name",
    [
        "report.json",
        "report.csv",
        "index.html",
        "evidence-00000.json",
        "checksums.json",
    ],
)
def test_corrupt_report_rejected(prepared, tmp_path, name):
    out = tmp_path / "report"
    write_report(build_report(**render_inputs(prepared)), out)
    with (out / name).open("ab") as file:
        file.write(b"corrupt")
    with pytest.raises(ValueError):
        read_report(out)


def test_no_clobber_or_symlink(prepared, tmp_path):
    report = build_report(**render_inputs(prepared))
    out = tmp_path / "report"
    write_report(report, out)
    with pytest.raises(ValueError):
        write_report(report, out)
    link = tmp_path / "link"
    link.symlink_to(out, target_is_directory=True)
    with pytest.raises(ValueError):
        write_report(report, link)
    (out / "index.html").unlink()
    (out / "index.html").symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError):
        read_report(out)


@pytest.mark.parametrize(
    "text", ["=1+1", "+cmd", "-cmd", "@SUM(A1)", " \t=cmd", "\r=cmd", "\x00=cmd"]
)
def test_csv_formula_defense(text):
    assert csv_cell(text) == "'" + text


def test_untrusted_content_html(prepared, tmp_path):
    # A standalone supplied-reference bundle is permitted; no fake result invention.
    source = json.loads((prepared[1] / "bundle.json").read_text())
    from draftbench.scoring.models import artifact_digest

    case = source["cases"][0]
    case["artifact"]["units"][0]["content"] = (
        "<script>alert(1)</script> javascript:evil()"
    )
    case["artifact"]["sha256"] = artifact_digest(case["artifact"]["units"])
    case["reference"]["binding"]["artifact_sha256"] = case["artifact"]["sha256"]
    seal(case)
    path = save(tmp_path / "injected.json", seal(source))
    report = build_report(bundle_path=path)
    # Private snapshot is JSON only, never embedded as executable HTML.
    out = tmp_path / "report"
    write_report(report, out)
    page = (out / "index.html").read_text()
    assert "<script" not in page and "script-src 'none'" in page
    assert 'href="javascript:' not in page


def test_stale_adjudication_and_disagreement(prepared, tmp_path):
    session, selection = annotated(prepared, tmp_path)
    original = json.loads((tmp_path / "response.json").read_text())
    adjudication = json.loads(json.dumps(original))
    adjudication["format"] = "draftbench-adjudication-responses-v1"
    adjudication["answers"][0]["based_on"] = [original["answers"][0]["response_id"]]
    adjudication["answers"][0]["response_id"] = "adjudicated-record"
    import_annotation_responses(
        session, save(tmp_path / "adjudicate.json", adjudication), adjudication=True
    )
    chosen = json.loads(selection.read_text())
    case_id = next(iter(chosen["records"]))
    chosen["records"][case_id] = "adjudicated-record"
    adjudicated = save(tmp_path / "adjudicated-selection.json", chosen)
    assert (
        build_report(
            **render_inputs(prepared), session=session, selection_path=adjudicated
        )["annotation_choices"][0]["condition"]
        == "explicit_adjudication"
    )
    original["answers"][0]["response_id"] = "second-original"
    original["answers"][0]["decision"] = "unacceptable"
    import_annotation_responses(session, save(tmp_path / "second.json", original))
    with pytest.raises(ReportError, match="stale_adjudication"):
        build_report(
            **render_inputs(prepared), session=session, selection_path=adjudicated
        )
    report = build_report(
        **render_inputs(prepared), session=session, selection_path=selection
    )
    assert report["annotation_choices"][0]["disagreement"] is True
    assert report["annotation_choices"][0]["original_count"] == 2


@pytest.mark.parametrize("mutation", ["artifact", "rubric", "source", "family"])
def test_annotation_requires_exact_source_context(prepared, tmp_path, mutation):
    session, selection = annotated(prepared, tmp_path)
    source = json.loads((prepared[1] / "bundle.json").read_text())
    case_id = next(iter(json.loads(selection.read_text())["records"]))
    case = next(case for case in source["cases"] if case["case_id"] == case_id)
    if mutation == "artifact":
        from draftbench.scoring.models import artifact_digest

        case["artifact"]["units"][0]["content"] += " changed"
        case["artifact"]["sha256"] = artifact_digest(case["artifact"]["units"])
        case["reference"]["binding"]["artifact_sha256"] = case["artifact"]["sha256"]
    elif mutation == "rubric":
        case["rubric"]["checks"][0]["applicability"] = "optional"
    elif mutation == "source":
        case["sources_state"] = "partial"
    else:
        case["source_family"] = "changed-family"
    seal(case)
    modified = save(tmp_path / "modified.json", seal(source))
    with pytest.raises(ReportError, match="annotation_source_mismatch"):
        build_report(bundle_path=modified, session=session, selection_path=selection)


@pytest.mark.parametrize("change", ["abstain", "partial", "missing"])
def test_partial_labels_stay_unknown(prepared, tmp_path, change):
    session, selection = annotated(prepared, tmp_path)
    original = json.loads((tmp_path / "response.json").read_text())
    answer = original["answers"][0]
    answer["response_id"] = "partial-record"
    if change == "abstain":
        answer["decision"] = "abstain"
    elif change == "partial":
        answer["defect_inventory"] = "partial"
    else:
        answer["criteria"] = []
    import_annotation_responses(session, save(tmp_path / "partial.json", original))
    chosen = json.loads(selection.read_text())
    chosen["records"] = {case_id: "partial-record" for case_id in chosen["records"]}
    report = build_report(
        **render_inputs(prepared),
        session=session,
        selection_path=save(tmp_path / "partial-selection.json", chosen),
    )
    assert report["annotation_coverage"]["numerator"] == 0
    assert all(
        case["reference"]["acceptability"] == "unknown"
        for case in report["snapshot"]["scoring_bundle"]["cases"]
    )


def test_cli_reporting(prepared, tmp_path, capsys):
    run, out = prepared
    target = tmp_path / "report"
    assert (
        main(
            [
                "report",
                "render",
                "--run",
                str(run),
                "--bundle",
                str(out / "bundle.json"),
                "--bindings",
                str(out / "bindings.json"),
                "--output",
                str(target),
            ]
        )
        == 0
    )
    assert (
        main(["report", "replay", str(target), "--output", str(tmp_path / "replay")])
        == 0
    )
    assert read_report(target) == read_report(tmp_path / "replay")
