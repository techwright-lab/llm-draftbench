"""Strict v1 contracts. All fields are explicit; unknown fields are rejected.

JSON Schema describes structure. Cross-reference, identity and file integrity
constraints additionally require the offline loader, not only a schema validator.
"""

from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import identity

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
Text = Annotated[str, Field(min_length=1, max_length=100_000)]
ShortText = Annotated[str, Field(min_length=1, max_length=512)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RelativePath = Annotated[
    str,
    Field(
        max_length=512,
        pattern=r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$",
    ),
]
State = Literal["available", "unavailable", "unknown", "inapplicable", "invalid"]
Role = Literal["writer", "reviewer", "revision"]
PositiveInt = Annotated[int, Field(ge=1, le=10_000)]


class Contract(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )


class FileReference(Contract):
    path: RelativePath
    sha256: Digest

    @model_validator(mode="after")
    def portable_names(self) -> Self:
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        for part in self.path.split("/"):
            if (
                len(part) > 255
                or part.endswith(".")
                or part.split(".")[0].upper() in reserved
            ):
                raise ValueError("nonportable_reference")
        return self


class Rights(Contract):
    license: ShortText
    usage: Literal["private", "redistributable"]
    authorization: Text


ObservationState = TypeVar("ObservationState")


class Observation(Contract, Generic[ObservationState]):
    state: ObservationState
    value: ShortText | None

    @model_validator(mode="after")
    def observation(self) -> Self:
        if (self.state == "known") != (self.value is not None):
            raise ValueError("observation_mismatch")
        return self


TextObservation = Observation[Literal["known", "unknown", "unavailable"]]
ModelObservation = Observation[
    Literal["known", "unknown", "unavailable", "inapplicable"]
]


class Metadata(Contract):
    content_type: TextObservation
    domain: TextObservation
    lane: TextObservation
    body_format: TextObservation
    source_system: TextObservation
    source_revision: TextObservation


class Producer(Contract):
    kind: Literal["human", "model", "workflow", "synthetic"]
    name: TextObservation
    version: TextObservation
    requested_model: ModelObservation
    served_model: ModelObservation

    @model_validator(mode="after")
    def model_provenance(self) -> Self:
        for observation in (self.requested_model, self.served_model):
            if (self.kind != "model") != (observation.state == "inapplicable"):
                raise ValueError("model_identity_applicability_mismatch")
        return self


class Artifact(Contract):
    artifact_id: Identifier
    file: FileReference
    rights: Rights
    producer: Producer


class Source(Contract):
    state: State
    artifact: Artifact | None

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "available") != (self.artifact is not None):
            raise ValueError("source_availability_mismatch")
        return self


class Message(Contract):
    role: Literal["system", "user", "assistant"]
    content: Text


class InputPacket(Contract):
    basis: Literal["historical_exact", "reconstructed", "synthetic"]
    prompt_version: ShortText
    messages: Annotated[list[Message], Field(min_length=1, max_length=256)]
    evidence_ids: Annotated[list[Identifier], Field(max_length=256)]
    draft_ids: Annotated[list[Identifier], Field(max_length=256)]
    review_ids: Annotated[list[Identifier], Field(max_length=256)]
    producer: Producer


class RoleInput(Contract):
    state: State
    input: InputPacket | None

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "available") != (self.input is not None):
            raise ValueError("input_availability_mismatch")
        return self


class Generator(Contract):
    writer: RoleInput
    reviewer: RoleInput
    revision: RoleInput


class Evidence(Contract):
    state: State
    artifacts: Annotated[list[Artifact], Field(max_length=256)]

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "available") != bool(self.artifacts):
            raise ValueError("evidence_availability_mismatch")
        return self


class PublicationUnit(Contract):
    unit_id: Identifier
    kind: Literal["text", "social_post"]
    content: Text


class Draft(Contract):
    draft_id: Identifier
    version: PositiveInt
    units: Annotated[list[PublicationUnit], Field(min_length=1, max_length=256)]
    producer: Producer

    @model_validator(mode="after")
    def unique_units(self) -> Self:
        _unique(unit.unit_id for unit in self.units)
        return self


class Review(Contract):
    review_id: Identifier
    round: PositiveInt
    draft_id: Identifier
    feedback: Text
    producer: Producer


class History(Contract):
    state: State
    drafts: Annotated[list[Draft], Field(max_length=256)]
    reviews: Annotated[list[Review], Field(max_length=256)]

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "available") != bool(self.drafts or self.reviews):
            raise ValueError("history_availability_mismatch")
        return self


class Label(Contract):
    label_id: Identifier
    basis: Literal["historical_exact", "reconstructed", "synthetic"]
    draft_id: Identifier
    question_id: Identifier
    question_version: ShortText
    state: Literal["known", "unknown", "inapplicable", "invalid"]
    value: bool | int | Text | None
    producer: Producer

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "known") != (self.value is not None):
            raise ValueError("label_availability_mismatch")
        return self


class Evaluator(Contract):
    labels: Annotated[list[Label], Field(max_length=2048)]


def _unique(values) -> set:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError("duplicate_id")
    return set(items)


def _references(values: list[str], available: set) -> None:
    if not _unique(values) <= available:
        raise ValueError("unknown_reference")


class Identified(Contract):
    schema_version: Literal["1"]
    identity: Digest

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        if identity(self.model_dump(mode="json")) != self.identity:
            raise ValueError("identity_mismatch")
        return self


class Case(Identified):
    case_id: Identifier
    source_family: Identifier
    metadata: Metadata
    split: Literal["development", "confirmation"]
    rights: Rights
    source: Source
    generator: Generator
    evidence: Evidence
    history: History
    evaluator: Evaluator

    @model_validator(mode="after")
    def references(self) -> Self:
        sources = [self.source.artifact] if self.source.artifact is not None else []
        if self.rights.usage == "redistributable" and any(
            artifact.rights.usage != "redistributable"
            for artifact in [*sources, *self.evidence.artifacts]
        ):
            raise ValueError("rights_conflict")
        evidence = _unique(a.artifact_id for a in self.evidence.artifacts)
        _unique([*(a.artifact_id for a in sources), *evidence])
        drafts = _unique(d.draft_id for d in self.history.drafts)
        _unique(d.version for d in self.history.drafts)
        reviews = _unique(r.review_id for r in self.history.reviews)
        _unique(label.label_id for label in self.evaluator.labels)
        for review in self.history.reviews:
            _references([review.draft_id], drafts)
        for label in self.evaluator.labels:
            _references([label.draft_id], drafts)
        for role in (
            self.generator.writer,
            self.generator.reviewer,
            self.generator.revision,
        ):
            if role.input is not None:
                _references(role.input.evidence_ids, evidence)
                _references(role.input.draft_ids, drafts)
                _references(role.input.review_ids, reviews)
        return self


class Suite(Identified):
    suite_id: Identifier
    purpose: Literal["synthetic_infrastructure", "evaluation"]
    rights: Rights
    cases: FileReference
