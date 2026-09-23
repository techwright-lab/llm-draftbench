import copy
import math

import pytest
from conftest import seal
from scoring_cases import bound_case, producer, score_case_data

from draftbench.scoring.models import (
    ScoreCase,
    ScoringBundle,
    artifact_digest,
    cohort_key,
    rate,
    reference_scope,
)


def bundle_data(cases):
    return seal(
        {
            "schema_version": "1",
            "format": "draftbench-scoring-v1",
            "purpose": "synthetic_infrastructure",
            "rights": {
                "license": "CC0-1.0",
                "usage": "redistributable",
                "authorization": "Synthetic test inputs owned by the project.",
            },
            "cases": [case.model_dump(mode="json") for case in cases],
        }
    )


def test_scoring_artifact_and_annotations_are_bound():
    case = bound_case(score_case_data())
    assert (
        artifact_digest([unit.model_dump() for unit in case.artifact.units])
        == case.artifact.sha256
    )
    assert ScoringBundle.model_validate(bundle_data([case])).cases == [case]
    for field in ("reference", "review", "alignment"):
        data = case.model_dump(mode="json")
        data[field]["binding"]["artifact_sha256"] = "0" * 64
        with pytest.raises(ValueError):
            ScoreCase.model_validate(seal(data))
        data = case.model_dump(mode="json")
        data[field]["binding"]["rubric_version"] = "other-version"
        with pytest.raises(ValueError):
            ScoreCase.model_validate(seal(data))


@pytest.mark.parametrize("change", ["body", "order", "source", "case", "extra"])
def test_tampering_and_unknown_fields_are_rejected(change):
    data = bound_case(score_case_data()).model_dump(mode="json")
    if change == "body":
        data["artifact"]["units"][0]["content"] += " changed"
        seal(data)
    elif change == "order":
        data["artifact"]["units"].insert(
            0, {"unit_id": "title", "kind": "text", "content": "New first unit"}
        )
        seal(data)
    elif change == "source":
        data["sources"][0]["text"] += " changed"
        seal(data)
    elif change == "case":
        data["case_id"] = "changed-without-reseal"
    else:
        data["reference"]["gold"] = True
        seal(data)
    with pytest.raises(ValueError):
        ScoreCase.model_validate(data)


def test_empty_outputs_can_be_loaded_for_invalid_output_scoring():
    data = score_case_data("")
    assert bound_case(data).artifact.units[0].content == ""
    data["artifact"]["units"] = []
    assert bound_case(data).artifact.units == []


def test_duplicate_observations_cannot_inflate_counts_under_new_case_ids():
    original = bound_case(score_case_data())
    data = original.model_dump(mode="json")
    data["case_id"] = "renamed"
    data["source_family"] = "misleading-new-family"
    renamed = ScoreCase.model_validate(seal(data))
    with pytest.raises(ValueError):
        ScoringBundle.model_validate(bundle_data([original, renamed]))


def test_duplicate_and_unknown_rubric_responses_are_rejected():
    data = score_case_data()
    data["review"]["checks"] *= 2
    with pytest.raises(ValueError):
        bound_case(data)
    data = score_case_data()
    data["review"]["checks"][0]["check_id"] = "not-in-rubric"
    with pytest.raises(ValueError):
        bound_case(data)


def test_source_missingness_and_reference_attribution_are_explicit():
    data = score_case_data()
    data["sources_state"] = "unavailable"
    with pytest.raises(ValueError):
        bound_case(data)
    data = score_case_data()
    data["reference"]["producer"] = None
    data["reference"]["basis"] = None
    with pytest.raises(ValueError):
        bound_case(data)
    data["reference"].update(acceptability="unknown", defect_inventory="unavailable")
    assert reference_scope(bound_case(data).reference) == "unassessed"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("human", "human_reference"),
        ("model", "auxiliary_model_agreement"),
        ("workflow", "workflow_agreement"),
        ("synthetic", "synthetic_reference"),
    ],
)
def test_reference_scopes_do_not_silently_promote_model_judges_to_humans(
    kind, expected
):
    data = score_case_data()
    data["reference"].update(producer=producer(kind), basis="historical_exact")
    case = bound_case(data)
    assert reference_scope(case.reference) == expected
    if kind != "synthetic":
        data["reference"]["independence"] = "same_system"
        assert reference_scope(bound_case(data).reference) == "self_agreement"


def test_cohorts_keep_reviewer_and_reference_configuration_separate():
    original = bound_case(score_case_data())
    data = score_case_data()
    data["review"]["producer"]["version"]["value"] = "2"
    assert cohort_key(bound_case(data)) != cohort_key(original)
    data = score_case_data()
    data["reference"]["basis"] = "reconstructed"
    assert cohort_key(bound_case(data)) != cohort_key(original)


def test_rates_have_explicit_denominators_and_no_perfect_empty_default():
    assert rate(0, 0) == {
        "state": "undefined",
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert rate(0, 3)["value"] == 0
    assert math.isclose(rate(1, 3)["value"], 1 / 3)
    assert rate(1, 3, assessed=False)["value"] is None
    assert rate(1, 3, assessed=False)["state"] == "unassessed"
    for n, d in ((1, 0), (-1, 2), (True, 2), (0, False), (1.0, 2)):
        with pytest.raises(ValueError):
            rate(n, d)


def test_binding_identifies_changed_order_even_when_rejoined_text_matches():
    data = score_case_data("firstsecond")
    original = bound_case(copy.deepcopy(data))
    data["artifact"]["units"] = [
        {"unit_id": "part-1", "kind": "social_post", "content": "first"},
        {"unit_id": "part-2", "kind": "social_post", "content": "second"},
    ]
    assert bound_case(data).artifact.sha256 != original.artifact.sha256
