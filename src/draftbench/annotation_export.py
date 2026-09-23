"""Pure, allowlisted blind packet construction. No paths or sidecars are read."""

import hashlib
from random import SystemRandom
from secrets import token_hex
from typing import TypeVar

from .annotation_models import (
    AnnotationError,
    AnnotationManifest,
    AnnotationRubric,
    Answer,
    Assignment,
    BlindCriterion,
    BlindPacket,
    BlindTask,
    RaterRoster,
    ResponseBatch,
    TaskMapping,
    utc_now,
)
from .identity import canonical_bytes, identity
from .models import Contract, Identified
from .scoring.models import (
    LengthCheck,
    LiteralCheck,
    ScoreCase,
    ScoringBundle,
    artifact_digest,
    required_check_ids,
)

MAX_CASES = 256
MAX_RATERS = 16
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
Model = TypeVar("Model", bound=Contract)
Sealed = TypeVar("Sealed", bound=Identified)


def _snapshot(model: type[Model], value: Model) -> Model:
    if not isinstance(value, model):
        raise AnnotationError("annotation_invalid")
    return model.model_validate(value.model_dump(mode="python", warnings="error"))


def _seal(model: type[Sealed], data: dict) -> Sealed:
    data = {"schema_version": "1", **data}
    data["identity"] = identity(data)
    return model.model_validate(data)


def _opaque(prefix: str) -> str:
    return f"{prefix}-{token_hex(16)}"


def _task(
    case: ScoreCase, rubric: AnnotationRubric, position: int
) -> tuple[BlindTask, TaskMapping]:
    task_id = _opaque("task")
    unit_ids = {unit.unit_id: _opaque("unit") for unit in case.artifact.units}
    source_ids = {source.source_id: _opaque("source") for source in case.sources}
    criterion_ids = {
        check.check_id: _opaque("criterion") for check in case.rubric.checks
    }
    questions = {row.criterion_id: row.question for row in rubric.criteria}
    required = required_check_ids(case.rubric)
    criteria = []
    for check in case.rubric.checks:
        applicability = check.applicability
        if applicability == "required" and check.check_id not in required:
            applicability = "inapplicable"
        data = {
            "criterion_id": criterion_ids[check.check_id],
            "question": questions[check.check_id],
            "applicability": applicability,
            "kind": check.kind,
            "unit_id": None,
            "min_chars": None,
            "max_chars": None,
            "literal": None,
        }
        if isinstance(check, (LengthCheck, LiteralCheck)):
            if check.unit_id is not None:
                if check.unit_id not in unit_ids:
                    raise AnnotationError("annotation_binding")
                data["unit_id"] = unit_ids[check.unit_id]
            if isinstance(check, LengthCheck):
                data.update(min_chars=check.min_chars, max_chars=check.max_chars)
            else:
                data["literal"] = check.literal
        # Fact/citation annotations are private evaluator observations, not prompts.
        criteria.append(BlindCriterion.model_validate(data).model_dump(mode="json"))
    units = [
        {"unit_id": unit_ids[unit.unit_id], "kind": unit.kind, "content": unit.content}
        for unit in case.artifact.units
    ]
    sources = [
        {
            "source_id": source_ids[source.source_id],
            "text": source.text,
            "sha256": source.sha256,
        }
        for source in case.sources
    ]
    task = _seal(
        BlindTask,
        {
            "task_id": task_id,
            "position": position,
            "artifact": {"sha256": artifact_digest(units), "units": units},
            "rubric_digest": identity(
                {"instructions": rubric.instructions, "criteria": criteria}
            ),
            "instructions": rubric.instructions,
            "criteria": criteria,
            "sources_state": case.sources_state,
            "sources": sources,
        },
    )
    mapping = TaskMapping(
        task_id=task_id,
        case_id=case.case_id,
        case_identity=case.identity,
        unit_ids={public: original for original, public in unit_ids.items()},
        source_ids={public: original for original, public in source_ids.items()},
        criterion_ids={public: original for original, public in criterion_ids.items()},
    )
    return task, mapping


def _build_export(
    bundle: ScoringBundle, rubric: AnnotationRubric, roster: RaterRoster
) -> tuple[AnnotationManifest, dict[str, bytes], dict[str, bytes]]:
    bundle = _snapshot(ScoringBundle, bundle)
    rubric = _snapshot(AnnotationRubric, rubric)
    roster = _snapshot(RaterRoster, roster)
    if len(bundle.cases) > MAX_CASES or len(roster.raters) > MAX_RATERS:
        raise AnnotationError("annotation_limit")
    mandate_ids = {row.criterion_id for row in rubric.criteria}
    for case in bundle.cases:
        if (
            case.rubric.rubric_id != rubric.rubric_id
            or case.rubric.version != rubric.rubric_version
            or not {check.check_id for check in case.rubric.checks} <= mandate_ids
        ):
            raise AnnotationError("annotation_binding")

    objects: dict[str, bytes] = {}
    files: dict[str, bytes] = {}
    total = 0

    def reserve(data: bytes) -> None:
        nonlocal total
        if len(data) > MAX_OBJECT_BYTES:
            raise AnnotationError("annotation_limit")
        total += len(data)
        if total > MAX_EXPORT_BYTES:
            raise AnnotationError("annotation_limit")

    def put_object(value: Contract) -> str:
        data = canonical_bytes(value.model_dump(mode="json"))
        digest = hashlib.sha256(data).hexdigest()
        if digest not in objects:
            reserve(data)
            objects[digest] = data
        elif objects[digest] != data:
            raise AnnotationError("annotation_conflict")
        return digest

    def put_file(path: str, digest: str) -> None:
        if path in files:
            raise AnnotationError("annotation_conflict")
        reserve(objects[digest])
        files[path] = objects[digest]

    source_digest = put_object(bundle)
    rubric_digest = put_object(rubric)
    roster_digest = put_object(roster)
    session_id = _opaque("session")
    created_at = utc_now()
    assignments = []
    random = SystemRandom()
    for rater in roster.raters:
        rater_id = _opaque("rater")
        packet_id = _opaque("packet")
        cases = list(bundle.cases)
        random.shuffle(cases)
        tasks, mappings = [], []
        task_bytes = 0
        for position, case in enumerate(cases):
            task, mapping = _task(case, rubric, position)
            task_bytes += len(canonical_bytes(task.model_dump(mode="json")))
            if (
                task_bytes > MAX_OBJECT_BYTES
                or total + 2 * task_bytes > MAX_EXPORT_BYTES
            ):
                raise AnnotationError("annotation_limit")
            tasks.append(task)
            mappings.append(mapping)
        packet = _seal(
            BlindPacket,
            {
                "format": "draftbench-blind-packet-v1",
                "session_id": session_id,
                "packet_id": packet_id,
                "rater_id": rater_id,
                "issued_at": utc_now(),
                "tasks": [task.model_dump(mode="json") for task in tasks],
            },
        )
        template = ResponseBatch(
            format="draftbench-annotation-responses-v1",
            session_id=session_id,
            packet_id=packet_id,
            rater_id=rater_id,
            packet_identity=packet.identity,
            task_order=[task.task_id for task in tasks],
            answers=[
                Answer(
                    response_id=_opaque("response"),
                    task_id=task.task_id,
                    task_identity=task.identity,
                    artifact_sha256=task.artifact.sha256,
                    rubric_digest=task.rubric_digest,
                    status="unanswered",
                    rated_at=None,
                    decision=None,
                    criteria=[],
                    defect_inventory="unavailable",
                    defects=[],
                    notes="",
                )
                for task in tasks
            ],
        )
        packet_digest = put_object(packet)
        template_digest = put_object(template)
        packet_path = f"packets/{rater_id}.json"
        template_path = f"packets/{rater_id}.responses.json"
        put_file(packet_path, packet_digest)
        put_file(template_path, template_digest)
        assignments.append(
            Assignment(
                rater_key=rater.rater_key,
                rater_id=rater_id,
                packet_id=packet_id,
                packet_identity=packet.identity,
                packet_digest=packet_digest,
                template_digest=template_digest,
                packet_path=packet_path,
                template_path=template_path,
                task_mappings=mappings,
            )
        )
    manifest = _seal(
        AnnotationManifest,
        {
            "format": "draftbench-annotation-session-v1",
            "session_id": session_id,
            "created_at": created_at,
            "source_digest": source_digest,
            "rubric_digest": rubric_digest,
            "roster_digest": roster_digest,
            "assignments": [row.model_dump(mode="json") for row in assignments],
        },
    )
    # Include the parent's private manifest object and manifest file in the budget.
    manifest_bytes = canonical_bytes(manifest.model_dump(mode="json"))
    reserve(manifest_bytes)
    reserve(manifest_bytes)
    return manifest, objects, files


def build_export(
    bundle: ScoringBundle, rubric: AnnotationRubric, roster: RaterRoster
) -> tuple[AnnotationManifest, dict[str, bytes], dict[str, bytes]]:
    """Return private manifest, content-addressed objects and public packet files.

    Only `files` is a public projection. Objects include the complete private
    scoring bundle and rater roster; never publish that dictionary. Byte limits
    cover objects plus file copies, including the caller-persisted manifest.
    """
    try:
        return _build_export(bundle, rubric, roster)
    except AnnotationError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AnnotationError("annotation_invalid") from None
