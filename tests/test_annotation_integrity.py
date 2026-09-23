import copy
import json
from datetime import datetime, timezone

import pytest
from test_annotation import annotation_inputs as annotation_inputs
from test_annotation import annotation_session as annotation_session
from test_annotation import answer_one, records, save, templates

from draftbench.annotation import (
    annotation_status,
    export_annotation_records,
    import_annotation_responses,
)
from draftbench.annotation_models import AnnotationError


def filled_answer(task, answer, *, decision="acceptable", record_id=None):
    answer = copy.deepcopy(answer)
    answer.update(
        status="answered",
        rated_at=datetime.now(timezone.utc).isoformat(),
        decision=decision,
        criteria=[
            {
                "check_id": criterion["criterion_id"],
                "state": "pass"
                if criterion["applicability"] != "inapplicable"
                else "inapplicable",
            }
            for criterion in task["criteria"]
        ],
        defect_inventory="complete",
        defects=[],
        notes="Synthetic returned label",
    )
    if record_id:
        answer["response_id"] = record_id
    return answer


def packet_for(template):
    return json.loads(
        template.with_name(
            template.name.replace(".responses.json", ".json")
        ).read_text()
    )


def test_different_raters_keep_individual_labels_on_same_original_artifact(
    annotation_session, tmp_path
):
    first, second = templates(annotation_session)
    one, task = answer_one(first)
    target_text = [unit["content"] for unit in task["artifact"]["units"]]
    two = json.loads(second.read_text())
    other_task = next(
        item
        for item in packet_for(second)["tasks"]
        if [unit["content"] for unit in item["artifact"]["units"]] == target_text
    )
    template_answer = next(
        answer
        for answer in two["answers"]
        if answer["task_id"] == other_task["task_id"]
    )
    two["answers"] = [
        filled_answer(other_task, template_answer, decision="unacceptable")
    ]
    assert one["rater_id"] != two["rater_id"]
    assert task["task_id"] != other_task["task_id"]
    import_annotation_responses(annotation_session, save(tmp_path / "one.json", one))
    import_annotation_responses(annotation_session, save(tmp_path / "two.json", two))
    imported = records(annotation_session, tmp_path)
    assert len(imported) == 2
    assert len({row["body"]["rater_key"] for row in imported}) == 2
    assert len({row["body"]["case_identity"] for row in imported}) == 1
    assert len({row["body"]["artifact_sha256"] for row in imported}) == 1
    assert annotation_status(annotation_session)["decision_disagreement_cases"] == 1


def test_alias_spans_round_trip_to_original_unit_ids(annotation_session, tmp_path):
    response, task = answer_one(
        templates(annotation_session)[0], decision="unacceptable"
    )
    unit = task["artifact"]["units"][0]
    quote = unit["content"][:10]
    response["answers"][0]["defects"] = [
        {
            "finding_id": "defect-one",
            "kind": "defect",
            "category": "factual",
            "severity": "major",
            "description": "Synthetic localized defect",
            "span": {
                "item_id": unit["unit_id"],
                "start": 0,
                "end": len(quote),
                "quote": quote,
            },
        }
    ]
    import_annotation_responses(
        annotation_session, save(tmp_path / "localized.json", response)
    )
    body = records(annotation_session, tmp_path)[0]["body"]
    assert body["defects"][0]["span"] == {
        "item_id": "body",
        "start": 0,
        "end": len(quote),
        "quote": quote,
    }
    assert body["original_response"]["defects"][0]["span"]["item_id"] == unit["unit_id"]
    assert body["rubric_id"] == "synthetic-reference-rubric"
    assert body["rubric_version"] == "1"
    assert body["unit_order"] == ["body"]


def test_conflicting_late_row_rolls_back_entire_import(annotation_session, tmp_path):
    template = templates(annotation_session)[0]
    response, _ = answer_one(template)
    import_annotation_responses(
        annotation_session, save(tmp_path / "initial.json", response)
    )
    all_answers = json.loads(template.read_text())
    packet = packet_for(template)
    next_task = packet["tasks"][1]
    next_answer = next(
        answer
        for answer in all_answers["answers"]
        if answer["task_id"] == next_task["task_id"]
    )
    conflicting = copy.deepcopy(response["answers"][0])
    conflicting["decision"] = "unacceptable"
    response["answers"] = [filled_answer(next_task, next_answer), conflicting]
    with pytest.raises(ValueError):
        import_annotation_responses(
            annotation_session, save(tmp_path / "conflict.json", response)
        )
    assert annotation_status(annotation_session)["original_records"] == 1


def test_adjudication_cannot_omit_an_existing_opinion(annotation_session, tmp_path):
    response, _ = answer_one(templates(annotation_session)[0])
    first_id = response["answers"][0]["response_id"]
    import_annotation_responses(
        annotation_session, save(tmp_path / "first.json", response)
    )
    second = copy.deepcopy(response)
    second["answers"][0].update(
        response_id="different-original", decision="unacceptable"
    )
    import_annotation_responses(
        annotation_session, save(tmp_path / "second.json", second)
    )
    response["format"] = "draftbench-adjudication-responses-v1"
    response["answers"][0].update(
        response_id="incomplete-adjudication", based_on=[first_id]
    )
    with pytest.raises(AnnotationError):
        import_annotation_responses(
            annotation_session,
            save(tmp_path / "adjudication.json", response),
            adjudication=True,
        )
    assert annotation_status(annotation_session)["adjudication_records"] == 0


def test_private_record_export_cannot_enter_blind_packet_directory(annotation_session):
    with pytest.raises(AnnotationError):
        export_annotation_records(
            annotation_session, annotation_session / "packets" / "private-records.json"
        )
    assert not (annotation_session / "packets" / "private-records.json").exists()


def test_private_custody_map_identifies_packets_without_entering_blind_files(
    annotation_session,
):
    custody = json.loads((annotation_session / "custody.json").read_text())
    assert {assignment["rater_key"] for assignment in custody["assignments"]} == {
        "private-editor-a",
        "private-editor-b",
    }
    for assignment in custody["assignments"]:
        assert (annotation_session / assignment["packet_path"]).is_file()
        assert (annotation_session / assignment["template_path"]).is_file()
    assert not (annotation_session / "packets" / "custody.json").exists()
    custody_path = annotation_session / "custody.json"
    custody_path.write_bytes(custody_path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        annotation_status(annotation_session)


def test_tampered_packet_cannot_be_used_with_old_response_bindings(
    annotation_session, tmp_path
):
    template = templates(annotation_session)[0]
    response, _ = answer_one(template)
    packet_path = template.with_name(template.name.replace(".responses.json", ".json"))
    packet_path.write_bytes(packet_path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        import_annotation_responses(
            annotation_session, save(tmp_path / "reply.json", response)
        )
