"""Strict annotation contracts; private custody and blind packets stay separate."""

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from .identity import identity
from .models import (
    Contract,
    Digest,
    FileReference,
    Identified,
    Identifier,
    Producer,
    RelativePath,
    ShortText,
    Text,
)
from .scoring.models import (
    Applicability,
    EvidenceSource,
    Finding,
    Independence,
    Index,
    ReviewCheck,
    ScoringArtifact,
)

_ERROR_CODES = frozenset(
    {
        "annotation_invalid",
        "annotation_limit",
        "annotation_binding",
        "annotation_conflict",
        "annotation_path",
        "annotation_exists",
        "annotation_io",
        "annotation_unknown_record",
    }
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


class AnnotationError(ValueError):
    """An allowlisted code, never an input value, path or underlying error."""

    def __init__(self, code: str):
        self.code = (
            code if type(code) is str and code in _ERROR_CODES else "annotation_invalid"
        )
        super().__init__(self.code)


def validate_timestamp(value: str) -> str:
    """Validate a timezone-aware ISO 8601 timestamp without normalizing its text."""
    if type(value) is not str or len(value) > 128 or not _TIMESTAMP.fullmatch(value):
        raise AnnotationError("annotation_invalid")
    try:
        if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, OverflowError):
        raise AnnotationError("annotation_invalid") from None
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


Timestamp = Annotated[
    str, Field(min_length=1, max_length=128), AfterValidator(validate_timestamp)
]
InventoryState = Literal["complete", "partial", "unavailable"]


def _unique(values) -> set:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError("annotation_invalid")
    return set(items)


class RubricCriterion(Contract):
    criterion_id: Identifier
    question: Text


class AnnotationRubric(Identified):
    format: Literal["draftbench-annotation-rubric-v1"]
    rubric_id: Identifier
    rubric_version: ShortText
    instructions: Text
    criteria: Annotated[list[RubricCriterion], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def identifiers(self):
        _unique(row.criterion_id for row in self.criteria)
        return self


class RaterSpec(Contract):
    rater_key: Identifier
    producer: Producer
    independence: Independence


class RaterRoster(Identified):
    format: Literal["draftbench-raters-v1"]
    raters: Annotated[list[RaterSpec], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def identifiers(self):
        _unique(row.rater_key for row in self.raters)
        return self


class BlindCriterion(Contract):
    criterion_id: Identifier
    question: Text
    applicability: Applicability
    kind: Literal[
        "nonempty",
        "length",
        "required_literal",
        "forbidden_literal",
        "citation",
        "fact",
    ]
    unit_id: Identifier | None
    min_chars: Index | None
    max_chars: Index | None
    literal: Text | None

    @model_validator(mode="after")
    def parameters(self):
        if self.kind == "length":
            if self.literal is not None or (
                self.min_chars is None and self.max_chars is None
            ):
                raise ValueError("annotation_invalid")
            if (
                self.min_chars is not None
                and self.max_chars is not None
                and self.min_chars > self.max_chars
            ):
                raise ValueError("annotation_invalid")
        elif self.kind in {"required_literal", "forbidden_literal"}:
            if (
                self.literal is None
                or self.min_chars is not None
                or self.max_chars is not None
            ):
                raise ValueError("annotation_invalid")
        elif any(
            value is not None
            for value in (self.unit_id, self.min_chars, self.max_chars, self.literal)
        ):
            raise ValueError("annotation_invalid")
        return self


class BlindTask(Identified):
    task_id: Identifier
    position: Annotated[int, Field(ge=0, le=255)]
    artifact: ScoringArtifact
    rubric_digest: Digest
    instructions: Text
    criteria: Annotated[list[BlindCriterion], Field(max_length=256)]
    sources_state: InventoryState
    sources: Annotated[list[EvidenceSource], Field(max_length=256)]

    @model_validator(mode="after")
    def bindings(self):
        units = {unit.unit_id for unit in self.artifact.units}
        _unique(row.criterion_id for row in self.criteria)
        _unique(row.source_id for row in self.sources)
        if self.sources_state == "unavailable" and self.sources:
            raise ValueError("annotation_invalid")
        if any(
            row.unit_id is not None and row.unit_id not in units
            for row in self.criteria
        ):
            raise ValueError("annotation_binding")
        if self.rubric_digest != identity(
            {
                "instructions": self.instructions,
                "criteria": [row.model_dump(mode="json") for row in self.criteria],
            }
        ):
            raise ValueError("annotation_binding")
        return self


class BlindPacket(Identified):
    format: Literal["draftbench-blind-packet-v1"]
    session_id: Identifier
    packet_id: Identifier
    rater_id: Identifier
    issued_at: Timestamp
    tasks: Annotated[list[BlindTask], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def order(self):
        _unique(task.task_id for task in self.tasks)
        if [task.position for task in self.tasks] != list(range(len(self.tasks))):
            raise ValueError("annotation_binding")
        _unique(unit.unit_id for task in self.tasks for unit in task.artifact.units)
        _unique(source.source_id for task in self.tasks for source in task.sources)
        _unique(row.criterion_id for task in self.tasks for row in task.criteria)
        return self


class Answer(Contract):
    response_id: Identifier
    task_id: Identifier
    task_identity: Digest
    artifact_sha256: Digest
    rubric_digest: Digest
    status: Literal["answered", "unanswered"]
    rated_at: Timestamp | None
    decision: Literal["acceptable", "unacceptable", "abstain"] | None
    criteria: Annotated[list[ReviewCheck], Field(max_length=256)]
    defect_inventory: InventoryState
    defects: Annotated[list[Finding], Field(max_length=512)]
    notes: Annotated[str, Field(max_length=100_000)]

    @model_validator(mode="after")
    def response(self):
        _unique(row.check_id for row in self.criteria)
        _unique(row.finding_id for row in self.defects)
        if any(row.kind != "defect" for row in self.defects):
            raise ValueError("annotation_invalid")
        if self.defect_inventory == "unavailable" and self.defects:
            raise ValueError("annotation_invalid")
        if self.status == "unanswered":
            if (
                self.rated_at is not None
                or self.decision is not None
                or self.criteria
                or self.defects
                or self.notes
                or self.defect_inventory != "unavailable"
            ):
                raise ValueError("annotation_invalid")
        elif self.rated_at is None or self.decision is None:
            raise ValueError("annotation_invalid")
        return self


class ResponseBatch(Contract):
    format: Literal["draftbench-annotation-responses-v1"]
    session_id: Identifier
    packet_id: Identifier
    rater_id: Identifier
    packet_identity: Digest
    task_order: Annotated[list[Identifier], Field(min_length=1, max_length=256)]
    answers: Annotated[list[Answer], Field(max_length=256)]

    @model_validator(mode="after")
    def identifiers(self):
        order = _unique(self.task_order)
        tasks = _unique(row.task_id for row in self.answers)
        _unique(row.response_id for row in self.answers)
        if not tasks <= order:
            raise ValueError("annotation_binding")
        return self


class AdjudicationAnswer(Answer):
    based_on: Annotated[list[Identifier], Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def original_records(self):
        _unique(self.based_on)
        if self.status != "answered":
            raise ValueError("annotation_invalid")
        return self


class AdjudicationBatch(Contract):
    format: Literal["draftbench-adjudication-responses-v1"]
    session_id: Identifier
    packet_id: Identifier
    rater_id: Identifier
    packet_identity: Digest
    task_order: Annotated[list[Identifier], Field(min_length=1, max_length=256)]
    answers: Annotated[list[AdjudicationAnswer], Field(max_length=256)]

    @model_validator(mode="after")
    def identifiers(self):
        order = _unique(self.task_order)
        tasks = _unique(row.task_id for row in self.answers)
        _unique(row.response_id for row in self.answers)
        if not tasks <= order:
            raise ValueError("annotation_binding")
        return self


class TaskMapping(Contract):
    task_id: Identifier
    case_id: Identifier
    case_identity: Digest
    unit_ids: Annotated[dict[Identifier, Identifier], Field(max_length=256)]
    source_ids: Annotated[dict[Identifier, Identifier], Field(max_length=256)]
    criterion_ids: Annotated[dict[Identifier, Identifier], Field(max_length=256)]

    @model_validator(mode="after")
    def bijections(self):
        for mapping in (self.unit_ids, self.source_ids, self.criterion_ids):
            _unique(mapping.values())
        return self


class Assignment(Contract):
    rater_key: Identifier
    rater_id: Identifier
    packet_id: Identifier
    packet_identity: Digest
    packet_digest: Digest
    template_digest: Digest
    packet_path: RelativePath
    template_path: RelativePath
    task_mappings: Annotated[list[TaskMapping], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def bindings(self):
        _unique(row.task_id for row in self.task_mappings)
        _unique(row.case_id for row in self.task_mappings)
        if (
            self.packet_path != f"packets/{self.rater_id}.json"
            or self.template_path != f"packets/{self.rater_id}.responses.json"
        ):
            raise ValueError("annotation_path")
        FileReference(path=self.packet_path, sha256=self.packet_digest)
        FileReference(path=self.template_path, sha256=self.template_digest)
        return self


class AnnotationManifest(Identified):
    format: Literal["draftbench-annotation-session-v1"]
    session_id: Identifier
    created_at: Timestamp
    source_digest: Digest
    rubric_digest: Digest
    roster_digest: Digest
    assignments: Annotated[list[Assignment], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def assignments_unique(self):
        _unique(row.rater_key for row in self.assignments)
        _unique(row.rater_id for row in self.assignments)
        _unique(row.packet_id for row in self.assignments)
        _unique(
            path
            for row in self.assignments
            for path in (row.packet_path, row.template_path)
        )
        _unique(
            mapping.task_id for row in self.assignments for mapping in row.task_mappings
        )
        expected = {
            (mapping.case_id, mapping.case_identity)
            for mapping in self.assignments[0].task_mappings
        }
        if any(
            {(mapping.case_id, mapping.case_identity) for mapping in row.task_mappings}
            != expected
            for row in self.assignments
        ):
            raise ValueError("annotation_binding")
        return self
