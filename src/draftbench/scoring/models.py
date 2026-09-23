"""Versioned scoring inputs; annotations are evidence, never executable instructions."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..identity import canonical_bytes, identity
from ..models import (
    Contract,
    Digest,
    Identified,
    Identifier,
    Producer,
    Rights,
    ShortText,
    Text,
)

CheckState = Literal["pass", "fail", "unknown", "inapplicable", "invalid"]
Applicability = Literal["required", "optional", "inapplicable"]
Basis = Literal["historical_exact", "reconstructed", "synthetic"]
Independence = Literal["independent", "same_system", "unknown"]
Index = Annotated[int, Field(ge=0, le=100_000)]


def artifact_digest(units: list[dict]) -> str:
    return hashlib.sha256(canonical_bytes({"units": units})).hexdigest()


class ScoringUnit(Contract):
    unit_id: Identifier
    kind: Literal["text", "social_post"]
    content: Annotated[str, Field(max_length=100_000)]


class ScoringArtifact(Contract):
    sha256: Digest
    units: Annotated[list[ScoringUnit], Field(max_length=256)]

    @model_validator(mode="after")
    def integrity(self):
        unique(unit.unit_id for unit in self.units)
        if self.sha256 != artifact_digest([unit.model_dump() for unit in self.units]):
            raise ValueError("scoring_artifact_mismatch")
        return self


class Binding(Contract):
    artifact_sha256: Digest
    rubric_id: Identifier
    rubric_version: ShortText


class Span(Contract):
    item_id: Identifier
    start: Index
    end: Index
    quote: Text

    @model_validator(mode="after")
    def bounds(self):
        if self.end <= self.start:
            raise ValueError("invalid_span_bounds")
        return self


def span_matches(span: Span, texts: dict[str, str]) -> bool:
    text = texts.get(span.item_id)
    return (
        text is not None
        and span.end <= len(text)
        and text[span.start : span.end] == span.quote
    )


class EvidenceSource(Contract):
    source_id: Identifier
    text: Annotated[str, Field(max_length=100_000)]
    sha256: Digest

    @model_validator(mode="after")
    def integrity(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("scoring_source_mismatch")
        return self


class CheckBase(Contract):
    check_id: Identifier
    applicability: Applicability


class NonemptyCheck(CheckBase):
    kind: Literal["nonempty"]


class LengthCheck(CheckBase):
    kind: Literal["length"]
    unit_id: Identifier | None
    min_chars: Index | None
    max_chars: Index | None

    @model_validator(mode="after")
    def limits(self):
        if self.min_chars is None and self.max_chars is None:
            raise ValueError("missing_length_bounds")
        if (
            self.min_chars is not None
            and self.max_chars is not None
            and self.min_chars > self.max_chars
        ):
            raise ValueError("invalid_length_bounds")
        return self


class LiteralCheck(CheckBase):
    kind: Literal["required_literal", "forbidden_literal"]
    unit_id: Identifier | None
    literal: Text


class CitationCheck(CheckBase):
    kind: Literal["citation"]
    span: Span
    source_id: Identifier
    relevance: Literal["relevant", "irrelevant", "unknown"]


class FactCheck(CheckBase):
    kind: Literal["fact"]
    span: Span
    observed_value: ShortText
    observed_unit: ShortText | None
    source_id: Identifier | None
    reference_span: Span | None
    expected_value: ShortText | None
    expected_unit: ShortText | None
    relevance: Literal["relevant", "irrelevant", "unknown"]
    assertion_mode: Literal["asserted", "hypothetical", "fictional"]
    authorized_nonfactual: bool


Check = Annotated[
    NonemptyCheck | LengthCheck | LiteralCheck | CitationCheck | FactCheck,
    Field(discriminator="kind"),
]


class Rubric(Contract):
    rubric_id: Identifier
    version: ShortText
    checks: Annotated[list[Check], Field(max_length=256)]

    @model_validator(mode="after")
    def identifiers(self):
        unique(check.check_id for check in self.checks)
        return self


def required_check_ids(rubric: Rubric) -> set[str]:
    return {
        check.check_id
        for check in rubric.checks
        if check.applicability == "required"
        and not (
            isinstance(check, FactCheck)
            and check.authorized_nonfactual
            and check.assertion_mode != "asserted"
        )
    }


class Finding(Contract):
    finding_id: Identifier
    kind: Literal["defect", "suggestion"]
    category: Identifier
    severity: Literal["critical", "major", "minor", "suggestion"]
    description: Text
    span: Span | None

    @model_validator(mode="after")
    def suggestion(self):
        if (self.kind == "suggestion") != (self.severity == "suggestion"):
            raise ValueError("finding_kind_severity_mismatch")
        return self


class Reference(Contract):
    binding: Binding
    producer: Producer | None
    basis: Basis | None
    independence: Independence
    acceptability: Literal["acceptable", "unacceptable", "unknown"]
    defect_inventory: Literal["complete", "partial", "unavailable"]
    defects: Annotated[list[Finding], Field(max_length=512)]

    @model_validator(mode="after")
    def provenance(self):
        if (self.producer is None) != (self.basis is None):
            raise ValueError("missing_reference_basis")
        if self.producer is None and (
            self.acceptability != "unknown" or self.defect_inventory != "unavailable"
        ):
            raise ValueError("unattributed_reference")
        if self.defect_inventory == "unavailable" and self.defects:
            raise ValueError("unavailable_defect_inventory")
        if any(finding.kind != "defect" for finding in self.defects):
            raise ValueError("suggestion_is_not_reference_defect")
        unique(finding.finding_id for finding in self.defects)
        return self


class ReviewCheck(Contract):
    check_id: Identifier
    state: CheckState


class ReviewerAssessment(Contract):
    binding: Binding
    producer: Producer
    decision: Literal["pass", "edit", "block", "abstain", "error"]
    checks: Annotated[list[ReviewCheck], Field(max_length=256)]
    findings: Annotated[list[Finding], Field(max_length=512)]

    @model_validator(mode="after")
    def identifiers(self):
        unique(check.check_id for check in self.checks)
        unique(finding.finding_id for finding in self.findings)
        return self


class FindingJudgment(Contract):
    finding_id: Identifier
    state: Literal["matched", "spurious", "ambiguous", "unassessed"]
    defect_ids: Annotated[list[Identifier], Field(max_length=512)]

    @model_validator(mode="after")
    def shape(self):
        unique(self.defect_ids)
        if self.state == "matched" and len(self.defect_ids) != 1:
            raise ValueError("matched_requires_one_defect")
        if self.state == "spurious" and self.defect_ids:
            raise ValueError("spurious_has_no_defect")
        return self


class Alignment(Contract):
    binding: Binding
    producer: Producer | None
    basis: Basis | None
    independence: Independence
    judgments: Annotated[list[FindingJudgment], Field(max_length=512)]

    @model_validator(mode="after")
    def provenance(self):
        if (self.producer is None) != (self.basis is None):
            raise ValueError("missing_alignment_basis")
        if self.producer is None and self.judgments:
            raise ValueError("unattributed_alignment")
        unique(item.finding_id for item in self.judgments)
        return self


class ScoreCase(Identified):
    case_id: Identifier
    source_family: Identifier
    artifact: ScoringArtifact
    rubric: Rubric
    sources_state: Literal["complete", "partial", "unavailable"]
    sources: Annotated[list[EvidenceSource], Field(max_length=256)]
    reference: Reference
    review: ReviewerAssessment | None
    alignment: Alignment | None

    @model_validator(mode="after")
    def bindings(self):
        unique(source.source_id for source in self.sources)
        if self.sources_state == "unavailable" and self.sources:
            raise ValueError("unavailable_sources")
        expected = Binding(
            artifact_sha256=self.artifact.sha256,
            rubric_id=self.rubric.rubric_id,
            rubric_version=self.rubric.version,
        )
        for item in (self.reference, self.review, self.alignment):
            if item is not None and item.binding != expected:
                raise ValueError("scoring_binding_mismatch")
        if self.review is not None:
            if {row.check_id for row in self.review.checks} - {
                check.check_id for check in self.rubric.checks
            }:
                raise ValueError("unknown_review_check")
        if self.alignment is not None:
            predictions = (
                {finding.finding_id for finding in self.review.findings}
                if self.review
                else set()
            )
            defects = {finding.finding_id for finding in self.reference.defects}
            for item in self.alignment.judgments:
                if item.finding_id not in predictions or set(item.defect_ids) - defects:
                    raise ValueError("unknown_finding_alignment")
        return self


class ScoringBundle(Identified):
    format: Literal["draftbench-scoring-v1"]
    purpose: Literal["synthetic_infrastructure", "evaluation"]
    rights: Rights
    cases: Annotated[list[ScoreCase], Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def identifiers(self):
        unique(case.case_id for case in self.cases)
        observations = []
        for case in self.cases:
            observations.append(observation_key(case))
            if self.purpose == "synthetic_infrastructure":
                for item in (case.reference, case.alignment):
                    if (
                        item is not None
                        and item.producer is not None
                        and item.basis != "synthetic"
                    ):
                        raise ValueError("synthetic_reference_basis_required")
        unique(observations)
        return self


def unique(values):
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError("duplicate_scoring_id")


def validated_case(case: ScoreCase) -> ScoreCase:
    if not isinstance(case, ScoreCase):
        raise ValueError("invalid_scoring_case")
    return ScoreCase.model_validate(case.model_dump(mode="json"))


def observation_key(case: ScoreCase) -> str:
    return identity(
        {
            "artifact_sha256": case.artifact.sha256,
            "rubric": case.rubric.model_dump(),
            "sources_state": case.sources_state,
            "sources": sorted(
                (source.model_dump() for source in case.sources),
                key=lambda row: row["source_id"],
            ),
            "reviewer": case.review.producer.model_dump() if case.review else None,
        }
    )


def reference_scope(reference: Reference | Alignment) -> str:
    if reference.producer is None or reference.independence == "unknown":
        return "unassessed"
    if reference.basis == "synthetic" or reference.producer.kind == "synthetic":
        return "synthetic_reference"
    if reference.independence == "same_system":
        return "self_agreement"
    return {
        "human": "human_reference",
        "model": "auxiliary_model_agreement",
        "workflow": "workflow_agreement",
    }[reference.producer.kind]


def rate(numerator: int, denominator: int, *, assessed: bool = True) -> dict:
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or not 0 <= numerator <= denominator
    ):
        raise ValueError("invalid_metric_counts")
    state = (
        "unassessed" if not assessed else "undefined" if denominator == 0 else "known"
    )
    return {
        "state": state,
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if state == "known" else None,
    }


def cohort_key(case: ScoreCase) -> str:
    return identity(
        {
            "rubric_id": case.rubric.rubric_id,
            "rubric_version": case.rubric.version,
            "reviewer": case.review.producer.model_dump() if case.review else None,
            "reference_scope": reference_scope(case.reference),
            "reference_producer": case.reference.producer.model_dump()
            if case.reference.producer
            else None,
            "reference_basis": case.reference.basis,
        }
    )
