"""Offline, annotation-bound constraint checks, not automatic prose fact-checking.

A fact result compares only caller-annotated literal values and units against an
inline source. Citation relevance is caller-supplied adjudication, never inferred
from prose, keywords or URLs. All matching uses exact Unicode codepoints. Text is
inert: there is no fetching, model invocation, regex, instruction execution or
normalization. The versioned result deliberately emits fixed reason codes rather
than copying artifact/source text into diagnostics.
"""

from .models import (
    Check,
    CheckState,
    CitationCheck,
    FactCheck,
    LengthCheck,
    LiteralCheck,
    NonemptyCheck,
    ScoreCase,
    required_check_ids,
    span_matches,
    validated_case,
)

_STATES: tuple[CheckState, ...] = ("pass", "fail", "unknown", "inapplicable", "invalid")
_Result = tuple[CheckState, str]


def _constraint(
    check: NonemptyCheck | LengthCheck | LiteralCheck, texts: dict[str, str]
) -> _Result:
    # Artifact emptiness has already been checked, independently of the rubric.
    if isinstance(check, NonemptyCheck):
        return "pass", "nonempty_artifact"
    if check.unit_id is not None and check.unit_id not in texts:
        return "invalid", "unknown_unit"
    selected = texts.values() if check.unit_id is None else (texts[check.unit_id],)
    if isinstance(check, LengthCheck):
        length = sum(len(text) for text in selected)
        if check.min_chars is not None and length < check.min_chars:
            return "fail", "length_below_minimum"
        if check.max_chars is not None and length > check.max_chars:
            return "fail", "length_above_maximum"
        return "pass", "length_within_bounds"
    # Never join units: neither a newline nor concatenation is an observed span.
    present = any(check.literal in text for text in selected)
    if check.kind == "required_literal":
        return (
            ("pass", "required_literal_present")
            if present
            else ("fail", "required_literal_missing")
        )
    return (
        ("fail", "forbidden_literal_present")
        if present
        else ("pass", "forbidden_literal_absent")
    )


def _missing_source(case: ScoreCase) -> _Result:
    if case.sources_state == "complete":
        return "fail", "supplied_source_missing"
    return "unknown", "source_unavailable"


def _citation(
    check: CitationCheck,
    case: ScoreCase,
    texts: dict[str, str],
    sources: dict[str, str],
) -> _Result:
    if not span_matches(check.span, texts):
        return "invalid", "observed_span_mismatch"
    if check.relevance == "irrelevant":
        return "fail", "source_irrelevant"
    if check.source_id not in sources:
        return _missing_source(case)
    if check.relevance == "unknown":
        return "unknown", "relevance_unknown"
    return "pass", "citation_supported"


def _fact_reference_error(check: FactCheck, sources: dict[str, str]) -> str | None:
    """Reject malformed supplied annotations before interpreting their content.

    An unavailable source cannot validate or invalidate quote offsets. A span
    pointing at a *different* named source is nevertheless an annotation error.
    Missing annotations are handled as unknown by the caller, not inferred here.
    """
    reference = check.reference_span
    if reference is None:
        return None
    if check.source_id is not None and reference.item_id != check.source_id:
        return "reference_span_mismatch"
    if reference.item_id not in sources:
        return None
    if not span_matches(reference, sources):
        return "reference_span_mismatch"
    if check.expected_value is not None and check.expected_value not in reference.quote:
        return "expected_literal_unanchored"
    if check.expected_unit is not None and check.expected_unit not in reference.quote:
        return "expected_literal_unanchored"
    return None


def _fact(
    check: FactCheck, case: ScoreCase, texts: dict[str, str], sources: dict[str, str]
) -> _Result:
    if not span_matches(check.span, texts):
        return "invalid", "observed_span_mismatch"
    if check.observed_value not in check.span.quote or (
        check.observed_unit is not None and check.observed_unit not in check.span.quote
    ):
        return "invalid", "observed_literal_unanchored"
    reference_error = _fact_reference_error(check, sources)
    if reference_error is not None:
        return "invalid", reference_error
    if check.assertion_mode != "asserted":
        return "fail", "unauthorized_nonfactual"
    if check.relevance == "irrelevant":
        return "fail", "source_irrelevant"
    # No named reference is a missing annotation, not a fabricated source ID.
    if check.source_id is None:
        return "unknown", "reference_annotation_missing"
    if check.source_id not in sources:
        return _missing_source(case)
    if check.reference_span is None or check.expected_value is None:
        return "unknown", "reference_annotation_missing"
    if check.relevance == "unknown":
        return "unknown", "relevance_unknown"
    if (check.observed_value, check.observed_unit) != (
        check.expected_value,
        check.expected_unit,
    ):
        return "fail", "reference_mismatch"
    return "pass", "reference_match"


def _evaluate(
    check: Check,
    case: ScoreCase,
    texts: dict[str, str],
    sources: dict[str, str],
    *,
    empty: bool,
) -> _Result:
    if check.applicability == "inapplicable":
        return "inapplicable", "declared_inapplicable"
    if (
        isinstance(check, FactCheck)
        and check.assertion_mode != "asserted"
        and check.authorized_nonfactual
    ):
        return "inapplicable", "authorized_nonfactual"
    if empty:
        return "invalid", "empty_artifact"
    if isinstance(check, CitationCheck):
        return _citation(check, case, texts, sources)
    if isinstance(check, FactCheck):
        return _fact(check, case, texts, sources)
    return _constraint(check, texts)


def score_deterministic(case: ScoreCase) -> dict:
    """Score requested checks only; optional results never gate required checks.

    Overall precedence is empty artifact / required invalid, required fail,
    required unknown, then pass. No applicable required checks is unassessed,
    including a rubric consisting solely of authorized fiction/hypotheticals.
    Invalid integrity/bindings raise validation errors at the public boundary;
    validly bound but malformed span annotations produce invalid measurements.
    """
    case = validated_case(case)
    texts = {unit.unit_id: unit.content for unit in case.artifact.units}
    sources = {source.source_id: source.text for source in case.sources}
    empty = not any(text.strip() for text in texts.values())
    required_ids = required_check_ids(case.rubric)
    checks = []
    states = dict.fromkeys(_STATES, 0)
    required_states = []
    for check in case.rubric.checks:
        state, reason = _evaluate(check, case, texts, sources, empty=empty)
        checks.append(
            {
                "check_id": check.check_id,
                "kind": check.kind,
                "applicability": check.applicability,
                "state": state,
                "reason": reason,
            }
        )
        states[state] += 1
        if check.check_id in required_ids:
            required_states.append(state)
    if empty or "invalid" in required_states:
        outcome = "invalid"
    elif "fail" in required_states:
        outcome = "fail"
    elif not required_states or any(state != "pass" for state in required_states):
        outcome = "unassessed"
    else:
        outcome = "pass"
    return {
        "metric_version": "deterministic-v1",
        "outcome": outcome,
        "checks": checks,
        "states": states,
    }
