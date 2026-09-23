"""Reviewer decisions and labeled accepted-set risk, not a composite quality score.

Rates describe one exact scoring cohort. They are conditional on the declared
reference provenance; automated reference agreement is never human ground truth.
Case and source-family counts are descriptive, not independent safety samples.
"""

from .models import (
    ScoreCase,
    cohort_key,
    observation_key,
    rate,
    reference_scope,
    required_check_ids,
    span_matches,
    unique,
    validated_case,
)


def assess_decision(case: ScoreCase) -> str:
    """Return pass/edit/block/abstain/measurement_error for validated evidence.

    Incomplete required-check coverage cannot support a decisive assessment.
    Explicit abstention does not claim complete coverage, but malformed supplied
    annotations and empty artifacts remain measurement errors. Only major and
    critical defects are material; suggestions and minor defects alone do not
    contradict a pass. This validates the review, not the truth of its checks.
    """
    return _assess_decision(validated_case(case))


def _assess_decision(case: ScoreCase) -> str:
    """Assess a snapshot already revalidated by a public entrypoint."""
    review = case.review
    if review is None or review.decision == "error":
        return "measurement_error"
    if not any(unit.content.strip() for unit in case.artifact.units):
        return "measurement_error"
    if any(check.state == "invalid" for check in review.checks):
        return "measurement_error"

    texts = {unit.unit_id: unit.content for unit in case.artifact.units}
    if any(
        finding.span is not None and not span_matches(finding.span, texts)
        for finding in review.findings
    ):
        return "measurement_error"

    if review.decision == "abstain":
        return "abstain"

    required = required_check_ids(case.rubric)
    checks = {check.check_id: check.state for check in review.checks}
    if not required or any(
        checks.get(check_id) not in {"pass", "fail"} for check_id in required
    ):
        return "measurement_error"

    if review.decision == "pass" and (
        any(checks[check_id] == "fail" for check_id in required)
        or any(
            finding.kind == "defect" and finding.severity in {"major", "critical"}
            for finding in review.findings
        )
    ):
        return "measurement_error"
    return review.decision


def score_decisions(cases: list[ScoreCase]) -> dict:
    """Score one cohort with explicit class, accepted-set, and coverage counts.

    False-pass and false-block denominators include every eligible labeled case,
    including abstentions and measurement errors. Accepted-set risk uses only
    effective passes with eligible known labels, and reports unknown-label passes
    separately. Invalid raw passes remain visible rather than being credited as
    safe decisions. Unassessed reference labels supply no eligible known gold.

    Zero denominators are undefined; unavailable reference assessment is
    unassessed. An empty cohort has undefined rates and unassessed scope. Repeated
    observations are rejected: v1 has no protocol for repeated-trial inference.
    """
    cases = [validated_case(case) for case in cases]
    unique(case.case_id for case in cases)
    if len({cohort_key(case) for case in cases}) > 1:
        raise ValueError("mixed_scoring_cohort")
    unique(observation_key(case) for case in cases)

    scope = reference_scope(cases[0].reference) if cases else "unassessed"
    reference_assessed = scope != "unassessed"
    decisions: dict[str, int] = dict.fromkeys(
        ("pass", "edit", "block", "abstain", "measurement_error"), 0
    )
    raw_decisions: dict[str, int] = dict.fromkeys(
        ("pass", "edit", "block", "abstain", "error", "missing"), 0
    )
    counts = {
        "case_count": len(cases),
        "source_family_count": len({case.source_family for case in cases}),
        "gold_acceptable": 0,
        "gold_unacceptable": 0,
        "gold_unknown": 0,
        "passed_known": 0,
        "passed_unknown": 0,
        "invalid_passes": 0,
    }
    false_passes = 0
    false_blocks = 0
    for case in cases:
        raw = case.review.decision if case.review is not None else "missing"
        effective = _assess_decision(case)
        raw_decisions[raw] += 1
        decisions[effective] += 1
        if raw == "pass" and effective == "measurement_error":
            counts["invalid_passes"] += 1

        gold = case.reference.acceptability if reference_assessed else "unknown"
        counts[f"gold_{gold}"] += 1
        if effective == "pass":
            counts["passed_unknown" if gold == "unknown" else "passed_known"] += 1
            if gold == "unacceptable":
                false_passes += 1
        elif effective in {"edit", "block"} and gold == "acceptable":
            false_blocks += 1

    # The empty-cohort contract is undefined, not an unavailable assessment.
    assessed = reference_assessed or not cases
    return {
        "metric_version": "decisions-v1",
        "reference_scope": scope,
        "counts": counts,
        "decisions": decisions,
        "raw_decisions": raw_decisions,
        "metrics": {
            "false_pass_rate": rate(
                false_passes, counts["gold_unacceptable"], assessed=assessed
            ),
            "false_block_rate": rate(
                false_blocks, counts["gold_acceptable"], assessed=assessed
            ),
            "accepted_set_risk": rate(
                false_passes, counts["passed_known"], assessed=assessed
            ),
            "decision_coverage": rate(
                decisions["pass"] + decisions["edit"] + decisions["block"], len(cases)
            ),
            "abstention_rate": rate(decisions["abstain"], len(cases)),
            "measurement_error_rate": rate(decisions["measurement_error"], len(cases)),
        },
    }
