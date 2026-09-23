"""Synthetic adjudications test bookkeeping, not reviewer semantic correctness."""

from copy import deepcopy

import pytest
from conftest import seal
from pydantic import ValidationError
from scoring_cases import bound_case, producer, score_case_data

from draftbench.scoring.findings import score_findings
from draftbench.scoring.models import ScoreCase


def finding(finding_id, *, category="factual", severity="critical", span=None):
    return {
        "finding_id": finding_id,
        "kind": "suggestion" if severity == "suggestion" else "defect",
        "category": category,
        "severity": severity,
        "description": "Synthetic annotation, not an observed model result.",
        "span": span,
    }


def judgment(finding_id, state="matched", defect_ids=("d1",)):
    return {
        "finding_id": finding_id,
        "state": state,
        "defect_ids": list(defect_ids),
    }


def annotated_case():
    data = score_case_data()
    data["reference"]["acceptability"] = "unacceptable"
    data["reference"]["defects"] = [finding("d1")]
    data["review"]["decision"] = "edit"
    data["review"]["findings"] = [finding("f1")]
    data["alignment"]["judgments"] = [judgment("f1")]
    return data


def assert_rate(result, metric, state, numerator, denominator, value):
    assert result["metrics"][metric] == {
        "state": state,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
    }


def assert_unassessed(result):
    for metric in result["metrics"].values():
        assert metric["state"] == "unassessed"
        assert metric["value"] is None
    assert result["counts"]["critical_missed"] is None


def test_one_to_one_matches_have_explicit_version_scope_counts_and_rates():
    result = score_findings(bound_case(annotated_case()))
    assert set(result) == {
        "metric_version",
        "reference_scope",
        "alignment_scope",
        "review_coverage",
        "measurement_valid",
        "counts",
        "metrics",
        "pending_finding_ids",
        "ambiguous_finding_ids",
        "unmatched_defect_ids",
    }
    assert result["metric_version"] == "findings-v1"
    assert result["reference_scope"] == "synthetic_reference"
    assert result["alignment_scope"] == "synthetic_reference"
    assert result["measurement_valid"] is True
    assert result["counts"] == {
        "predictions": 1,
        "suggestions": 0,
        "reference_defects": 1,
        "matched": 1,
        "spurious": 0,
        "unassessed": 0,
        "ambiguous": 0,
        "unmatched_defects": 0,
        "critical_defects": 1,
        "critical_matched": 1,
        "critical_missed": 0,
    }
    for metric in ("precision", "recall", "critical_recall"):
        assert_rate(result, metric, "known", 1, 1, 1.0)
    assert result["pending_finding_ids"] == []
    assert result["ambiguous_finding_ids"] == []
    assert result["unmatched_defect_ids"] == []


@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
def test_find_nothing_does_not_pass_a_defective_reference(decision):
    data = annotated_case()
    data["review"].update(decision=decision, findings=[])
    data["alignment"]["judgments"] = []
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "undefined", 0, 0, None)
    assert_rate(result, "recall", "known", 0, 1, 0.0)
    assert_rate(result, "critical_recall", "known", 0, 1, 0.0)
    assert result["counts"]["critical_missed"] == 1
    assert result["unmatched_defect_ids"] == ["d1"]


def test_complete_empty_gold_and_empty_predictions_are_undefined_not_perfect():
    result = score_findings(bound_case(score_case_data()))
    for metric in ("precision", "recall", "critical_recall"):
        assert_rate(result, metric, "undefined", 0, 0, None)
    assert result["counts"]["critical_missed"] == 0


@pytest.mark.parametrize("has_defect", [False, True])
def test_explicit_all_spurious_predictions_have_zero_precision(has_defect):
    data = annotated_case()
    if not has_defect:
        data["reference"].update(acceptability="acceptable", defects=[])
    data["review"]["findings"] = [finding("f1"), finding("f2")]
    data["alignment"]["judgments"] = [
        judgment("f1", "spurious", ()),
        judgment("f2", "spurious", ()),
    ]
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "known", 0, 2, 0.0)
    assert result["counts"]["spurious"] == 2
    assert result["counts"]["matched"] == 0
    assert result["counts"]["critical_missed"] == int(has_defect)
    if has_defect:
        assert_rate(result, "recall", "known", 0, 1, 0.0)
    else:
        assert_rate(result, "recall", "undefined", 0, 0, None)


@pytest.mark.parametrize("missing", ["alignment", "judgment", "explicit_unassessed"])
def test_no_inferred_matches_from_equal_category_description_or_quote(missing):
    data = annotated_case()
    span = {"item_id": "body", "start": 0, "end": 9, "quote": "Synthetic"}
    data["review"]["findings"][0]["span"] = dict(span)
    data["reference"]["defects"][0]["span"] = dict(span)
    if missing == "alignment":
        data["alignment"] = None
    elif missing == "judgment":
        data["alignment"]["judgments"] = []
    else:
        data["alignment"]["judgments"][0]["state"] = "unassessed"
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["matched"] == 0
    assert result["counts"]["spurious"] == 0
    assert result["counts"]["unassessed"] == 1
    assert result["pending_finding_ids"] == ["f1"]
    assert result["unmatched_defect_ids"] == ["d1"]


def test_partial_adjudication_uses_all_predictions_and_withholds_rates():
    data = annotated_case()
    data["review"]["findings"].append(finding("f2"))
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert_rate(result, "precision", "unassessed", 1, 2, None)
    assert_rate(result, "recall", "unassessed", 1, 1, None)
    assert result["counts"]["matched"] == 1
    assert result["counts"]["unassessed"] == 1
    assert result["pending_finding_ids"] == ["f2"]
    assert result["unmatched_defect_ids"] == []


def test_ambiguous_candidates_remain_pending_and_are_not_false_positives():
    data = annotated_case()
    data["reference"]["defects"].append(finding("d2", severity="major"))
    data["alignment"]["judgments"] = [judgment("f1", "ambiguous", ("d1", "d2"))]
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["ambiguous"] == 1
    assert result["counts"]["unassessed"] == 0
    assert result["counts"]["spurious"] == 0
    assert result["pending_finding_ids"] == ["f1"]
    assert result["ambiguous_finding_ids"] == ["f1"]
    assert result["unmatched_defect_ids"] == ["d1", "d2"]


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_semantic_matches_credit_neither_finding(reverse):
    data = annotated_case()
    data["review"]["findings"].append(finding("f2"))
    data["alignment"]["judgments"].append(judgment("f2"))
    if reverse:
        data["review"]["findings"].reverse()
        data["alignment"]["judgments"].reverse()
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["matched"] == 0
    assert result["counts"]["critical_matched"] == 0
    assert result["counts"]["ambiguous"] == 2
    assert result["counts"]["predictions"] == 2
    assert result["pending_finding_ids"] == ["f1", "f2"]
    assert result["ambiguous_finding_ids"] == ["f1", "f2"]
    assert result["unmatched_defect_ids"] == ["d1"]


def test_explicit_resolution_after_collision_gives_one_credit_only():
    data = annotated_case()
    data["review"]["findings"].append(finding("f2"))
    data["alignment"]["judgments"].append(judgment("f2", "spurious", ()))
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "known", 1, 2, 0.5)
    assert_rate(result, "recall", "known", 1, 1, 1.0)
    assert result["counts"]["matched"] == 1
    assert result["counts"]["spurious"] == 1
    assert result["pending_finding_ids"] == []


def test_multiple_distinct_defects_use_reference_not_predicted_severity():
    data = annotated_case()
    data["reference"]["defects"].extend(
        [
            finding("d2", severity="major"),
            finding("d3"),
        ]
    )
    data["review"]["findings"][0]["severity"] = "minor"
    data["review"]["findings"].append(finding("f2"))
    data["alignment"]["judgments"].append(judgment("f2", defect_ids=("d2",)))
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "known", 2, 2, 1.0)
    assert_rate(result, "recall", "known", 2, 3, 2 / 3)
    assert_rate(result, "critical_recall", "known", 1, 2, 0.5)
    assert result["counts"]["critical_missed"] == 1
    assert result["unmatched_defect_ids"] == ["d3"]


def test_partial_inventory_withholds_recall_but_can_assess_precision():
    data = annotated_case()
    data["reference"]["defect_inventory"] = "partial"
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "known", 1, 1, 1.0)
    assert_rate(result, "recall", "unassessed", 1, 1, None)
    assert_rate(result, "critical_recall", "unassessed", 1, 1, None)
    assert result["counts"]["critical_missed"] is None


@pytest.mark.parametrize(
    "missing", ["unavailable", "unattributed", "review", "alignment"]
)
def test_missing_inputs_are_not_an_empty_correct_reference(missing):
    data = score_case_data()
    if missing in ("unavailable", "unattributed"):
        data["reference"].update(
            acceptability="unknown", defect_inventory="unavailable"
        )
        if missing == "unattributed":
            data["reference"].update(producer=None, basis=None, independence="unknown")
    else:
        data[missing] = None
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["predictions"] == 0
    assert result["counts"]["reference_defects"] == 0
    if missing == "unattributed":
        assert result["reference_scope"] == "unassessed"
    if missing == "alignment":
        assert result["alignment_scope"] == "unassessed"


def test_missing_review_keeps_unmatched_reference_without_known_critical_miss():
    data = annotated_case()
    data["review"] = None
    data["alignment"]["judgments"] = []
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["reference_defects"] == 1
    assert result["unmatched_defect_ids"] == ["d1"]


@pytest.mark.parametrize("side", ["reference", "alignment"])
def test_unknown_independence_never_produces_assessed_metrics(side):
    data = annotated_case()
    data[side]["independence"] = "unknown"
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result[f"{side}_scope"] == "unassessed"
    assert result["counts"]["predictions"] == 1
    assert result["counts"]["reference_defects"] == 1
    if side == "alignment":
        assert result["counts"]["matched"] == 0
        assert result["pending_finding_ids"] == ["f1"]


@pytest.mark.parametrize("state", ["matched", "spurious"])
def test_unknown_alignment_authority_cannot_assess_a_finding(state):
    data = annotated_case()
    data["alignment"]["independence"] = "unknown"
    data["alignment"]["judgments"] = [
        judgment("f1", state, ("d1",) if state == "matched" else ()),
    ]
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["matched"] == 0
    assert result["counts"]["spurious"] == 0
    assert result["pending_finding_ids"] == ["f1"]


@pytest.mark.parametrize(
    ("kind", "basis", "independence", "scope"),
    [
        ("human", "historical_exact", "independent", "human_reference"),
        ("synthetic", "synthetic", "independent", "synthetic_reference"),
        ("model", "reconstructed", "independent", "auxiliary_model_agreement"),
        ("workflow", "historical_exact", "independent", "workflow_agreement"),
        ("model", "historical_exact", "same_system", "self_agreement"),
    ],
)
def test_metrics_retain_their_actual_reference_and_alignment_scope(
    kind, basis, independence, scope
):
    data = annotated_case()
    for side in ("reference", "alignment"):
        data[side].update(
            producer=producer(kind), basis=basis, independence=independence
        )
    result = score_findings(bound_case(data))
    assert result["reference_scope"] == scope
    assert result["alignment_scope"] == scope
    assert_rate(result, "precision", "known", 1, 1, 1.0)


@pytest.mark.parametrize("alignment_kind", ["model", "workflow"])
def test_nonhuman_alignment_does_not_inherit_human_reference_authority(alignment_kind):
    data = annotated_case()
    data["reference"].update(producer=producer("human"), basis="historical_exact")
    data["alignment"].update(
        producer=producer(alignment_kind), basis="historical_exact"
    )
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["reference_scope"] == "human_reference"
    assert result["alignment_scope"] == (
        "auxiliary_model_agreement"
        if alignment_kind == "model"
        else "workflow_agreement"
    )
    assert result["counts"]["predictions"] == 1
    assert result["counts"]["reference_defects"] == 1
    # These are the attributed aligner's counts, never human correctness metrics.
    assert result["counts"]["matched"] == 1


@pytest.mark.parametrize("side", ["review", "reference"])
@pytest.mark.parametrize(
    "span",
    [
        {"item_id": "body", "start": 0, "end": 9, "quote": "Incorrect"},
        {"item_id": "missing", "start": 0, "end": 9, "quote": "Synthetic"},
        {"item_id": "body", "start": 0, "end": 999, "quote": "Synthetic"},
    ],
)
def test_bad_spans_invalidate_measurement_not_content(side, span):
    data = annotated_case()
    rows = "findings" if side == "review" else "defects"
    data[side][rows][0]["span"] = span
    result = score_findings(bound_case(data))
    assert result["measurement_valid"] is False
    assert_unassessed(result)
    assert result["counts"]["spurious"] == 0
    assert result["counts"]["matched"] == 0
    assert result["counts"]["predictions"] == 1
    assert result["counts"]["reference_defects"] == 1
    assert result["pending_finding_ids"] == ["f1"]
    assert result["unmatched_defect_ids"] == ["d1"]


def test_valid_unicode_span_and_different_descriptions_do_not_override_adjudication():
    data = annotated_case()
    data["artifact"]["units"][0]["content"] = "é🐈 synthetic"
    data["review"]["findings"][0].update(
        description="Different synthetic wording.",
        span={"item_id": "body", "start": 1, "end": 2, "quote": "🐈"},
    )
    result = score_findings(bound_case(data))
    assert result["measurement_valid"] is True
    assert_rate(result, "precision", "known", 1, 1, 1.0)


def test_category_incompatible_alleged_match_is_invalid_not_credit_or_spurious():
    data = annotated_case()
    data["review"]["findings"][0]["category"] = "style"
    result = score_findings(bound_case(data))
    assert result["measurement_valid"] is False
    assert_unassessed(result)
    assert result["counts"]["matched"] == 0
    assert result["counts"]["spurious"] == 0
    assert result["pending_finding_ids"] == ["f1"]
    assert result["unmatched_defect_ids"] == ["d1"]


@pytest.mark.parametrize(
    "state", [None, "matched", "spurious", "ambiguous", "unassessed"]
)
def test_suggestions_are_separate_and_cannot_mint_or_steal_defect_credit(state):
    data = annotated_case()
    data["review"]["findings"].append(finding("s1", severity="suggestion"))
    if state is not None:
        targets = () if state == "spurious" else ("d1",)
        data["alignment"]["judgments"].append(judgment("s1", state, targets))
    result = score_findings(bound_case(data))
    assert result["counts"]["suggestions"] == 1
    assert result["counts"]["predictions"] == 1
    assert result["counts"]["matched"] == 1
    assert result["counts"]["spurious"] == 0
    assert result["pending_finding_ids"] == []
    assert_rate(result, "precision", "known", 1, 1, 1.0)


def test_suggestion_only_match_cannot_find_a_critical_defect():
    data = annotated_case()
    data["review"]["findings"] = [finding("f1", severity="suggestion")]
    result = score_findings(bound_case(data))
    assert_rate(result, "precision", "undefined", 0, 0, None)
    assert_rate(result, "recall", "known", 0, 1, 0.0)
    assert result["counts"]["matched"] == 0
    assert result["counts"]["suggestions"] == 1
    assert result["counts"]["critical_missed"] == 1


def test_suggestion_bad_span_is_still_a_measurement_error():
    data = annotated_case()
    data["review"]["findings"].append(
        finding(
            "s1",
            severity="suggestion",
            span={"item_id": "missing", "start": 0, "end": 1, "quote": "X"},
        )
    )
    result = score_findings(bound_case(data))
    assert result["measurement_valid"] is False
    assert_unassessed(result)
    assert result["counts"]["suggestions"] == 1


@pytest.mark.parametrize("side", ["reference", "review", "alignment"])
@pytest.mark.parametrize("field", ["artifact_sha256", "rubric_id", "rubric_version"])
def test_shared_models_reject_binding_mismatch_before_scoring(side, field):
    data = annotated_case()
    data[side]["binding"][field] = "0" * 64 if field == "artifact_sha256" else "other"
    with pytest.raises(ValidationError, match="scoring_binding_mismatch"):
        ScoreCase.model_validate(seal(data))


@pytest.mark.parametrize(
    ("side", "rows"),
    [("review", "findings"), ("reference", "defects"), ("alignment", "judgments")],
)
def test_duplicate_ids_are_rejected_rather_than_silently_deduplicated(side, rows):
    data = annotated_case()
    data[side][rows].append(deepcopy(data[side][rows][0]))
    with pytest.raises(ValidationError, match="duplicate_scoring_id"):
        bound_case(data)


@pytest.mark.parametrize("field", ["finding_id", "defect_ids"])
def test_unknown_alignment_ids_are_rejected_by_shared_models(field):
    data = annotated_case()
    data["alignment"]["judgments"][0][field] = (
        "missing" if field == "finding_id" else ["missing"]
    )
    with pytest.raises(ValidationError, match="unknown_finding_alignment"):
        bound_case(data)


def test_mixed_states_keep_every_row_and_every_unresolved_identifier():
    data = annotated_case()
    data["reference"]["defects"].append(finding("d2"))
    data["review"]["findings"].extend(
        [
            finding("f2"),
            finding("f3"),
            finding("f4"),
            finding("f5"),
            finding("s1", severity="suggestion"),
        ]
    )
    data["alignment"]["judgments"].extend(
        [
            judgment("f2", "spurious", ()),
            judgment("f3", "ambiguous", ("d2",)),
            judgment("f4", "unassessed", ()),
        ]
    )
    result = score_findings(bound_case(data))
    counts = result["counts"]
    assert counts["predictions"] == 5
    assert counts["suggestions"] == 1
    assert counts["matched"] == 1
    assert counts["spurious"] == 1
    assert counts["ambiguous"] == 1
    assert counts["unassessed"] == 2
    assert counts["predictions"] == sum(
        counts[state] for state in ("matched", "spurious", "ambiguous", "unassessed")
    )
    assert (
        counts["reference_defects"] == counts["matched"] + counts["unmatched_defects"]
    )
    assert result["pending_finding_ids"] == ["f3", "f4", "f5"]
    assert result["ambiguous_finding_ids"] == ["f3"]
    assert result["unmatched_defect_ids"] == ["d2"]
    assert_rate(result, "precision", "unassessed", 1, 5, None)
    assert_unassessed(result)


def test_explicit_ambiguity_survives_unknown_alignment_authority():
    data = annotated_case()
    data["alignment"]["independence"] = "unknown"
    data["alignment"]["judgments"][0]["state"] = "ambiguous"
    result = score_findings(bound_case(data))
    assert_unassessed(result)
    assert result["counts"]["ambiguous"] == 1
    assert result["ambiguous_finding_ids"] == ["f1"]
    assert result["pending_finding_ids"] == ["f1"]


def test_zero_critical_denominator_is_undefined_even_when_recall_is_perfect():
    data = annotated_case()
    data["reference"]["defects"][0]["severity"] = "major"
    result = score_findings(bound_case(data))
    assert_rate(result, "recall", "known", 1, 1, 1.0)
    assert_rate(result, "critical_recall", "undefined", 0, 0, None)
    assert result["counts"]["critical_missed"] == 0


@pytest.mark.parametrize("side", ["review", "reference", "alignment"])
def test_nested_mutation_is_rejected_at_the_public_scoring_entry(side):
    case = bound_case(annotated_case())
    assert case.review is not None
    assert case.alignment is not None
    if side == "review":
        case.review.findings.clear()
    elif side == "reference":
        case.reference.defects.clear()
    else:
        case.alignment.judgments.clear()
    with pytest.raises(ValidationError):
        score_findings(case)


def test_scoring_is_deterministic_and_does_not_modify_supplied_adjudications():
    case = bound_case(annotated_case())
    before = case.model_dump()
    assert score_findings(case) == score_findings(case)
    assert case.model_dump() == before
