"""Synthetic reviewer decisions: validity, label custody, and honest denominators."""

import copy
import json

import pytest
from conftest import seal
from pydantic import ValidationError
from scoring_cases import bound_case, producer, score_case_data

from draftbench.scoring.decisions import assess_decision, score_decisions
from draftbench.scoring.models import ScoreCase


def decision_case(decision="pass", gold="acceptable", *, case_id="case-1"):
    data = score_case_data(
        f"Synthetic source-grounded draft. {case_id}", case_id=case_id
    )
    data["review"]["decision"] = decision
    data["reference"]["acceptability"] = gold
    return bound_case(data)


def finding(*, kind="defect", severity="major", span=None):
    return {
        "finding_id": "finding-1",
        "kind": kind,
        "category": "factual",
        "severity": severity,
        "description": "Synthetic annotation; not a measured model result.",
        "span": span,
    }


def assert_rate(metric, numerator, denominator, *, assessed=True):
    state = (
        "unassessed" if not assessed else "undefined" if not denominator else "known"
    )
    value = pytest.approx(numerator / denominator) if state == "known" else None
    assert metric == {
        "state": state,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
    }


@pytest.mark.parametrize(
    ("raw", "effective"),
    [
        ("pass", "pass"),
        ("edit", "edit"),
        ("block", "block"),
        ("abstain", "abstain"),
        ("error", "measurement_error"),
    ],
)
def test_preserves_each_valid_disposition(raw, effective):
    assert assess_decision(decision_case(raw)) == effective


def test_missing_measurement_is_not_a_pass_or_abstention():
    data = score_case_data()
    data["review"] = None
    data["alignment"] = None
    case = bound_case(data)
    assert assess_decision(case) == "measurement_error"
    result = score_decisions([case])
    assert result["raw_decisions"] == {
        "pass": 0,
        "edit": 0,
        "block": 0,
        "abstain": 0,
        "error": 0,
        "missing": 1,
    }
    assert result["counts"]["invalid_passes"] == 0
    assert_rate(result["metrics"]["decision_coverage"], 0, 1)
    assert_rate(result["metrics"]["measurement_error_rate"], 1, 1)


@pytest.mark.parametrize("decision", ["pass", "edit", "block", "abstain", "error"])
@pytest.mark.parametrize("text", ["", " \n\t"])
def test_empty_artifact_is_always_a_measurement_error(decision, text):
    data = score_case_data(text)
    data["review"]["decision"] = decision
    case = bound_case(data)
    assert assess_decision(case) == "measurement_error"
    assert score_decisions([case])["counts"]["invalid_passes"] == int(
        decision == "pass"
    )


def test_artifact_without_units_is_a_measurement_error():
    data = score_case_data()
    data["artifact"]["units"] = []
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
@pytest.mark.parametrize("state", ["missing", "unknown", "invalid", "inapplicable"])
def test_required_measurement_must_be_present_and_conclusive(decision, state):
    data = score_case_data()
    data["review"]["decision"] = decision
    if state == "missing":
        data["review"]["checks"] = []
    else:
        data["review"]["checks"][0]["state"] = state
    case = bound_case(data)
    assert assess_decision(case) == "measurement_error"
    assert score_decisions([case])["counts"]["invalid_passes"] == int(
        decision == "pass"
    )


@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
def test_partial_rubric_cannot_be_decisive(decision):
    data = score_case_data()
    data["review"]["decision"] = decision
    data["rubric"]["checks"].append(
        {
            "kind": "required_literal",
            "check_id": "second-required",
            "applicability": "required",
            "unit_id": "body",
            "literal": "Synthetic",
        }
    )
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize("applicability", [None, "optional", "inapplicable"])
def test_no_required_checks_is_not_a_vacuous_pass(applicability):
    data = score_case_data()
    if applicability is None:
        data["rubric"]["checks"] = []
        data["review"]["checks"] = []
    else:
        data["rubric"]["checks"][0]["applicability"] = applicability
    case = bound_case(data)
    assert assess_decision(case) == "measurement_error"
    assert score_decisions([case])["counts"]["invalid_passes"] == 1


@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
def test_failed_required_check_invalidates_only_a_claimed_pass(decision):
    data = score_case_data()
    data["review"]["decision"] = decision
    data["review"]["checks"][0]["state"] = "fail"
    case = bound_case(data)
    assert assess_decision(case) == (
        "measurement_error" if decision == "pass" else decision
    )
    assert score_decisions([case])["counts"]["invalid_passes"] == int(
        decision == "pass"
    )


@pytest.mark.parametrize("severity", ["critical", "major"])
@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
def test_material_defects_are_inconsistent_with_pass(severity, decision):
    data = score_case_data()
    data["review"]["decision"] = decision
    data["review"]["findings"] = [finding(severity=severity)]
    case = bound_case(data)
    assert assess_decision(case) == (
        "measurement_error" if decision == "pass" else decision
    )
    assert score_decisions([case])["counts"]["invalid_passes"] == int(
        decision == "pass"
    )


@pytest.mark.parametrize(
    ("kind", "severity"),
    [("suggestion", "suggestion"), ("defect", "minor")],
)
def test_nonmaterial_findings_do_not_override_a_valid_pass(kind, severity):
    data = score_case_data()
    data["review"]["findings"] = [finding(kind=kind, severity=severity)]
    assert assess_decision(bound_case(data)) == "pass"


@pytest.mark.parametrize("state", [None, "pass", "fail", "unknown", "inapplicable"])
def test_optional_style_check_does_not_change_mandatory_pass(state):
    data = score_case_data()
    data["rubric"]["checks"].append(
        {
            "kind": "required_literal",
            "check_id": "optional-style",
            "applicability": "optional",
            "unit_id": "body",
            "literal": "Please",
        }
    )
    if state is not None:
        data["review"]["checks"].append({"check_id": "optional-style", "state": state})
    data["review"]["findings"] = [finding(kind="suggestion", severity="suggestion")]
    assert assess_decision(bound_case(data)) == "pass"


@pytest.mark.parametrize("decision", ["pass", "edit", "block", "abstain"])
def test_explicitly_invalid_optional_annotation_is_a_measurement_error(decision):
    data = score_case_data()
    data["review"]["decision"] = decision
    data["rubric"]["checks"].append(
        {
            "kind": "nonempty",
            "check_id": "optional-check",
            "applicability": "optional",
        }
    )
    data["review"]["checks"].append({"check_id": "optional-check", "state": "invalid"})
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize("assertion_mode", ["hypothetical", "fictional"])
@pytest.mark.parametrize("state", [None, "inapplicable"])
def test_authorized_nonfactual_fact_is_not_required(assertion_mode, state):
    data = score_case_data()
    data["rubric"]["checks"].append(
        {
            "kind": "fact",
            "check_id": "nonfactual",
            "applicability": "required",
            "span": {"item_id": "body", "start": 0, "end": 9, "quote": "Synthetic"},
            "observed_value": "Synthetic",
            "observed_unit": None,
            "source_id": None,
            "reference_span": None,
            "expected_value": None,
            "expected_unit": None,
            "relevance": "unknown",
            "assertion_mode": assertion_mode,
            "authorized_nonfactual": True,
        }
    )
    if state is not None:
        data["review"]["checks"].append({"check_id": "nonfactual", "state": state})
    assert assess_decision(bound_case(data)) == "pass"
    data["rubric"]["checks"][-1]["authorized_nonfactual"] = False
    assert assess_decision(bound_case(data)) == "measurement_error"
    data["rubric"]["checks"][-1]["authorized_nonfactual"] = True
    data["rubric"]["checks"][-1]["assertion_mode"] = "asserted"
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize(
    "checks", [[], [{"check_id": "body-present", "state": "unknown"}]]
)
def test_explicit_abstention_need_not_claim_complete_checks(checks):
    data = score_case_data()
    data["review"]["decision"] = "abstain"
    data["review"]["checks"] = checks
    assert assess_decision(bound_case(data)) == "abstain"


def test_invalid_required_annotation_takes_precedence_over_abstention():
    data = score_case_data()
    data["review"]["decision"] = "abstain"
    data["review"]["checks"][0]["state"] = "invalid"
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize("decision", ["pass", "edit", "block", "abstain"])
@pytest.mark.parametrize(
    "span",
    [
        {"item_id": "other-artifact", "start": 0, "end": 9, "quote": "Synthetic"},
        {"item_id": "body", "start": 0, "end": 9, "quote": "synthetic"},
        {"item_id": "body", "start": 1, "end": 10, "quote": "Synthetic"},
        {"item_id": "body", "start": 0, "end": 100, "quote": "Synthetic"},
    ],
)
def test_finding_spans_must_bind_to_actual_artifact(decision, span):
    data = score_case_data()
    data["review"]["decision"] = decision
    data["review"]["findings"] = [
        finding(kind="suggestion", severity="suggestion", span=span)
    ]
    assert assess_decision(bound_case(data)) == "measurement_error"


@pytest.mark.parametrize(
    "span",
    [
        None,
        {"item_id": "body", "start": 0, "end": 9, "quote": "Synthetic"},
    ],
)
def test_valid_local_suggestions_and_unspanned_global_omissions_are_allowed(span):
    data = score_case_data()
    data["review"]["findings"] = [
        finding(kind="suggestion", severity="suggestion", span=span)
    ]
    assert assess_decision(bound_case(data)) == "pass"
    data["review"]["decision"] = "edit"
    data["review"]["findings"] = [finding(span=span)]
    assert assess_decision(bound_case(data)) == "edit"


def test_denominators_keep_abstention_errors_unknown_accepts_and_families_visible():
    rows = [
        ("pass", "acceptable"),
        ("pass", "unacceptable"),
        ("pass", "unknown"),
        ("edit", "acceptable"),
        ("block", "acceptable"),
        ("block", "unacceptable"),
        ("abstain", "acceptable"),
        ("abstain", "unacceptable"),
        ("error", "acceptable"),
        ("error", "unacceptable"),
        ("pass", "unacceptable"),
        ("block", "unknown"),
    ]
    cases = []
    for index, (decision, gold) in enumerate(rows):
        data = score_case_data(
            f"Synthetic draft number {index}.", case_id=f"case-{index}"
        )
        data["review"]["decision"] = decision
        data["reference"]["acceptability"] = gold
        data["source_family"] = "shared-family"
        if index == 10:
            data["review"]["checks"][0]["state"] = "fail"
        cases.append(bound_case(data))
    before = [case.model_dump() for case in cases]
    result = score_decisions(cases)
    assert result["metric_version"] == "decisions-v1"
    assert result["reference_scope"] == "synthetic_reference"
    assert result["counts"] == {
        "case_count": 12,
        "source_family_count": 1,
        "gold_acceptable": 5,
        "gold_unacceptable": 5,
        "gold_unknown": 2,
        "passed_known": 2,
        "passed_unknown": 1,
        "invalid_passes": 1,
    }
    assert result["decisions"] == {
        "pass": 3,
        "edit": 1,
        "block": 3,
        "abstain": 2,
        "measurement_error": 3,
    }
    assert result["raw_decisions"] == {
        "pass": 4,
        "edit": 1,
        "block": 3,
        "abstain": 2,
        "error": 2,
        "missing": 0,
    }
    metrics = result["metrics"]
    assert_rate(metrics["false_pass_rate"], 1, 5)
    assert_rate(metrics["false_block_rate"], 2, 5)
    assert_rate(metrics["accepted_set_risk"], 1, 2)
    assert_rate(metrics["decision_coverage"], 7, 12)
    assert_rate(metrics["abstention_rate"], 2, 12)
    assert_rate(metrics["measurement_error_rate"], 3, 12)
    assert sum(result["decisions"].values()) == result["counts"]["case_count"]
    assert sum(result["raw_decisions"].values()) == result["counts"]["case_count"]
    assert [case.model_dump() for case in cases] == before
    assert score_decisions(list(reversed(cases))) == result
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert set(result) == {
        "metric_version",
        "reference_scope",
        "counts",
        "decisions",
        "raw_decisions",
        "metrics",
    }
    assert set(metrics) == {
        "false_pass_rate",
        "false_block_rate",
        "accepted_set_risk",
        "decision_coverage",
        "abstention_rate",
        "measurement_error_rate",
    }


def test_all_abstain_has_zero_coverage_not_a_perfect_accepted_set():
    cases = [
        decision_case("abstain", "acceptable", case_id="good"),
        decision_case("abstain", "unacceptable", case_id="bad"),
    ]
    result = score_decisions(cases)
    assert_rate(result["metrics"]["false_pass_rate"], 0, 1)
    assert_rate(result["metrics"]["false_block_rate"], 0, 1)
    assert_rate(result["metrics"]["accepted_set_risk"], 0, 0)
    assert_rate(result["metrics"]["decision_coverage"], 0, 2)
    assert_rate(result["metrics"]["abstention_rate"], 2, 2)
    assert_rate(result["metrics"]["measurement_error_rate"], 0, 2)


def test_always_reject_penalizes_false_blocks_and_has_no_accepted_set():
    result = score_decisions(
        [
            decision_case("edit", "acceptable", case_id="good-edit"),
            decision_case("block", "acceptable", case_id="good-block"),
            decision_case("block", "unacceptable", case_id="bad"),
        ]
    )
    assert_rate(result["metrics"]["false_pass_rate"], 0, 1)
    assert_rate(result["metrics"]["false_block_rate"], 2, 2)
    assert_rate(result["metrics"]["accepted_set_risk"], 0, 0)
    assert_rate(result["metrics"]["decision_coverage"], 3, 3)
    assert result["counts"]["passed_known"] == result["counts"]["passed_unknown"] == 0


def test_all_errors_remain_in_class_denominators_and_have_zero_coverage():
    result = score_decisions(
        [
            decision_case("error", "acceptable", case_id="good"),
            decision_case("error", "unacceptable", case_id="bad"),
        ]
    )
    assert_rate(result["metrics"]["false_pass_rate"], 0, 1)
    assert_rate(result["metrics"]["false_block_rate"], 0, 1)
    assert_rate(result["metrics"]["accepted_set_risk"], 0, 0)
    assert_rate(result["metrics"]["decision_coverage"], 0, 2)
    assert_rate(result["metrics"]["measurement_error_rate"], 2, 2)


def test_all_unknown_gold_keeps_unknown_passes_out_of_accepted_set_denominator():
    result = score_decisions(
        [
            decision_case("pass", "unknown", case_id="unknown-pass"),
            decision_case("block", "unknown", case_id="unknown-block"),
        ]
    )
    assert result["counts"]["gold_unknown"] == 2
    assert result["counts"]["passed_unknown"] == 1
    assert result["counts"]["passed_known"] == 0
    for name in ("false_pass_rate", "false_block_rate", "accepted_set_risk"):
        assert_rate(result["metrics"][name], 0, 0)
    assert_rate(result["metrics"]["decision_coverage"], 2, 2)


def test_empty_cohort_has_explicit_zero_counts_and_undefined_rates():
    result = score_decisions([])
    assert result["metric_version"] == "decisions-v1"
    assert result["reference_scope"] == "unassessed"
    assert all(count == 0 for count in result["counts"].values())
    assert all(count == 0 for count in result["decisions"].values())
    assert all(count == 0 for count in result["raw_decisions"].values())
    for metric in result["metrics"].values():
        assert_rate(metric, 0, 0)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("kind", "basis", "independence", "scope"),
    [
        ("human", "historical_exact", "independent", "human_reference"),
        ("model", "historical_exact", "independent", "auxiliary_model_agreement"),
        ("workflow", "reconstructed", "independent", "workflow_agreement"),
        ("model", "historical_exact", "same_system", "self_agreement"),
        ("human", "synthetic", "independent", "synthetic_reference"),
        ("synthetic", "historical_exact", "independent", "synthetic_reference"),
    ],
)
def test_reference_provenance_never_promotes_automated_labels_to_human_gold(
    kind, basis, independence, scope
):
    data = score_case_data()
    data["reference"].update(
        {
            "producer": producer(kind=kind),
            "basis": basis,
            "independence": independence,
            "acceptability": "unacceptable",
        }
    )
    result = score_decisions([bound_case(data)])
    assert result["reference_scope"] == scope
    assert_rate(result["metrics"]["false_pass_rate"], 1, 1)
    assert_rate(result["metrics"]["accepted_set_risk"], 1, 1)


@pytest.mark.parametrize("label", ["acceptable", "unacceptable", "unknown"])
def test_unassessed_reference_does_not_turn_supplied_labels_into_eligible_gold(label):
    data = score_case_data()
    data["reference"].update({"acceptability": label, "independence": "unknown"})
    result = score_decisions([bound_case(data)])
    assert result["reference_scope"] == "unassessed"
    assert (
        result["counts"]["gold_acceptable"]
        == result["counts"]["gold_unacceptable"]
        == 0
    )
    assert result["counts"]["gold_unknown"] == result["counts"]["passed_unknown"] == 1
    assert result["counts"]["passed_known"] == 0
    for name in ("false_pass_rate", "false_block_rate", "accepted_set_risk"):
        assert_rate(result["metrics"][name], 0, 0, assessed=False)
    assert_rate(result["metrics"]["decision_coverage"], 1, 1)


def test_absent_reference_producer_is_unassessed_not_synthetic_gold():
    data = score_case_data()
    data["reference"].update(
        {
            "producer": None,
            "basis": None,
            "acceptability": "unknown",
            "independence": "unknown",
            "defect_inventory": "unavailable",
        }
    )
    result = score_decisions([bound_case(data)])
    assert result["reference_scope"] == "unassessed"
    assert_rate(result["metrics"]["accepted_set_risk"], 0, 0, assessed=False)


def test_duplicate_case_ids_are_rejected_even_if_observations_differ():
    cases = [decision_case(), decision_case("block")]
    with pytest.raises(ValueError, match="^duplicate_scoring_id$"):
        score_decisions(cases)


@pytest.mark.parametrize(
    "path",
    [
        "rubric.rubric_id",
        "rubric.version",
        "review.producer.name.value",
        "review.producer.version.value",
        "reference.producer.name.value",
        "reference.producer.version.value",
        "reference.basis",
        "reference.independence",
    ],
)
def test_mixed_scoring_cohorts_are_rejected_with_safe_fixed_error(path):
    first = score_case_data(case_id="first")
    second = score_case_data(case_id="second")
    target = second
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    replacement = {"basis": "reconstructed", "independence": "unknown"}.get(
        keys[-1], "private-mismatch-marker"
    )
    target[keys[-1]] = replacement
    with pytest.raises(ValueError, match="^mixed_scoring_cohort$"):
        score_decisions([bound_case(first), bound_case(second)])


@pytest.mark.parametrize("field", ["requested_model", "served_model"])
def test_exact_reviewer_model_configuration_is_a_cohort_boundary(field):
    first = score_case_data(case_id="first")
    first["review"]["producer"] = producer(kind="model")
    second = copy.deepcopy(first)
    second["case_id"] = "second"
    second["review"]["producer"][field] = {"state": "known", "value": "other-config"}
    with pytest.raises(ValueError, match="^mixed_scoring_cohort$"):
        score_decisions([bound_case(first), bound_case(second)])


def test_missing_reviewer_identity_cannot_be_pooled_with_identified_reviewer():
    first = score_case_data(case_id="first")
    second = score_case_data(case_id="second")
    second["review"] = second["alignment"] = None
    with pytest.raises(ValueError, match="^mixed_scoring_cohort$"):
        score_decisions([bound_case(first), bound_case(second)])


@pytest.mark.parametrize("name", ["review", "reference", "alignment"])
def test_schema_rejects_binding_to_another_artifact_before_scoring(name):
    data = score_case_data()
    data[name]["binding"]["artifact_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="scoring_binding_mismatch"):
        ScoreCase.model_validate(seal(data))


def test_schema_rejects_tampered_evidence_source_before_scoring():
    data = score_case_data()
    data["sources"][0]["text"] = "A different source, without updating its digest."
    with pytest.raises(ValidationError, match="scoring_source_mismatch"):
        bound_case(data)


@pytest.mark.parametrize("duplicate", ["check", "finding"])
def test_schema_rejects_duplicate_review_annotations(duplicate):
    data = score_case_data()
    if duplicate == "check":
        data["review"]["checks"].append(dict(data["review"]["checks"][0]))
    else:
        data["review"]["findings"] = [finding(), finding()]
    with pytest.raises(ValidationError, match="duplicate_scoring_id"):
        bound_case(data)


def test_schema_rejects_unknown_check_ids_instead_of_treating_them_as_coverage():
    data = score_case_data()
    data["review"]["checks"].append({"check_id": "not-in-rubric", "state": "pass"})
    with pytest.raises(ValidationError, match="unknown_review_check"):
        bound_case(data)


@pytest.mark.parametrize("entrypoint", ["assess", "score"])
def test_mutated_nested_measurements_are_revalidated_at_public_entrypoints(entrypoint):
    case = decision_case()
    assert case.review is not None
    case.review.checks.clear()
    with pytest.raises(ValidationError, match="identity_mismatch"):
        if entrypoint == "assess":
            assess_decision(case)
        else:
            score_decisions([case])


@pytest.mark.parametrize("entrypoint", ["assess", "score"])
def test_unvalidated_input_is_rejected_at_public_entrypoints(entrypoint):
    with pytest.raises(ValueError, match="^invalid_scoring_case$"):
        if entrypoint == "assess":
            assess_decision(score_case_data())
        else:
            score_decisions([score_case_data()])


@pytest.mark.parametrize("change", ["none", "label", "decision", "family", "findings"])
def test_same_observation_cannot_be_inflated_with_new_ids_or_conflicting_annotations(
    change,
):
    first = score_case_data(case_id="first")
    second = copy.deepcopy(first)
    second["case_id"] = "second"
    if change == "label":
        second["reference"]["acceptability"] = "unacceptable"
    elif change == "decision":
        second["review"]["decision"] = "block"
    elif change == "family":
        second["source_family"] = "another-family"
    elif change == "findings":
        second["review"]["findings"] = [finding()]
    with pytest.raises(ValueError, match="^duplicate_scoring_id$"):
        score_decisions([bound_case(first), bound_case(second)])


def test_instance_specific_rubric_parameters_do_not_split_the_versioned_cohort():
    first = score_case_data(case_id="first")
    second = score_case_data(case_id="second")
    second["rubric"]["checks"] = [
        {
            "kind": "required_literal",
            "check_id": "body-present",
            "applicability": "required",
            "unit_id": "body",
            "literal": "Synthetic",
        }
    ]
    result = score_decisions([bound_case(first), bound_case(second)])
    assert (
        result["counts"]["case_count"] == result["counts"]["source_family_count"] == 2
    )
    assert_rate(result["metrics"]["decision_coverage"], 2, 2)
