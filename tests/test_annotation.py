import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import seal
from scoring_cases import producer

from draftbench.annotation import (
    annotation_status,
    create_annotation_session,
    export_annotation_records,
    import_annotation_responses,
)
from draftbench.annotation_models import AnnotationError
from draftbench.annotation_store import AnnotationStoreError
from draftbench.cli import main


@pytest.fixture
def annotation_inputs(tmp_path):
    source = Path(__file__).parents[1] / "examples/scoring/scoring.json"
    rubric = tmp_path / "rubric.json"
    rubric.write_text(
        json.dumps(
            seal(
                {
                    "schema_version": "1",
                    "format": "draftbench-annotation-rubric-v1",
                    "rubric_id": "synthetic-reference-rubric",
                    "rubric_version": "1",
                    "instructions": "Independently assess the draft and supplied evidence. Mark unknown when evidence is insufficient.",
                    "criteria": [
                        {
                            "criterion_id": "body-present",
                            "question": "Does the artifact contain substantive text?",
                        },
                        {
                            "criterion_id": "launch-date",
                            "question": "Are the date claims supported by the supplied evidence?",
                        },
                    ],
                }
            )
        )
    )
    roster = tmp_path / "raters.json"
    roster.write_text(
        json.dumps(
            seal(
                {
                    "schema_version": "1",
                    "format": "draftbench-raters-v1",
                    "raters": [
                        {
                            "rater_key": "private-editor-a",
                            "producer": producer(name="PRIVATE-ALICE"),
                            "independence": "independent",
                        },
                        {
                            "rater_key": "private-editor-b",
                            "producer": producer(name="PRIVATE-BOB"),
                            "independence": "independent",
                        },
                    ],
                }
            )
        )
    )
    return source, rubric, roster


@pytest.fixture
def annotation_session(tmp_path, annotation_inputs):
    out = tmp_path / "session"
    result = create_annotation_session(*annotation_inputs, out)
    assert result["packet_count"] == 2
    assert result["assignment_count"] == 2 * 4
    return out


def templates(session):
    return sorted((session / "packets").glob("*.responses.json"))


def answer_one(template, *, decision="acceptable", response_id=None):
    value = json.loads(template.read_text())
    packet_path = template.with_name(template.name.replace(".responses.json", ".json"))
    packet = json.loads(packet_path.read_text())
    answer = value["answers"][0]
    task = next(
        task for task in packet["tasks"] if task["task_id"] == answer["task_id"]
    )
    answer.update(
        status="answered",
        decision=decision,
        rated_at=datetime.now(timezone.utc).isoformat(),
        criteria=[
            {
                "check_id": item["criterion_id"],
                "state": "inapplicable"
                if item["applicability"] == "inapplicable"
                else "pass",
            }
            for item in task["criteria"]
        ],
        defect_inventory="complete",
        defects=[],
        notes="Synthetic annotation fixture, not a real human judgment.",
    )
    if response_id is not None:
        answer["response_id"] = response_id
    value["answers"] = [answer]
    return value, task


def save(path, value):
    path.write_text(json.dumps(value))
    return path


def records(session, tmp_path, name="records.json"):
    path = tmp_path / name
    export_annotation_records(session, path)
    return json.loads(path.read_text())["records"]


def test_blind_packets_have_no_prior_labels_or_private_identity(annotation_session):
    packets = annotation_session / "packets"
    for path in packets.glob("*.json"):
        text = path.read_text()
        for marker in (
            "PRIVATE-ALICE",
            "PRIVATE-BOB",
            "private-editor-a",
            "private-editor-b",
            "synthetic-reviewer",
            "date-defect",
            "reported-date",
            "Synthetic reference-date mismatch",
            "scoring-fixture-",
        ):
            assert marker not in text
    assert annotation_session.stat().st_mode & 0o777 == 0o700
    for path in annotation_session.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_unanswered_templates_do_not_create_fake_labels(annotation_session):
    result = import_annotation_responses(
        annotation_session, templates(annotation_session)[0]
    )
    assert result["inserted"] == 0
    status = annotation_status(annotation_session)
    assert status["original_records"] == 0
    assert status["pending_assignments"] == status["assignment_count"]


def test_originals_are_idempotent_and_conflicts_do_not_replace(
    annotation_session, tmp_path
):
    response, task = answer_one(templates(annotation_session)[0])
    path = save(tmp_path / "response.json", response)
    assert import_annotation_responses(annotation_session, path)["inserted"] == 1
    original = records(annotation_session, tmp_path)
    assert import_annotation_responses(annotation_session, path)["duplicates"] == 1
    assert records(annotation_session, tmp_path, "again.json") == original
    saved = original[0]
    assert saved["kind"] == "original"
    assert saved["body"]["basis"] == "synthetic"
    assert saved["body"]["producer"]["name"]["value"] in {
        "PRIVATE-ALICE",
        "PRIVATE-BOB",
    }
    assert saved["body"]["rated_at"] == response["answers"][0]["rated_at"]
    assert saved["body"]["coverage"]["complete"] is True
    response["answers"][0]["decision"] = "unacceptable"
    save(path, response)
    with pytest.raises((AnnotationError, AnnotationStoreError, ValueError)):
        import_annotation_responses(annotation_session, path)
    assert records(annotation_session, tmp_path, "after-conflict.json") == original


@pytest.mark.parametrize(
    "field",
    [
        "packet_identity",
        "task_order",
        "task_identity",
        "artifact_sha256",
        "rubric_digest",
        "rater_id",
    ],
)
def test_stale_bindings_and_presentation_order_fail_atomically(
    annotation_session, tmp_path, field
):
    value, _ = answer_one(templates(annotation_session)[0])
    if field == "task_order":
        value[field].reverse()
    elif field in ("packet_identity", "rater_id"):
        value[field] = "f" * 64
    else:
        value["answers"][0][field] = "f" * 64
    with pytest.raises((AnnotationError, ValueError)):
        import_annotation_responses(
            annotation_session, save(tmp_path / "bad.json", value)
        )
    assert annotation_status(annotation_session)["original_records"] == 0


def test_bad_span_rejects_whole_batch(annotation_session, tmp_path):
    value, _ = answer_one(templates(annotation_session)[0])
    value["answers"][0]["defects"] = [
        {
            "finding_id": "d1",
            "kind": "defect",
            "category": "factual",
            "severity": "major",
            "description": "Synthetic",
            "span": {"item_id": "unknown-unit", "start": 0, "end": 1, "quote": "x"},
        }
    ]
    with pytest.raises((AnnotationError, ValueError)):
        import_annotation_responses(
            annotation_session, save(tmp_path / "bad-span.json", value)
        )
    assert annotation_status(annotation_session)["original_records"] == 0


def test_partial_and_abstention_are_retained_as_incomplete(
    annotation_session, tmp_path
):
    value, _ = answer_one(templates(annotation_session)[0], decision="abstain")
    value["answers"][0]["criteria"] = []
    value["answers"][0]["defect_inventory"] = "unavailable"
    import_annotation_responses(
        annotation_session, save(tmp_path / "abstain.json", value)
    )
    body = records(annotation_session, tmp_path)[0]["body"]
    assert body["decision"] == "abstain"
    assert body["coverage"]["complete"] is False
    assert body["coverage"]["missing_criteria"]


def test_new_original_opinion_and_adjudication_never_overwrite(
    annotation_session, tmp_path
):
    value, _ = answer_one(templates(annotation_session)[0])
    first_id = value["answers"][0]["response_id"]
    import_annotation_responses(
        annotation_session, save(tmp_path / "first.json", value)
    )
    second = copy.deepcopy(value)
    second["answers"][0].update(response_id="second-opinion", decision="unacceptable")
    import_annotation_responses(
        annotation_session, save(tmp_path / "second.json", second)
    )
    status = annotation_status(annotation_session)
    assert status["original_records"] == 2
    assert status["decision_disagreement_cases"] == 1
    adjudicated = copy.deepcopy(value)
    adjudicated["format"] = "draftbench-adjudication-responses-v1"
    adjudicated["answers"][0].update(
        response_id="adjudication-1", based_on=[first_id, "second-opinion"]
    )
    path = save(tmp_path / "adjudication.json", adjudicated)
    assert (
        import_annotation_responses(annotation_session, path, adjudication=True)[
            "inserted"
        ]
        == 1
    )
    after = records(annotation_session, tmp_path)
    assert [row["kind"] for row in after].count("original") == 2
    assert [row["kind"] for row in after].count("adjudication") == 1
    assert annotation_status(annotation_session)["unresolved_disagreement_cases"] == 0
    third = copy.deepcopy(second)
    third["answers"][0]["response_id"] = "third-opinion"
    import_annotation_responses(
        annotation_session, save(tmp_path / "third.json", third)
    )
    assert annotation_status(annotation_session)["unresolved_disagreement_cases"] == 1
    assert (
        import_annotation_responses(annotation_session, path, adjudication=True)[
            "duplicates"
        ]
        == 1
    )


def test_cli_aggregate_status_and_safe_errors(annotation_session, tmp_path, capsys):
    assert main(["review", "status", str(annotation_session)]) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["packet_count"] == 2
    for marker in (
        "PRIVATE",
        str(annotation_session),
        "scoring-fixture",
        "synthetic-reviewer",
    ):
        assert marker not in result.out
    bad = tmp_path / "PRIVATE-RESPONSE.json"
    bad.write_text('{"private":"PRIVATE-CONTENT"}')
    assert main(["review", "import", str(annotation_session), str(bad)]) == 2
    result = capsys.readouterr()
    assert (
        not result.out and "PRIVATE" not in result.err and "Traceback" not in result.err
    )
