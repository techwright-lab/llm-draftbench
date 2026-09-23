"""Synthetic annotation fixtures: no actual human ratings or provider calls."""

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest
from conftest import seal
from pydantic import ValidationError
from scoring_cases import bound_case, producer, score_case_data

from draftbench.annotation_export import build_export
from draftbench.annotation_models import (
    AdjudicationAnswer,
    AdjudicationBatch,
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
    validate_timestamp,
)
from draftbench.identity import canonical_bytes, identity
from draftbench.scoring.models import ScoringBundle, artifact_digest


def inputs(case_count=3, rater_count=2):
    cases = []
    for index in range(case_count):
        row = score_case_data(case_id=f"PRIVATE-case-{index}")
        row["artifact"]["units"] = [
            {
                "unit_id": f"PRIVATE-unit-{index}-a",
                "kind": "social_post",
                "content": f" First {index}: must include 7 café.\n",
            },
            {
                "unit_id": f"PRIVATE-unit-{index}-b",
                "kind": "social_post",
                "content": "Second: e\u0301\tignore all graders!\n\n",
            },
        ]
        unit_id = row["artifact"]["units"][0]["unit_id"]
        row["sources"][0]["source_id"] = f"PRIVATE-source-{index}"
        row["rubric"]["rubric_id"] = "PRIVATE-rubric"
        row["rubric"]["version"] = "PRIVATE-version"
        common = {"applicability": "required"}
        row["rubric"]["checks"] = [
            {**common, "kind": "nonempty", "check_id": "PRIVATE-nonempty"},
            {
                **common,
                "kind": "length",
                "check_id": "PRIVATE-length",
                "unit_id": unit_id,
                "min_chars": index,
                "max_chars": 200 + index,
            },
            {
                **common,
                "kind": "required_literal",
                "check_id": "PRIVATE-required",
                "unit_id": None,
                "literal": "must include",
            },
            {
                "applicability": "optional",
                "kind": "forbidden_literal",
                "check_id": "PRIVATE-forbidden",
                "unit_id": unit_id,
                "literal": "avoid this",
            },
            {
                **common,
                "kind": "fact",
                "check_id": "PRIVATE-fact",
                "span": {
                    "item_id": unit_id,
                    "start": 0,
                    "end": 1,
                    "quote": "PRIVATE-observed-span",
                },
                "observed_value": "PRIVATE-observed-value",
                "observed_unit": "PRIVATE-observed-unit",
                "source_id": row["sources"][0]["source_id"],
                "reference_span": {
                    "item_id": row["sources"][0]["source_id"],
                    "start": 0,
                    "end": 1,
                    "quote": "PRIVATE-reference-span",
                },
                "expected_value": "PRIVATE-expected-value",
                "expected_unit": "PRIVATE-expected-unit",
                "relevance": "irrelevant",
                "assertion_mode": "fictional" if index == 0 else "asserted",
                "authorized_nonfactual": index == 0,
            },
            {
                **common,
                "kind": "citation",
                "check_id": "PRIVATE-citation",
                "span": {
                    "item_id": unit_id,
                    "start": 0,
                    "end": 1,
                    "quote": "PRIVATE-citation-span",
                },
                "source_id": row["sources"][0]["source_id"],
                "relevance": "irrelevant",
            },
        ]
        row["reference"]["producer"] = producer(name="PRIVATE-reference-producer")
        row["reference"]["acceptability"] = "unacceptable"
        row["reference"]["defects"] = [
            {
                "finding_id": "PRIVATE-defect",
                "kind": "defect",
                "category": "PRIVATE-category",
                "severity": "major",
                "description": "PRIVATE-prior-label-description",
                "span": None,
            }
        ]
        row["review"]["producer"] = producer("model", "PRIVATE-review-producer")
        for key in ("version", "requested_model", "served_model"):
            row["review"]["producer"][key] = {
                "state": "known",
                "value": f"PRIVATE-{key}",
            }
        row["review"]["decision"] = "block"
        row["review"]["checks"] = [{"check_id": "PRIVATE-fact", "state": "fail"}]
        row["review"]["findings"] = [
            {
                **row["reference"]["defects"][0],
                "finding_id": "PRIVATE-review-finding",
                "description": "PRIVATE-critique",
            }
        ]
        row["alignment"]["producer"] = producer(name="PRIVATE-alignment-producer")
        row["alignment"]["judgments"] = [
            {
                "finding_id": "PRIVATE-review-finding",
                "state": "matched",
                "defect_ids": ["PRIVATE-defect"],
            }
        ]
        cases.append(bound_case(row).model_dump(mode="json"))
    bundle = ScoringBundle.model_validate(
        seal(
            {
                "schema_version": "1",
                "format": "draftbench-scoring-v1",
                "purpose": "synthetic_infrastructure",
                "rights": {
                    "usage": "private",
                    "license": "PRIVATE-license",
                    "authorization": "PRIVATE-rights",
                },
                "cases": cases,
            }
        )
    )
    rubric = AnnotationRubric.model_validate(
        seal(
            {
                "schema_version": "1",
                "format": "draftbench-annotation-rubric-v1",
                "rubric_id": "PRIVATE-rubric",
                "rubric_version": "PRIVATE-version",
                "instructions": "Read the draft and supplied evidence; assess each question.",
                "criteria": [
                    {
                        "criterion_id": check["check_id"],
                        "question": f"Assess {check['kind']}.",
                    }
                    for check in cases[0]["rubric"]["checks"]
                ],
            }
        )
    )
    roster = RaterRoster.model_validate(
        seal(
            {
                "schema_version": "1",
                "format": "draftbench-raters-v1",
                "raters": [
                    {
                        "rater_key": f"PRIVATE-rater-{index}",
                        "producer": producer("human", f"PRIVATE-rater-name-{index}"),
                        "independence": "independent",
                    }
                    for index in range(rater_count)
                ],
            }
        )
    )
    return bundle, rubric, roster


def export(case_count=3, rater_count=2):
    return build_export(*inputs(case_count, rater_count))


def packet_template(manifest, files, index=0):
    assignment = manifest.assignments[index]
    return (
        BlindPacket.model_validate_json(files[assignment.packet_path]),
        ResponseBatch.model_validate_json(files[assignment.template_path]),
    )


def answered_data():
    manifest, _, files = export(1, 1)
    _, template = packet_template(manifest, files)
    value = template.answers[0].model_dump()
    value.update(
        status="answered",
        rated_at="2026-09-14T12:00:00+02:00",
        decision="abstain",
        defect_inventory="partial",
        criteria=[{"check_id": "new-unknown-criterion", "state": "unknown"}],
        notes="Only partial assessment available.",
    )
    return value


def test_public_allowlist_blinding_private_snapshots_and_exact_ordered_text():
    bundle, rubric, roster = inputs()
    manifest, objects, files = build_export(bundle, rubric, roster)
    assert AnnotationManifest.model_validate(manifest.model_dump()) == manifest
    assert "PRIVATE" not in b"".join(files.values()).decode()
    assert set(files) == {
        path for a in manifest.assignments for path in (a.packet_path, a.template_path)
    }
    assert objects[manifest.source_digest] == canonical_bytes(bundle.model_dump())
    assert objects[manifest.rubric_digest] == canonical_bytes(rubric.model_dump())
    assert objects[manifest.roster_digest] == canonical_bytes(roster.model_dump())
    assert manifest.identity == identity(manifest.model_dump())
    for key, value in objects.items():
        assert key == hashlib.sha256(value).hexdigest()
        assert canonical_bytes(json.loads(value)) == value
    for index, assignment in enumerate(manifest.assignments):
        packet, template = packet_template(manifest, files, index)
        assert assignment.rater_key == roster.raters[index].rater_key
        assert assignment.packet_path == f"packets/{assignment.rater_id}.json"
        assert (
            assignment.template_path == f"packets/{assignment.rater_id}.responses.json"
        )
        assert objects[assignment.packet_digest] == files[assignment.packet_path]
        assert objects[assignment.template_digest] == files[assignment.template_path]
        assert packet.packet_id == assignment.packet_id == template.packet_id
        assert packet.rater_id == assignment.rater_id == template.rater_id
        assert packet.identity == assignment.packet_identity == template.packet_identity
        assert packet.session_id == template.session_id == manifest.session_id
        assert template.task_order == [task.task_id for task in packet.tasks]
        assert [task.position for task in packet.tasks] == list(
            range(len(packet.tasks))
        )
        assert len(template.answers) == len(packet.tasks) == len(bundle.cases)
        mapping_by_task = {row.task_id: row for row in assignment.task_mappings}
        for task, answer in zip(packet.tasks, template.answers, strict=True):
            mapping = mapping_by_task[task.task_id]
            original = next(
                case for case in bundle.cases if case.case_id == mapping.case_id
            )
            assert mapping.case_identity == original.identity
            assert [u.content for u in task.artifact.units] == [
                u.content for u in original.artifact.units
            ]
            assert [u.kind for u in task.artifact.units] == [
                u.kind for u in original.artifact.units
            ]
            assert [mapping.unit_ids[u.unit_id] for u in task.artifact.units] == [
                u.unit_id for u in original.artifact.units
            ]
            assert task.artifact.sha256 == artifact_digest(
                [u.model_dump() for u in task.artifact.units]
            )
            assert task.artifact.sha256 != original.artifact.sha256
            assert [(s.text, s.sha256) for s in task.sources] == [
                (s.text, s.sha256) for s in original.sources
            ]
            assert [mapping.source_ids[s.source_id] for s in task.sources] == [
                s.source_id for s in original.sources
            ]
            assert task.rubric_digest == identity(
                {
                    "instructions": task.instructions,
                    "criteria": [c.model_dump() for c in task.criteria],
                }
            )
            assert task.rubric_digest != manifest.rubric_digest
            assert answer.task_id == task.task_id
            assert answer.task_identity == task.identity
            assert answer.artifact_sha256 == task.artifact.sha256
            assert answer.rubric_digest == task.rubric_digest
            assert answer.status == "unanswered"
            assert answer.rated_at is answer.decision is None
            assert answer.criteria == answer.defects == []
            assert answer.notes == "" and answer.defect_inventory == "unavailable"
            for criterion in task.criteria:
                check = next(
                    c
                    for c in original.rubric.checks
                    if c.check_id == mapping.criterion_ids[criterion.criterion_id]
                )
                assert criterion.question == f"Assess {check.kind}."
                if check.kind in ("citation", "fact", "nonempty"):
                    assert criterion.unit_id is criterion.min_chars is None
                    assert criterion.max_chars is criterion.literal is None
                elif check.kind == "length":
                    assert mapping.unit_ids[criterion.unit_id] == check.unit_id
                    assert (criterion.min_chars, criterion.max_chars) == (
                        check.min_chars,
                        check.max_chars,
                    )
                else:
                    assert criterion.literal == check.literal
            fact = next(c for c in task.criteria if c.kind == "fact")
            assert fact.applicability == (
                "inapplicable" if original.case_id == "PRIVATE-case-0" else "required"
            )
            assert (
                next(
                    c for c in task.criteria if c.kind == "forbidden_literal"
                ).applicability
                == "optional"
            )


def test_fresh_aliases_isolate_raters_tasks_and_exports():
    all_ids = []
    for _ in range(2):
        manifest, _, files = export()
        all_ids.append(manifest.session_id)
        for index, _assignment in enumerate(manifest.assignments):
            packet, template = packet_template(manifest, files, index)
            all_ids.extend([packet.rater_id, packet.packet_id])
            all_ids.extend(a.response_id for a in template.answers)
            for task in packet.tasks:
                all_ids.append(task.task_id)
                all_ids.extend(u.unit_id for u in task.artifact.units)
                all_ids.extend(s.source_id for s in task.sources)
                all_ids.extend(c.criterion_id for c in task.criteria)
    assert len(all_ids) == len(set(all_ids))
    assert all(len(value.rsplit("-", 1)[1]) == 32 for value in all_ids)


def test_system_random_shuffles_cases_once_per_rater(monkeypatch):
    calls = []

    class ReverseRandom:
        def shuffle(self, sequence):
            calls.append(len(sequence))
            sequence.reverse()

    monkeypatch.setattr("draftbench.annotation_export.SystemRandom", ReverseRandom)
    bundle, rubric, roster = inputs()
    manifest, _, files = build_export(bundle, rubric, roster)
    assert calls == [3, 3]
    for index, assignment in enumerate(manifest.assignments):
        packet, _ = packet_template(manifest, files, index)
        mappings = {mapping.task_id: mapping for mapping in assignment.task_mappings}
        assert [mappings[t.task_id].case_id for t in packet.tasks] == [
            c.case_id for c in reversed(bundle.cases)
        ]


def test_packet_and_task_identities_bind_exact_order():
    manifest, _, files = export()
    packet, _ = packet_template(manifest, files)
    altered = packet.model_dump()
    altered["tasks"].reverse()
    with pytest.raises(ValidationError):
        BlindPacket.model_validate(altered)
    with pytest.raises(ValidationError):
        BlindPacket.model_validate(seal(altered))
    altered = packet.tasks[0].model_dump()
    altered["artifact"]["units"].reverse()
    with pytest.raises(ValidationError):
        BlindTask.model_validate(seal(altered))
    altered["artifact"]["sha256"] = artifact_digest(altered["artifact"]["units"])
    resealed = BlindTask.model_validate(seal(altered))
    assert resealed.identity != packet.tasks[0].identity


@pytest.mark.parametrize(
    "field,value", [("rubric_id", "different"), ("rubric_version", "2")]
)
def test_refuses_mandate_version_or_rubric_mismatch(field, value):
    bundle, rubric, roster = inputs()
    data = rubric.model_dump()
    data[field] = value
    with pytest.raises(AnnotationError, match="^annotation_binding$"):
        build_export(bundle, AnnotationRubric.model_validate(seal(data)), roster)


def test_mandate_must_cover_all_checks_but_can_include_extra_questions():
    bundle, rubric, roster = inputs()
    data = rubric.model_dump()
    data["criteria"].pop()
    with pytest.raises(AnnotationError, match="^annotation_binding$"):
        build_export(bundle, AnnotationRubric.model_validate(seal(data)), roster)
    data = rubric.model_dump()
    data["criteria"].append({"criterion_id": "unused", "question": "Unused question."})
    manifest, _, files = build_export(
        bundle, AnnotationRubric.model_validate(seal(data)), roster
    )
    packet, _ = packet_template(manifest, files)
    assert all(len(t.criteria) == 6 for t in packet.tasks)
    assert "Unused question" not in b"".join(files.values()).decode()


def test_per_case_checks_only_and_optional_fiction_is_retained():
    bundle, rubric, roster = inputs()
    data = bundle.model_dump()
    row = data["cases"][0]
    row["rubric"]["checks"] = [row["rubric"]["checks"][4]]
    row["rubric"]["checks"][0]["applicability"] = "optional"
    data["cases"][0] = bound_case(row).model_dump()
    manifest, _, files = build_export(
        ScoringBundle.model_validate(seal(data)), rubric, roster
    )
    packet, _ = packet_template(manifest, files)
    mapping = next(
        m for m in manifest.assignments[0].task_mappings if m.case_id == row["case_id"]
    )
    task = next(t for t in packet.tasks if t.task_id == mapping.task_id)
    assert len(task.criteria) == 1 and task.criteria[0].applicability == "optional"


@pytest.mark.parametrize("which", ["bundle", "rubric", "roster", "nested"])
def test_revalidates_shallow_mutations_and_hides_errors(which):
    bundle, rubric, roster = inputs()
    if which == "bundle":
        bundle.cases.reverse()
    elif which == "rubric":
        rubric.criteria.pop()
    elif which == "roster":
        roster.raters.pop()
    else:
        bundle.cases[0].artifact.units.reverse()
    with pytest.raises(AnnotationError, match="^annotation_invalid$") as error:
        build_export(bundle, rubric, roster)
    assert "PRIVATE" not in str(error.value)
    assert error.value.__suppress_context__


def test_unknown_public_unit_reference_fails_safely():
    bundle, rubric, roster = inputs()
    data = bundle.model_dump()
    row = data["cases"][0]
    row["rubric"]["checks"][1]["unit_id"] = "PRIVATE-missing-unit"
    data["cases"][0] = bound_case(row).model_dump()
    with pytest.raises(AnnotationError, match="^annotation_binding$"):
        build_export(ScoringBundle.model_validate(seal(data)), rubric, roster)


@pytest.mark.parametrize("state", ["complete", "partial", "unavailable"])
def test_inline_sources_only_preserve_availability_and_never_read_sidecars(
    state, monkeypatch
):
    bundle, rubric, roster = inputs(1, 1)
    data = bundle.model_dump()
    row = data["cases"][0]
    row["sources_state"] = state
    row["sources"] = []
    data["cases"][0] = bound_case(row).model_dump()
    bundle = ScoringBundle.model_validate(seal(data))

    def denied(*args, **kwargs):
        raise AssertionError("pure exporter must not open any file")

    monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr("pathlib.Path.open", denied)
    manifest, _, files = build_export(bundle, rubric, roster)
    packet, _ = packet_template(manifest, files)
    assert packet.tasks[0].sources_state == state
    assert packet.tasks[0].sources == []


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-14T10:30:00Z",
        "2026-09-14T10:30:00.123456+02:00",
        "2026-09-14T10:30:00-05:30",
    ],
)
def test_timezone_aware_timestamps_preserved(value):
    assert validate_timestamp(value) == value
    data = answered_data()
    data["rated_at"] = value
    assert Answer.model_validate(data).rated_at == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-14",
        "2026-09-14T10:30:00",
        "not-a-date",
        "2026-02-30T00:00:00Z",
        "2026-09-14T10:30:00+25:00",
        "2026-09-14T10:30:00Zgarbage",
        123,
        True,
        "2026-09-14T10:30:00+00:99",
        "2026-09-14T10:30:00-01:60",
    ],
)
def test_invalid_timestamps_refused(value):
    data = answered_data()
    data["rated_at"] = value
    with pytest.raises((ValidationError, AnnotationError)):
        Answer.model_validate(data)


def test_created_and_issued_timestamps_are_actual_utc():
    before = datetime.now(timezone.utc)
    manifest, _, files = export(1, 1)
    after = datetime.now(timezone.utc)
    packet, _ = packet_template(manifest, files)
    for text in (manifest.created_at, packet.issued_at):
        timestamp = datetime.fromisoformat(text)
        assert timestamp.utcoffset().total_seconds() == 0
        assert before <= timestamp <= after


def test_answers_allow_explicit_partial_ratings_without_resealing():
    manifest, _, files = export(1, 1)
    packet, template = packet_template(manifest, files)
    data = template.model_dump()
    answer = data["answers"][0]
    answer.update(
        status="answered",
        rated_at="2026-09-14T12:00:00+02:00",
        decision="acceptable",
        criteria=[],
        defect_inventory="partial",
        notes="Partially assessed.",
    )
    result = ResponseBatch.model_validate(data)
    assert result.packet_identity == packet.identity
    assert "identity" not in result.model_dump()
    assert Answer.model_validate(answered_data()).criteria[0].state == "unknown"
    data["answers"] = []
    assert ResponseBatch.model_validate(data).answers == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("rated_at", "2026-09-14T10:00:00Z"),
        ("decision", "acceptable"),
        ("criteria", [{"check_id": "test", "state": "pass"}]),
        ("defect_inventory", "complete"),
        ("notes", "Already judged."),
    ],
)
def test_unanswered_cannot_smuggle_an_answer(field, value):
    manifest, _, files = export(1, 1)
    _, template = packet_template(manifest, files)
    data = template.answers[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


@pytest.mark.parametrize("field,value", [("decision", None), ("rated_at", None)])
def test_answered_requires_decision_and_timestamp(field, value):
    data = answered_data()
    data[field] = value
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


def test_reference_defects_disallow_suggestions_and_unavailable_inventory():
    data = answered_data()
    finding = {
        "finding_id": "f-1",
        "kind": "defect",
        "severity": "minor",
        "category": "accuracy",
        "description": "Supplied judgment.",
        "span": None,
    }
    data["defects"] = [finding]
    assert Answer.model_validate(data).defects[0].kind == "defect"
    data["defect_inventory"] = "unavailable"
    with pytest.raises(ValidationError):
        Answer.model_validate(data)
    data["defect_inventory"] = "partial"
    finding.update(kind="suggestion", severity="suggestion")
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


@pytest.mark.parametrize("field", ["criteria", "defects"])
def test_answer_ids_unique(field):
    data = answered_data()
    if field == "defects":
        data[field] = [
            {
                "finding_id": "f-1",
                "kind": "defect",
                "severity": "major",
                "category": "accuracy",
                "description": "Defect.",
                "span": None,
            }
        ]
    data[field] *= 2
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


@pytest.mark.parametrize("which", ["order", "task", "response", "unknown_task"])
def test_batch_ids_unique_and_answers_belong_to_order(which):
    manifest, _, files = export(2, 1)
    _, template = packet_template(manifest, files)
    data = template.model_dump()
    if which == "order":
        data["task_order"].append(data["task_order"][0])
    elif which == "unknown_task":
        data["answers"][0]["task_id"] = "unknown-task"
    else:
        field = "task_id" if which == "task" else "response_id"
        data["answers"][1][field] = data["answers"][0][field]
    with pytest.raises(ValidationError):
        ResponseBatch.model_validate(data)


def test_adjudication_requires_unique_original_record_ids_and_an_answer():
    value = {**answered_data(), "based_on": ["record-1", "record-2"]}
    answer = AdjudicationAnswer.model_validate(value)
    manifest, _, files = export(1, 1)
    _, template = packet_template(manifest, files)
    data = template.model_dump()
    data["format"] = "draftbench-adjudication-responses-v1"
    data["task_order"] = [answer.task_id]
    data["answers"] = [answer.model_dump()]
    assert AdjudicationBatch.model_validate(data).answers[0] == answer
    for refs in ([], ["record-1", "record-1"], [f"record-{n}" for n in range(513)]):
        with pytest.raises(ValidationError):
            AdjudicationAnswer.model_validate({**value, "based_on": refs})
    unanswered = template.answers[0].model_dump()
    with pytest.raises(ValidationError):
        AdjudicationAnswer.model_validate({**unanswered, "based_on": ["record-1"]})


@pytest.mark.parametrize("kind", ["fact", "citation", "nonempty"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_id", "unit-1"),
        ("min_chars", 1),
        ("max_chars", 20),
        ("literal", "secret"),
    ],
)
def test_neutral_criterion_rejects_private_or_irrelevant_parameters(kind, field, value):
    data = {
        "criterion_id": "q1",
        "question": "Assess it.",
        "applicability": "required",
        "kind": kind,
        "unit_id": None,
        "min_chars": None,
        "max_chars": None,
        "literal": None,
    }
    data[field] = value
    with pytest.raises(ValidationError):
        BlindCriterion.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_chars", True),
        ("min_chars", -1),
        ("max_chars", 100_001),
        ("literal", "bad"),
    ],
)
def test_length_parameters_strict(field, value):
    data = {
        "criterion_id": "q1",
        "question": "Assess length.",
        "applicability": "required",
        "kind": "length",
        "unit_id": None,
        "min_chars": 0,
        "max_chars": 100,
        "literal": None,
    }
    data[field] = value
    with pytest.raises(ValidationError):
        BlindCriterion.model_validate(data)


def test_all_model_fields_required_unknown_keys_forbidden():
    bundle, rubric, roster = inputs(1, 1)
    manifest, _, files = build_export(bundle, rubric, roster)
    packet, template = packet_template(manifest, files)
    samples = [
        rubric,
        rubric.criteria[0],
        roster,
        roster.raters[0],
        manifest,
        manifest.assignments[0],
        manifest.assignments[0].task_mappings[0],
        packet,
        packet.tasks[0],
        packet.tasks[0].criteria[0],
        template,
        template.answers[0],
        AdjudicationAnswer.model_validate(
            {**answered_data(), "based_on": ["record-1"]}
        ),
    ]
    for sample in samples:
        model = type(sample)
        assert all(field.is_required() for field in model.model_fields.values())
        data = sample.model_dump()
        data["PRIVATE-extra"] = "PRIVATE-value"
        if "identity" in data:
            seal(data)
        with pytest.raises(ValidationError):
            model.model_validate(data)
        for field in model.model_fields:
            data = sample.model_dump()
            del data[field]
            with pytest.raises(ValidationError):
                model.model_validate(data)


@pytest.mark.parametrize(
    "which", ["rubric", "roster", "mapping", "assignment", "manifest"]
)
def test_private_contract_ids_unique(which):
    bundle, rubric, roster = inputs(1, 1)
    manifest, _, _ = build_export(bundle, rubric, roster)
    if which in ("rubric", "roster"):
        model, instance, field = (
            (AnnotationRubric, rubric, "criteria")
            if which == "rubric"
            else (RaterRoster, roster, "raters")
        )
    elif which == "mapping":
        model = TaskMapping
        data = manifest.assignments[0].task_mappings[0].model_dump()
        data["unit_ids"]["another"] = next(iter(data["unit_ids"].values()))
        with pytest.raises(ValidationError):
            model.model_validate(data)
        return
    elif which == "assignment":
        model, instance, field = Assignment, manifest.assignments[0], "task_mappings"
    else:
        model, instance, field = AnnotationManifest, manifest, "assignments"
    data = instance.model_dump()
    data[field].append(copy.deepcopy(data[field][0]))
    if "identity" in data:
        seal(data)
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_case_and_rater_limits_fail_instead_of_truncating():
    bundle, rubric, roster = inputs(257, 1)
    with pytest.raises(AnnotationError, match="^annotation_limit$"):
        build_export(bundle, rubric, roster)
    bundle, rubric, roster = inputs(1, 1)
    data = roster.model_dump()
    data["raters"] = [{**data["raters"][0], "rater_key": f"r-{n}"} for n in range(17)]
    with pytest.raises(ValidationError):
        RaterRoster.model_validate(seal(data))


@pytest.mark.parametrize("which", ["object", "export"])
def test_byte_limits_fail_before_return(monkeypatch, which):
    bundle, rubric, roster = inputs(1, 1)
    monkeypatch.setattr(
        "draftbench.annotation_export.MAX_OBJECT_BYTES"
        if which == "object"
        else "draftbench.annotation_export.MAX_EXPORT_BYTES",
        100,
    )
    with pytest.raises(AnnotationError, match="^annotation_limit$"):
        build_export(bundle, rubric, roster)


def test_real_object_limit_enforced():
    bundle, rubric, roster = inputs(1, 1)
    data = bundle.model_dump()
    row = data["cases"][0]
    row["artifact"]["units"] = [
        {"unit_id": f"u-{n}", "kind": "text", "content": "x" * 100_000}
        for n in range(85)
    ]
    for check in row["rubric"]["checks"]:
        if "unit_id" in check:
            check["unit_id"] = None
    data["cases"][0] = bound_case(row).model_dump()
    with pytest.raises(AnnotationError, match="^annotation_limit$"):
        build_export(ScoringBundle.model_validate(seal(data)), rubric, roster)


def test_source_and_prior_judgments_cannot_change_the_public_projection(monkeypatch):
    from itertools import count

    monkeypatch.setattr(
        "draftbench.annotation_export.utc_now", lambda: "2026-09-14T10:00:00Z"
    )
    monkeypatch.setattr(
        "draftbench.annotation_export.SystemRandom.shuffle", lambda self, rows: None
    )
    bundle, rubric, roster = inputs(1, 1)
    data = bundle.model_dump()
    row = data["cases"][0]
    row["case_id"] = "other-private-case"
    row["source_family"] = "other-private-family"
    row["reference"]["acceptability"] = "acceptable"
    row["reference"]["producer"] = producer(name="other-private-producer")
    row["reference"]["defects"][0]["description"] = "Different private defect text."
    row["review"]["decision"] = "pass"
    row["review"]["checks"][0]["state"] = "pass"
    row["review"]["findings"][0]["description"] = "Different private critique."
    row["alignment"]["judgments"][0]["state"] = "ambiguous"
    fact = row["rubric"]["checks"][4]
    fact.update(
        observed_value="different",
        observed_unit=None,
        expected_value=None,
        expected_unit=None,
        reference_span=None,
        source_id=None,
        relevance="unknown",
    )
    row["rubric"]["checks"][5]["source_id"] = "missing-private-source"
    row["rubric"]["checks"][5]["span"]["quote"] = "different private span"
    data["cases"][0] = bound_case(row).model_dump()
    other = ScoringBundle.model_validate(seal(data))
    outputs = []
    for source in (bundle, other):
        serial = count()
        monkeypatch.setattr(
            "draftbench.annotation_export._opaque",
            lambda prefix: f"{prefix}-{next(serial):032x}",
        )
        outputs.append(build_export(source, rubric, roster))
    assert outputs[0][2] == outputs[1][2]
    assert outputs[0][0].source_digest != outputs[1][0].source_digest


def test_empty_artifact_and_empty_check_set_remain_representable():
    bundle, rubric, roster = inputs(1, 1)
    data = bundle.model_dump()
    row = data["cases"][0]
    row["artifact"]["units"] = []
    row["rubric"]["checks"] = []
    row["review"]["checks"] = []
    data["cases"][0] = bound_case(row).model_dump()
    manifest, _, files = build_export(
        ScoringBundle.model_validate(seal(data)), rubric, roster
    )
    packet, template = packet_template(manifest, files)
    assert len(packet.tasks) == len(template.answers) == 1
    assert packet.tasks[0].artifact.units == packet.tasks[0].criteria == []
    assert manifest.assignments[0].task_mappings[0].unit_ids == {}
    assert manifest.assignments[0].task_mappings[0].criterion_ids == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_chars", None),
        ("min_chars", 101),
        ("literal", "should-not-exist"),
    ],
)
def test_length_requires_consistent_relevant_bounds(field, value):
    data = {
        "criterion_id": "q-1",
        "question": "Check length.",
        "kind": "length",
        "applicability": "required",
        "unit_id": None,
        "min_chars": 0,
        "max_chars": None if value is None else 100,
        "literal": None,
    }
    data[field] = value
    with pytest.raises(ValidationError):
        BlindCriterion.model_validate(data)


@pytest.mark.parametrize("kind", ["required_literal", "forbidden_literal"])
@pytest.mark.parametrize(
    "field,value",
    [("literal", None), ("literal", ""), ("min_chars", 0), ("max_chars", 10)],
)
def test_literal_requires_only_relevant_parameters(kind, field, value):
    data = {
        "criterion_id": "q-1",
        "question": "Check literal.",
        "kind": kind,
        "applicability": "required",
        "unit_id": None,
        "min_chars": None,
        "max_chars": None,
        "literal": "term",
    }
    data[field] = value
    with pytest.raises(ValidationError):
        BlindCriterion.model_validate(data)


@pytest.mark.parametrize(
    "field,count", [("criteria", 257), ("defects", 513), ("notes", 100_001)]
)
def test_answer_collection_and_text_limits(field, count):
    data = answered_data()
    if field == "criteria":
        data[field] = [{"check_id": f"c-{n}", "state": "unknown"} for n in range(count)]
    elif field == "defects":
        data[field] = [
            {
                "finding_id": f"f-{n}",
                "kind": "defect",
                "category": "accuracy",
                "severity": "minor",
                "description": "Defect.",
                "span": None,
            }
            for n in range(count)
        ]
    else:
        data[field] = "x" * count
    with pytest.raises(ValidationError):
        Answer.model_validate(data)


def test_maximum_case_and_rater_counts_are_accepted():
    manifest, _, files = export(256, 1)
    packet, template = packet_template(manifest, files)
    assert len(packet.tasks) == len(template.answers) == 256
    assert packet.tasks[-1].position == 255
    manifest, _, files = export(1, 16)
    assert len(manifest.assignments) == 16 and len(files) == 32


def test_packet_expansion_is_bounded_separately_from_input_objects(monkeypatch):
    from draftbench import annotation_export

    bundle, rubric, roster = inputs(3, 1)
    data = rubric.model_dump()
    data["instructions"] = "x" * 100_000
    rubric = AnnotationRubric.model_validate(seal(data))
    limit = max(
        len(canonical_bytes(obj.model_dump())) for obj in (bundle, rubric, roster)
    )
    monkeypatch.setattr("draftbench.annotation_export.MAX_OBJECT_BYTES", limit)
    original_seal = annotation_export._seal

    def bounded_seal(model, data):
        assert model is not BlindPacket, "oversized packet must not be assembled"
        return original_seal(model, data)

    monkeypatch.setattr(annotation_export, "_seal", bounded_seal)
    with pytest.raises(AnnotationError, match="^annotation_limit$"):
        build_export(bundle, rubric, roster)


def test_whole_export_budget_counts_file_copies_and_private_manifest(monkeypatch):
    monkeypatch.setattr(
        "draftbench.annotation_export.utc_now", lambda: "2026-09-14T10:00:00Z"
    )
    args = inputs(2, 2)
    manifest, objects, files = build_export(*args)
    total = (
        sum(map(len, objects.values()))
        + sum(map(len, files.values()))
        + 2 * len(canonical_bytes(manifest.model_dump()))
    )
    monkeypatch.setattr("draftbench.annotation_export.MAX_EXPORT_BYTES", total)
    assert len(build_export(*args)[2]) == 4
    monkeypatch.setattr("draftbench.annotation_export.MAX_EXPORT_BYTES", total - 1)
    with pytest.raises(AnnotationError, match="^annotation_limit$"):
        build_export(*args)


@pytest.mark.parametrize("target", ["bundle", "rubric", "roster"])
def test_wrong_input_types_refused_safely(target):
    args = list(inputs(1, 1))
    args[["bundle", "rubric", "roster"].index(target)] = {"PRIVATE": "bad input"}
    with pytest.raises(AnnotationError, match="^annotation_invalid$"):
        build_export(*args)


@pytest.mark.parametrize(
    "code",
    [
        "annotation_invalid",
        "annotation_limit",
        "annotation_binding",
        "annotation_conflict",
        "annotation_path",
        "annotation_exists",
        "annotation_io",
        "annotation_unknown_record",
    ],
)
def test_safe_error_codes_allowlisted(code):
    assert str(AnnotationError(code)) == code
    assert (
        str(AnnotationError("PRIVATE arbitrary exception /secret/path"))
        == "annotation_invalid"
    )
