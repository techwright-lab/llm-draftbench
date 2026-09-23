import copy

import pytest
from test_annotation import annotation_inputs as annotation_inputs
from test_annotation import annotation_session as annotation_session
from test_annotation import answer_one, records, save, templates

from draftbench.annotation import annotation_status, import_annotation_responses


@pytest.mark.parametrize("inventory", ["partial", "unavailable"])
def test_incomplete_defect_inventory_is_not_complete_annotation(
    annotation_session, tmp_path, inventory
):
    response, _ = answer_one(templates(annotation_session)[0])
    response["answers"][0]["defect_inventory"] = inventory
    import_annotation_responses(
        annotation_session, save(tmp_path / "partial.json", response)
    )
    result = records(annotation_session, tmp_path)[0]
    assert result["body"]["defect_inventory"] == inventory
    assert result["body"]["coverage"]["complete"] is False
    status = annotation_status(annotation_session)
    assert status["received_assignments"] == 1
    assert status["complete_assignments"] == 0
    assert status["incomplete_assignments"] == 1


def test_partial_inventory_adjudication_cannot_close_disagreement(
    annotation_session, tmp_path
):
    first, _ = answer_one(templates(annotation_session)[0])
    first_id = first["answers"][0]["response_id"]
    import_annotation_responses(
        annotation_session, save(tmp_path / "first.json", first)
    )
    second = copy.deepcopy(first)
    second["answers"][0].update(response_id="second-opinion", decision="unacceptable")
    import_annotation_responses(
        annotation_session, save(tmp_path / "second.json", second)
    )
    adjudication = copy.deepcopy(first)
    adjudication["format"] = "draftbench-adjudication-responses-v1"
    adjudication["answers"][0].update(
        response_id="partial-adjudication",
        based_on=[first_id, "second-opinion"],
        defect_inventory="partial",
    )
    import_annotation_responses(
        annotation_session,
        save(tmp_path / "adjudication.json", adjudication),
        adjudication=True,
    )
    status = annotation_status(annotation_session)
    assert status["adjudication_records"] == 1
    assert status["unresolved_disagreement_cases"] == 1
