"""One-to-one bookkeeping of explicit finding adjudications, not semantic matching."""

from collections import Counter

from .decisions import assess_decision
from .models import ScoreCase, rate, reference_scope, span_matches, validated_case


def score_findings(case: ScoreCase) -> dict:
    """Score defect predictions only under their supplied reference/alignment scopes.

    Counts describe usable attributed judgments, not independent proof of content
    correctness. Different authorities retain their separate scopes and counts but
    never yield assessed rates. Unknown alignment authority cannot resolve a row.
    Optional suggestions neither consume reference defects nor enter precision or
    pending adjudication; their supplied spans must still identify actual text.

    Pending IDs include every unresolved defect prediction, including ambiguous
    rows. Unmatched reference IDs mean *not confirmed matched*, not known misses.
    Zero denominators are undefined, and incomplete evidence is unassessed.
    """
    case = validated_case(case)
    if case.review is None or case.review.decision == "error":
        review_coverage = "unavailable"
    elif assess_decision(case) in {"pass", "edit", "block"}:
        review_coverage = "complete"
    else:
        review_coverage = "incomplete"
    reference = case.reference
    review_rows = case.review.findings if case.review is not None else []
    predictions = [row for row in review_rows if row.kind == "defect"]
    defects = {row.finding_id: row for row in reference.defects}
    judgments = (
        {row.finding_id: row for row in case.alignment.judgments}
        if case.alignment is not None
        else {}
    )
    reference_authority = reference_scope(reference)
    alignment_authority = (
        reference_scope(case.alignment) if case.alignment is not None else "unassessed"
    )

    texts = {unit.unit_id: unit.content for unit in case.artifact.units}
    invalid_predictions = {
        row.finding_id
        for row in review_rows
        if row.span is not None and not span_matches(row.span, texts)
    }
    invalid_defects = {
        row.finding_id
        for row in reference.defects
        if row.span is not None and not span_matches(row.span, texts)
    }
    measurement_valid = not invalid_predictions and not invalid_defects

    # Count all alleged defect matches before resolving any: order cannot select
    # a winner when multiple predictions claim the same reference defect.
    claims = Counter(
        judgments[row.finding_id].defect_ids[0]
        for row in predictions
        if row.finding_id in judgments and judgments[row.finding_id].state == "matched"
    )
    states = {}
    matched_defects = set()
    for row in predictions:
        finding_id = row.finding_id
        judgment = judgments.get(finding_id)
        state = "unassessed"
        if judgment is not None and judgment.state == "ambiguous":
            # Preserve declared ambiguity even when its authority is unknown.
            state = "ambiguous"
        elif judgment is not None and judgment.state == "matched":
            defect_id = judgment.defect_ids[0]
            category_valid = row.category == defects[defect_id].category
            measurement_valid = measurement_valid and category_valid
            if claims[defect_id] > 1:
                state = "ambiguous"
            elif (
                alignment_authority != "unassessed"
                and finding_id not in invalid_predictions
                and defect_id not in invalid_defects
                and category_valid
            ):
                state = "matched"
                matched_defects.add(defect_id)
        elif (
            judgment is not None
            and judgment.state == "spurious"
            and alignment_authority != "unassessed"
            and finding_id not in invalid_predictions
        ):
            state = "spurious"
        states[finding_id] = state

    state_counts = Counter(states.values())
    pending = sorted(
        finding_id
        for finding_id, state in states.items()
        if state in ("unassessed", "ambiguous")
    )
    ambiguous = sorted(
        finding_id for finding_id, state in states.items() if state == "ambiguous"
    )
    unmatched = sorted(defects.keys() - matched_defects)
    critical_defects = {
        finding_id for finding_id, row in defects.items() if row.severity == "critical"
    }
    critical_matched = len(critical_defects & matched_defects)
    assessed = (
        measurement_valid
        and review_coverage != "unavailable"
        and reference.defect_inventory != "unavailable"
        and reference_authority != "unassessed"
        and reference_authority == alignment_authority
        and not pending
    )
    recall_assessed = (
        assessed
        and reference.defect_inventory == "complete"
        and review_coverage == "complete"
    )
    matched = len(matched_defects)

    return {
        "metric_version": "findings-v1",
        "reference_scope": reference_authority,
        "alignment_scope": alignment_authority,
        "review_coverage": review_coverage,
        "measurement_valid": measurement_valid,
        "counts": {
            "predictions": len(predictions),
            "suggestions": len(review_rows) - len(predictions),
            "reference_defects": len(defects),
            "matched": matched,
            "spurious": state_counts["spurious"],
            "unassessed": state_counts["unassessed"],
            "ambiguous": state_counts["ambiguous"],
            "unmatched_defects": len(unmatched),
            "critical_defects": len(critical_defects),
            "critical_matched": critical_matched,
            "critical_missed": (
                len(critical_defects) - critical_matched if recall_assessed else None
            ),
        },
        "metrics": {
            "precision": rate(matched, len(predictions), assessed=assessed),
            "recall": rate(matched, len(defects), assessed=recall_assessed),
            "critical_recall": rate(
                critical_matched, len(critical_defects), assessed=recall_assessed
            ),
        },
        "pending_finding_ids": pending,
        "ambiguous_finding_ids": ambiguous,
        "unmatched_defect_ids": unmatched,
    }
