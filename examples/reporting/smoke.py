"""Credential-free SYNTHETIC end-to-end CLI exercise; fabricates no real labels.

Run: python examples/reporting/smoke.py /absolute/new/private-directory
Requires installed draftbench (or uv run --offline python ... from checkout).
"""

import json
import sys
from pathlib import Path

from draftbench.annotation import _write_new
from draftbench.cli import main
from draftbench.identity import canonical_bytes
from draftbench.reporting import identified, read_json, read_report
from draftbench.workflow import _directory


def exercise(output):
    root = _directory(output, create=True)
    suite = Path(__file__).resolve().parents[1] / "smoke" / "suite.json"

    def command(*args, expected=0):
        actual = main([str(arg) for arg in args])
        if actual != expected:
            raise RuntimeError("synthetic_smoke_command_failed")

    def save(name, value):
        path = root / name
        _write_new(path, canonical_bytes(value))
        return path

    run, prepared, session, report = [
        root / name for name in ("run", "prepared", "annotations", "report")
    ]
    command(
        "run", suite, "--adapter", "fake", "--output", run, "--max-steps", 1, expected=3
    )
    command("resume", run)
    command("report", "prepare", run, "--output", prepared)
    observed = {"state": "known", "value": "SYNTHETIC fabricated fixture"}
    roster = save(
        "raters.json",
        identified(
            {
                "format": "draftbench-raters-v1",
                "raters": [
                    {
                        "rater_key": "synthetic-fixture",
                        "independence": "same_system",
                        "producer": {
                            "kind": "synthetic",
                            "name": observed,
                            "version": {"state": "known", "value": "1"},
                            "requested_model": {"state": "inapplicable", "value": None},
                            "served_model": {"state": "inapplicable", "value": None},
                        },
                    }
                ],
            }
        ),
    )
    command(
        "review",
        "export",
        prepared / "bundle.json",
        "--rubric",
        prepared / "rubric.json",
        "--raters",
        roster,
        "--output",
        session,
    )
    template = next((session / "packets").glob("*.responses.json"))
    batch = read_json(template)
    packet = read_json(
        template.with_name(template.name.replace(".responses.json", ".json"))
    )
    tasks = {task["task_id"]: task for task in packet["tasks"]}
    for answer in batch["answers"]:
        answer.update(
            status="answered",
            decision="acceptable",
            rated_at="2000-01-01T00:00:00Z",
            criteria=[
                {"check_id": criterion["criterion_id"], "state": "pass"}
                for criterion in tasks[answer["task_id"]]["criteria"]
            ],
            defect_inventory="complete",
            defects=[],
            notes="Intentionally fabricated SYNTHETIC label, not human evaluation.",
        )
    responses = save("synthetic-responses.json", batch)
    command("review", "import", session, responses)
    command("review", "records", session, "--output", root / "private-records.json")
    records = read_json(root / "private-records.json")["records"]
    selection = save(
        "annotation-selection.json",
        {
            "format": "draftbench-annotation-selection-v1",
            "records": {
                record["body"]["case_id"]: record["record_id"] for record in records
            },
        },
    )
    command(
        "report",
        "render",
        "--run",
        run,
        "--bundle",
        prepared / "bundle.json",
        "--bindings",
        prepared / "bindings.json",
        "--session",
        session,
        "--selection",
        selection,
        "--output",
        report,
    )
    frozen = read_report(report)
    release_selection = save(
        "release-selection.json",
        {
            "format": "draftbench-release-selection-v1",
            "panels": ["writer", "reviewer", "revision"],
            "evidence_ids": [frozen["evidence"][0]["evidence_id"]],
        },
    )
    command("release", "check", report, "--selection", release_selection)
    from draftbench.release import check_release

    preview = check_release(report, release_selection)
    approval = save(
        "synthetic-approval.json",
        {
            "format": "draftbench-release-approval-v1",
            "approved": True,
            "source_report_identity": preview["source_report_identity"],
            "projection_digest": preview["projection_digest"],
            "selection": preview["selection"],
            "reviewer_kind": "synthetic",
            "declared_reviewer": "SYNTHETIC smoke fixture, NOT HUMAN approval",
            "rights_attestation": "Synthetic fixture components are marked redistributable. Infrastructure test only.",
        },
    )
    command(
        "release",
        "export",
        report,
        "--approval",
        approval,
        "--output",
        root / "public-local",
    )
    command("report", "replay", report, "--output", root / "replayed")
    assert read_report(root / "replayed") == frozen
    assert read_report(root / "public-local") == preview["projection"]
    print(
        json.dumps(
            {
                "synthetic_smoke_verified": True,
                "report_identity": frozen["identity"],
                "projection_digest": preview["projection_digest"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    exercise(sys.argv[1])
