"""Read-only, identifier-free summaries of reference-conditioned offline scoring."""

from collections import Counter, defaultdict
from pathlib import Path

from ..identity import strict_json_loads
from ..loader import SuiteError, _Reader
from .models import ScoringBundle, cohort_key, rate, reference_scope

CHECK_STATES = ("pass", "fail", "unknown", "inapplicable", "invalid")
OUTCOMES = ("pass", "fail", "unassessed", "invalid")
FINDING_COUNTS = (
    "predictions",
    "suggestions",
    "reference_defects",
    "matched",
    "spurious",
    "unassessed",
    "ambiguous",
    "unmatched_defects",
    "critical_defects",
    "critical_matched",
    "critical_missed",
)


class ScoringError(ValueError):
    """Fixed safe codes only; no private values or filenames."""

    def __init__(self, code):
        allowed = {
            "invalid_scoring_bundle",
            "input_unreadable",
            "unsafe_reference",
            "input_limit",
        }
        super().__init__(code if code in allowed else "invalid_scoring_bundle")


def load_scoring_bundle(path: str | Path) -> ScoringBundle:
    try:
        path = Path(path).absolute()
        data = _Reader(path.parent.resolve()).read(path)
        return ScoringBundle.model_validate(strict_json_loads(data.decode("utf-8")))
    except SuiteError as exc:
        raise ScoringError(str(exc)) from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise ScoringError("invalid_scoring_bundle") from None


def _finding_summary(results):
    counts = {}
    for key in FINDING_COUNTS:
        values = [result["counts"][key] for result in results]
        counts[key] = None if any(value is None for value in values) else sum(values)
    metrics = {}
    for key in ("precision", "recall", "critical_recall"):
        rows = [result["metrics"][key] for result in results]
        metrics[key] = rate(
            sum(row["numerator"] for row in rows),
            sum(row["denominator"] for row in rows),
            assessed=all(row["state"] != "unassessed" for row in rows)
            and all(result["measurement_valid"] for result in results),
        )
    return {
        "metric_version": "findings-v1",
        "counts": counts,
        "metrics": metrics,
        "alignment_scopes": dict(
            sorted(Counter(result["alignment_scope"] for result in results).items())
        ),
        "review_coverage": dict(
            sorted(Counter(result["review_coverage"] for result in results).items())
        ),
        "invalid_measurements": sum(
            not result["measurement_valid"] for result in results
        ),
    }


def score_bundle(bundle: ScoringBundle) -> dict:
    from .decisions import score_decisions
    from .deterministic import score_deterministic
    from .findings import score_findings

    if not isinstance(bundle, ScoringBundle):
        raise ScoringError("invalid_scoring_bundle")
    try:
        bundle = ScoringBundle.model_validate(bundle.model_dump(mode="json"))
    except (ValueError, TypeError, RecursionError):
        raise ScoringError("invalid_scoring_bundle") from None
    grouped = defaultdict(list)
    for case in bundle.cases:
        grouped[cohort_key(case)].append(case)
    cohorts = []
    for index, key in enumerate(sorted(grouped), 1):
        cases = grouped[key]
        deterministic = [score_deterministic(case) for case in cases]
        outcomes = Counter(result["outcome"] for result in deterministic)
        check_counts = Counter()
        for result in deterministic:
            check_counts.update(result["states"])
        decisions = score_decisions(cases)
        findings = _finding_summary([score_findings(case) for case in cases])
        cohorts.append(
            {
                "cohort_index": index,
                "reference_scope": reference_scope(cases[0].reference),
                "deterministic": {
                    "metric_version": "deterministic-v1",
                    "outcomes": {state: outcomes[state] for state in OUTCOMES},
                    "checks": {state: check_counts[state] for state in CHECK_STATES},
                },
                "decisions": decisions,
                "findings": findings,
            }
        )
    if sum(cohort["decisions"]["counts"]["case_count"] for cohort in cohorts) != len(
        bundle.cases
    ):
        raise ScoringError("invalid_scoring_bundle")
    return {
        "schema_version": "1",
        "report_kind": "offline_reference_scoring",
        "valid": True,
        "scoring_performed": True,
        "model_execution_performed": False,
        "purpose": bundle.purpose,
        "case_count": len(bundle.cases),
        "source_family_count": len({case.source_family for case in bundle.cases}),
        "cohort_count": len(cohorts),
        "cohorts": cohorts,
        "interpretation": "Reference-conditioned observations, not universal quality, independent gold verification, a leaderboard or publication permission.",
    }
