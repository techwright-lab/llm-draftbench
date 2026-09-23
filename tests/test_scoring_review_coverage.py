"""Review failure/coverage must not become a measured finding miss."""

import pytest
from scoring_cases import bound_case
from test_findings import annotated_case, finding
from test_scoring_contracts import bundle_data

from draftbench.scoring.decisions import assess_decision
from draftbench.scoring.findings import score_findings
from draftbench.scoring.models import ScoringBundle
from draftbench.scoring.reporting import score_bundle


@pytest.mark.parametrize(
    "decision,check_state,coverage",
    [
        ("error", None, "unavailable"),
        ("edit", None, "incomplete"),
        ("block", "unknown", "incomplete"),
        ("pass", "inapplicable", "incomplete"),
        ("abstain", None, "incomplete"),
        ("abstain", "pass", "incomplete"),
    ],
)
def test_incomplete_reviews_preserve_unmatched_references_without_known_misses(
    decision, check_state, coverage
):
    data = annotated_case()
    data["review"].update(
        decision=decision,
        findings=[],
        checks=[]
        if check_state is None
        else [{"check_id": "body-present", "state": check_state}],
    )
    data["alignment"]["judgments"] = []
    case = bound_case(data)
    assert assess_decision(case) == (
        "abstain" if decision == "abstain" else "measurement_error"
    )
    findings = score_findings(case)
    assert findings["metrics"]["recall"] == {
        "state": "unassessed",
        "numerator": 0,
        "denominator": 1,
        "value": None,
    }
    assert findings["metrics"]["critical_recall"]["value"] is None
    assert findings["counts"]["critical_missed"] is None
    assert findings["unmatched_defect_ids"] == ["d1"]
    assert findings["review_coverage"] == coverage
    report = score_bundle(ScoringBundle.model_validate(bundle_data([case])))
    group = report["cohorts"][0]
    assert group["findings"]["metrics"]["recall"]["state"] == "unassessed"
    assert group["findings"]["counts"]["critical_missed"] is None
    assert group["findings"]["review_coverage"] == {coverage: 1}


@pytest.mark.parametrize("decision", ["pass", "edit", "block"])
def test_completed_review_finding_nothing_still_has_known_zero_recall(decision):
    data = annotated_case()
    data["review"].update(decision=decision, findings=[])
    data["alignment"]["judgments"] = []
    case = bound_case(data)
    result = score_findings(case)
    assert result["review_coverage"] == "complete"
    assert result["metrics"]["recall"] == {
        "state": "known",
        "numerator": 0,
        "denominator": 1,
        "value": 0.0,
    }
    assert result["counts"]["critical_missed"] == 1
    summary = score_bundle(ScoringBundle.model_validate(bundle_data([case])))
    assert summary["cohorts"][0]["findings"]["metrics"]["recall"]["state"] == "known"


def test_partial_review_can_retain_attributed_precision_but_not_assert_exhaustive_recall():
    data = annotated_case()
    data["review"]["checks"] = []
    data["reference"]["defects"].append(finding("d2"))
    result = score_findings(bound_case(data))
    assert result["metrics"]["precision"] == {
        "state": "known",
        "numerator": 1,
        "denominator": 1,
        "value": 1.0,
    }
    assert result["metrics"]["recall"] == {
        "state": "unassessed",
        "numerator": 1,
        "denominator": 2,
        "value": None,
    }
    assert result["counts"]["critical_missed"] is None
    assert result["unmatched_defect_ids"] == ["d2"]


def test_explicit_error_does_not_claim_precision_from_incomplete_output():
    data = annotated_case()
    data["review"]["decision"] = "error"
    result = score_findings(bound_case(data))
    assert result["counts"]["matched"] == 1
    assert all(metric["value"] is None for metric in result["metrics"].values())
    assert result["counts"]["critical_missed"] is None
