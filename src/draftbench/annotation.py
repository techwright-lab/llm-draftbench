"""Private blind-review custody and immutable annotation/adjudication imports."""

import hashlib
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from .annotation_export import build_export
from .annotation_models import (
    AdjudicationBatch,
    AnnotationError,
    AnnotationManifest,
    AnnotationRubric,
    BlindPacket,
    RaterRoster,
    ResponseBatch,
)
from .annotation_store import AnnotationStore
from .identity import canonical_bytes, identity, strict_json_loads
from .loader import _Reader
from .models import Contract, Digest, FileReference, Identifier
from .scoring.models import ScoringBundle, required_check_ids, span_matches
from .scoring.reporting import load_scoring_bundle
from .store import ArtifactStore
from .workflow import _directory, _fsync, _run_lock

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


class _Pointer(Contract):
    format: Literal["draftbench-annotation-pointer-v1"]
    session_id: Identifier
    manifest_digest: Digest


def _load(path, model):
    path = Path(path).absolute()
    data = _Reader(path.parent.resolve()).read(path)
    return model.model_validate(strict_json_loads(data.decode("utf-8")))


def _write_new(path: Path, data: bytes):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync(path.parent)


def create_annotation_session(bundle_path, rubric_path, roster_path, output) -> dict:
    bundle = load_scoring_bundle(bundle_path)
    rubric = _load(rubric_path, AnnotationRubric)
    roster = _load(roster_path, RaterRoster)
    manifest, objects, files = build_export(bundle, rubric, roster)
    manifest_bytes = canonical_bytes(manifest.model_dump(mode="json"))
    expected_files = {
        path
        for assignment in manifest.assignments
        for path in (assignment.packet_path, assignment.template_path)
    }
    if set(files) != expected_files:
        raise AnnotationError("annotation_invalid")
    all_data = [*objects.values(), *files.values(), manifest_bytes, manifest_bytes]
    if (
        any(len(data) > MAX_FILE_BYTES for data in all_data)
        or sum(map(len, all_data)) > MAX_TOTAL_BYTES
    ):
        raise AnnotationError("annotation_limit")
    root = _directory(output, create=True)
    with _run_lock(root, create=True):
        store = ArtifactStore(root / "objects", create=True)
        for digest, data in objects.items():
            if store.put(data) != digest:
                raise AnnotationError("annotation_binding")
        digest = store.put(manifest_bytes)
        (root / "packets").mkdir(mode=0o700)
        (root / "packets").chmod(0o700)
        for name, data in files.items():
            reference = FileReference(
                path=name, sha256=hashlib.sha256(data).hexdigest()
            )
            if (
                len(Path(reference.path).parts) != 2
                or Path(reference.path).parts[0] != "packets"
            ):
                raise AnnotationError("annotation_path")
            _write_new(root / reference.path, data)
        pointer = _Pointer(
            format="draftbench-annotation-pointer-v1",
            session_id=manifest.session_id,
            manifest_digest=digest,
        )
        _write_new(root / "custody.json", manifest_bytes)
        _write_new(
            root / "session.json", canonical_bytes(pointer.model_dump(mode="json"))
        )
        with AnnotationStore.create(
            root / "annotations.sqlite3", manifest.session_id, digest
        ):
            _fsync(root)
    return annotation_status(root)


def _verify_task(task, mapping, case, rubric):
    if mapping.case_identity != case.identity:
        raise AnnotationError("annotation_binding")
    unit_ids = {unit.unit_id for unit in task.artifact.units}
    if set(mapping.unit_ids) != unit_ids or len(set(mapping.unit_ids.values())) != len(
        unit_ids
    ):
        raise AnnotationError("annotation_binding")
    units = [
        {**unit.model_dump(mode="json"), "unit_id": mapping.unit_ids[unit.unit_id]}
        for unit in task.artifact.units
    ]
    if units != [unit.model_dump(mode="json") for unit in case.artifact.units]:
        raise AnnotationError("annotation_binding")
    source_ids = {source.source_id for source in task.sources}
    if set(mapping.source_ids) != source_ids or len(
        set(mapping.source_ids.values())
    ) != len(source_ids):
        raise AnnotationError("annotation_binding")
    sources = {
        mapping.source_ids[source.source_id]: (source.text, source.sha256)
        for source in task.sources
    }
    if (
        sources
        != {source.source_id: (source.text, source.sha256) for source in case.sources}
        or task.sources_state != case.sources_state
    ):
        raise AnnotationError("annotation_binding")
    original_checks = {check.check_id: check for check in case.rubric.checks}
    questions = {
        criterion.criterion_id: criterion.question for criterion in rubric.criteria
    }
    if set(mapping.criterion_ids) != {
        criterion.criterion_id for criterion in task.criteria
    } or set(mapping.criterion_ids.values()) != set(original_checks):
        raise AnnotationError("annotation_binding")
    if (
        len(mapping.criterion_ids) != len(original_checks)
        or task.instructions != rubric.instructions
    ):
        raise AnnotationError("annotation_binding")
    required = required_check_ids(case.rubric)
    unit_aliases = {original: alias for alias, original in mapping.unit_ids.items()}
    for criterion in task.criteria:
        original_id = mapping.criterion_ids[criterion.criterion_id]
        check = original_checks[original_id]
        applicability = check.applicability
        if applicability == "required" and original_id not in required:
            applicability = "inapplicable"
        expected = {
            "kind": check.kind,
            "applicability": applicability,
            "question": questions[original_id],
            "unit_id": unit_aliases.get(getattr(check, "unit_id", None)),
            "min_chars": getattr(check, "min_chars", None),
            "max_chars": getattr(check, "max_chars", None),
            "literal": getattr(check, "literal", None),
        }
        if any(getattr(criterion, key) != value for key, value in expected.items()):
            raise AnnotationError("annotation_binding")
    if task.rubric_digest != identity(
        {
            "instructions": task.instructions,
            "criteria": [
                criterion.model_dump(mode="json") for criterion in task.criteria
            ],
        }
    ):
        raise AnnotationError("annotation_binding")


@contextmanager
def _session(directory, *, readonly=False):
    root = _directory(directory)
    with _run_lock(root):
        pointer = _load(root / "session.json", _Pointer)
        objects = ArtifactStore(root / "objects")
        manifest = AnnotationManifest.model_validate(
            objects.get_json(pointer.manifest_digest)
        )
        if pointer.session_id != manifest.session_id:
            raise AnnotationError("annotation_binding")
        bundle = ScoringBundle.model_validate(objects.get_json(manifest.source_digest))
        rubric = AnnotationRubric.model_validate(
            objects.get_json(manifest.rubric_digest)
        )
        roster = RaterRoster.model_validate(objects.get_json(manifest.roster_digest))
        cases = {case.case_id: case for case in bundle.cases}
        raters = {rater.rater_key: rater for rater in roster.raters}
        if {assignment.rater_key for assignment in manifest.assignments} != set(
            raters
        ) or len(manifest.assignments) != len(raters):
            raise AnnotationError("annotation_binding")
        packets = {}
        mappings = {}
        reader = _Reader(root)
        reader.reference(
            FileReference(path="custody.json", sha256=pointer.manifest_digest)
        )
        for assignment in manifest.assignments:
            packet = BlindPacket.model_validate(
                objects.get_json(assignment.packet_digest)
            )
            reader.reference(
                FileReference(
                    path=assignment.packet_path, sha256=assignment.packet_digest
                )
            )
            template = ResponseBatch.model_validate(
                objects.get_json(assignment.template_digest)
            )
            if (
                packet.session_id,
                packet.packet_id,
                packet.rater_id,
                packet.identity,
            ) != (
                manifest.session_id,
                assignment.packet_id,
                assignment.rater_id,
                assignment.packet_identity,
            ):
                raise AnnotationError("annotation_binding")
            if (
                template.session_id,
                template.packet_id,
                template.rater_id,
                template.packet_identity,
            ) != (
                packet.session_id,
                packet.packet_id,
                packet.rater_id,
                packet.identity,
            ):
                raise AnnotationError("annotation_binding")
            order = [task.task_id for task in packet.tasks]
            if template.task_order != order or any(
                answer.status != "unanswered" for answer in template.answers
            ):
                raise AnnotationError("annotation_binding")
            task_maps = {
                mapping.task_id: mapping for mapping in assignment.task_mappings
            }
            if (
                set(task_maps) != set(order)
                or len(task_maps) != len(cases)
                or {mapping.case_id for mapping in task_maps.values()} != set(cases)
            ):
                raise AnnotationError("annotation_binding")
            for position, task in enumerate(packet.tasks):
                if task.position != position:
                    raise AnnotationError("annotation_binding")
                mapping = task_maps[task.task_id]
                case = cases[mapping.case_id]
                if (case.rubric.rubric_id, case.rubric.version) != (
                    rubric.rubric_id,
                    rubric.rubric_version,
                ):
                    raise AnnotationError("annotation_binding")
                _verify_task(task, mapping, case, rubric)
                mappings[task.task_id] = (mapping, case)
            packets[packet.packet_id] = (assignment, packet)
        with AnnotationStore.open(
            root / "annotations.sqlite3", readonly=readonly
        ) as records:
            if (records.session_id, records.manifest_digest) != (
                manifest.session_id,
                pointer.manifest_digest,
            ):
                raise AnnotationError("annotation_binding")
            yield root, manifest, bundle, rubric, raters, packets, mappings, records


def _normalize(answer, task, mapping, case, rubric, rater, batch, *, synthetic):
    if (answer.task_identity, answer.artifact_sha256, answer.rubric_digest) != (
        task.identity,
        task.artifact.sha256,
        task.rubric_digest,
    ):
        raise AnnotationError("annotation_binding")
    labels = {label.check_id: label.state for label in answer.criteria}
    if set(labels) - set(mapping.criterion_ids):
        raise AnnotationError("annotation_binding")
    texts = {unit.unit_id: unit.content for unit in task.artifact.units}
    defects = []
    for defect in answer.defects:
        value = defect.model_dump(mode="json")
        if defect.span is not None:
            if not span_matches(defect.span, texts):
                raise AnnotationError("annotation_binding")
            value["span"]["item_id"] = mapping.unit_ids[defect.span.item_id]
        defects.append(value)
    required = {
        criterion.criterion_id
        for criterion in task.criteria
        if criterion.applicability == "required"
    }
    unresolved = sorted(
        required - {key for key, state in labels.items() if state in {"pass", "fail"}}
    )
    missing = sorted(set(mapping.criterion_ids) - set(labels))
    return {
        "session_id": batch.session_id,
        "packet_id": batch.packet_id,
        "rater_id": batch.rater_id,
        "rater_key": rater.rater_key,
        "case_id": case.case_id,
        "case_identity": case.identity,
        "source_family": case.source_family,
        "artifact_sha256": case.artifact.sha256,
        "rubric_id": case.rubric.rubric_id,
        "rubric_version": case.rubric.version,
        "annotation_rubric_identity": rubric.identity,
        "presentation_position": task.position,
        "unit_order": [unit.unit_id for unit in case.artifact.units],
        "producer": rater.producer.model_dump(mode="json"),
        "basis": "synthetic" if synthetic else "historical_exact",
        "independence": rater.independence,
        "rated_at": answer.rated_at,
        "decision": answer.decision,
        "criteria": [
            {"check_id": mapping.criterion_ids[label.check_id], "state": label.state}
            for label in answer.criteria
        ],
        "defect_inventory": answer.defect_inventory,
        "defects": defects,
        "notes": answer.notes,
        "coverage": {
            "required_count": len(required),
            "answered_required_count": len(required) - len(unresolved),
            "missing_criteria": [mapping.criterion_ids[key] for key in missing],
            "unresolved_required": [mapping.criterion_ids[key] for key in unresolved],
            "complete": answer.decision != "abstain"
            and not unresolved
            and answer.defect_inventory == "complete",
        },
        "original_response": answer.model_dump(mode="json"),
        "based_on": list(getattr(answer, "based_on", [])),
    }


def import_annotation_responses(
    directory, response_path, *, adjudication=False
) -> dict:
    if type(adjudication) is not bool:
        raise AnnotationError("annotation_invalid")
    batch = _load(response_path, AdjudicationBatch if adjudication else ResponseBatch)
    with _session(directory) as context:
        _, manifest, bundle, rubric, raters, packets, mappings, store = context
        if batch.session_id != manifest.session_id or batch.packet_id not in packets:
            raise AnnotationError("annotation_binding")
        assignment, packet = packets[batch.packet_id]
        if (batch.rater_id, batch.packet_identity, batch.task_order) != (
            packet.rater_id,
            packet.identity,
            [task.task_id for task in packet.tasks],
        ):
            raise AnnotationError("annotation_binding")
        tasks = {task.task_id: task for task in packet.tasks}
        existing = {record["record_id"]: record for record in store.records()}
        pending = []
        unanswered = 0
        for answer in batch.answers:
            if answer.task_id not in tasks:
                raise AnnotationError("annotation_binding")
            task = tasks[answer.task_id]
            mapping, case = mappings[answer.task_id]
            body = _normalize(
                answer,
                task,
                mapping,
                case,
                rubric,
                raters[assignment.rater_key],
                batch,
                synthetic=bundle.purpose == "synthetic_infrastructure",
            )
            if answer.status == "unanswered":
                unanswered += 1
                continue
            if adjudication:
                for identifier in body["based_on"]:
                    original = existing.get(identifier)
                    if original is None or original["kind"] != "original":
                        raise AnnotationError("annotation_unknown_record")
                    if (
                        original["body"]["case_identity"],
                        original["body"]["annotation_rubric_identity"],
                    ) != (case.identity, rubric.identity):
                        raise AnnotationError("annotation_binding")
                all_originals = {
                    identifier
                    for identifier, record in existing.items()
                    if record["kind"] == "original"
                    and record["body"]["case_identity"] == case.identity
                }
                if (
                    answer.response_id not in existing
                    and set(body["based_on"]) != all_originals
                ):
                    raise AnnotationError("annotation_binding")
            pending.append(
                {
                    "record_id": answer.response_id,
                    "kind": "adjudication" if adjudication else "original",
                    "task_id": answer.task_id,
                    "body": body,
                }
            )
        result = store.append(pending)
        return {
            "operation": "adjudication_import" if adjudication else "annotation_import",
            **result,
            "unanswered": unanswered,
            "submitted_answers": len(batch.answers),
            "model_execution_performed": False,
        }


def annotation_status(directory) -> dict:
    with _session(directory) as context:
        _, manifest, bundle, _, _, _, _, store = context
        records = store.records()
        originals = [record for record in records if record["kind"] == "original"]
        adjudications = [
            record for record in records if record["kind"] == "adjudication"
        ]
        assignments = sum(
            len(assignment.task_mappings) for assignment in manifest.assignments
        )
        received_tasks = {record["task_id"] for record in originals}
        complete_tasks = {
            record["task_id"]
            for record in originals
            if record["body"]["coverage"]["complete"]
        }
        grouped = defaultdict(list)
        for record in originals:
            grouped[record["body"]["case_identity"]].append(record)
        disputes = {
            case
            for case, rows in grouped.items()
            if {row["body"]["decision"] for row in rows}
            >= {"acceptable", "unacceptable"}
        }
        resolved = {
            case
            for case in disputes
            if any(
                record["body"]["case_identity"] == case
                and set(record["body"]["based_on"])
                == {row["record_id"] for row in grouped[case]}
                and record["body"]["coverage"]["complete"]
                for record in adjudications
            )
        }
        return {
            "operation": "annotation_status",
            "packet_count": len(manifest.assignments),
            "case_count": len(bundle.cases),
            "assignment_count": assignments,
            "original_records": len(originals),
            "adjudication_records": len(adjudications),
            "received_assignments": len(received_tasks),
            "complete_assignments": len(complete_tasks),
            "pending_assignments": assignments - len(received_tasks),
            "incomplete_assignments": len(received_tasks - complete_tasks),
            "assignments_with_multiple_originals": sum(
                count > 1
                for count in Counter(record["task_id"] for record in originals).values()
            ),
            "decision_disagreement_cases": len(disputes),
            "unresolved_disagreement_cases": len(disputes - resolved),
            "model_execution_performed": False,
            "gold_verification_performed": False,
        }


def export_annotation_records(directory, output, *, kind=None) -> dict:
    with _session(directory) as context:
        root, manifest, _, _, _, _, _, store = context
        records = store.records(kind=kind)
        data = canonical_bytes(
            {
                "format": "draftbench-private-annotation-records-v1",
                "session_id": manifest.session_id,
                "records": records,
            }
        )
        if len(data) > MAX_TOTAL_BYTES:
            raise AnnotationError("annotation_limit")
        path = Path(output).absolute()
        parent = path.parent.resolve(strict=True)
        if parent.is_relative_to(root / "packets"):
            raise AnnotationError("annotation_path")
        for ancestor in (parent, *parent.parents):
            if (ancestor / ".git").exists() or (ancestor / ".git").is_symlink():
                raise AnnotationError("annotation_path")
        _write_new(parent / path.name, data)
        return {
            "operation": "annotation_records_export",
            "record_count": len(records),
            "private": True,
            "model_execution_performed": False,
        }
