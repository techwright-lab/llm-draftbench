"""Synthetic adversarial inputs; all filesystem writes stay under pytest tmp dirs."""

import copy
import json
import os
from pathlib import Path

import pytest
from conftest import digest, seal
from pydantic import ValidationError

from draftbench.cli import main
from draftbench.identity import canonical_bytes, strict_json_loads
from draftbench.loader import SuiteError, load_suite
from draftbench.models import Case, Suite
from draftbench.report import generator_payload, inventory


def reseal_manifest(path, **updates):
    value = json.loads(path.read_text())
    value.update(updates)
    path.write_text(json.dumps(seal(value)))


def test_manifest_identity_changes(make_suite):
    path = make_suite()
    value = json.loads(path.read_text())
    value["suite_id"] = "changed"
    path.write_text(json.dumps(value))
    with pytest.raises(SuiteError, match="invalid_manifest"):
        load_suite(path)


def test_annotation_change_requires_new_identity(case_data):
    case_data["evaluator"]["labels"][0].update(state="known", value=False)
    with pytest.raises(ValidationError, match="identity_mismatch"):
        Case.model_validate(case_data)
    assert Case.model_validate(seal(case_data)).evaluator.labels[0].value is False


def test_redistributable_case_cannot_embed_private_artifact(case_data):
    case_data["source"]["artifact"]["rights"] = {
        **case_data["rights"],
        "usage": "private",
    }
    with pytest.raises(ValidationError, match="rights_conflict"):
        Case.model_validate(seal(case_data))


def test_private_external_suite_with_evidence_and_review(make_suite, case_data):
    # Exercises private usage without any non-synthetic or repo-local private data.
    case_data["rights"]["usage"] = "private"
    artifact = copy.deepcopy(case_data["source"]["artifact"])
    artifact.update(
        artifact_id="evidence-1",
        file={"path": "evidence.txt", "sha256": digest(b"Synthetic evidence.")},
    )
    case_data["evidence"] = {"state": "available", "artifacts": [artifact]}
    producer = case_data["source"]["artifact"]["producer"]
    case_data["history"]["reviews"] = [
        {
            "review_id": "review-1",
            "round": 1,
            "draft_id": "draft-1",
            "feedback": "Synthetic feedback.",
            "producer": producer,
        }
    ]
    packet = case_data["generator"]["writer"]["input"]
    packet.update(
        basis="reconstructed",
        evidence_ids=["evidence-1"],
        draft_ids=["draft-1"],
        review_ids=["review-1"],
    )
    case_data["generator"]["revision"] = {
        "state": "available",
        "input": copy.deepcopy(packet),
    }
    path = make_suite([seal(case_data)])
    (path.parent / "evidence.txt").write_bytes(b"Synthetic evidence.")
    reseal_manifest(path, rights=case_data["rights"], purpose="evaluation")
    loaded = load_suite(path)
    assert loaded.manifest.rights.usage == "private"
    report = inventory(loaded)
    assert report["evidence"]["available"] == 1
    assert report["roles"]["revision"]["reconstructed"] == 1
    projected = generator_payload(loaded.cases[0], "revision")
    assert projected["evidence"][0]["file"]["sha256"] == digest(b"Synthetic evidence.")
    assert projected["reviews"][0]["feedback"] == "Synthetic feedback."
    (path.parent / "evidence.txt").write_bytes(b"Changed evidence.")
    with pytest.raises(SuiteError, match="artifact_hash_mismatch"):
        load_suite(path)


def test_same_source_cannot_cross_splits_under_new_family(make_suite, case_data):
    other = copy.deepcopy(case_data)
    other.update(case_id="case-2", source_family="family-2", split="confirmation")
    path = make_suite([case_data, seal(other)])
    reseal_manifest(path, purpose="evaluation")
    with pytest.raises(SuiteError, match="split_source_collision"):
        load_suite(path)


@pytest.mark.parametrize("state", ["unknown", "unavailable", "inapplicable", "invalid"])
def test_unavailable_states_remain_distinct(state, make_suite, case_data):
    case_data["generator"]["writer"] = {"state": state, "input": None}
    case_data["history"] = {"state": state, "drafts": [], "reviews": []}
    case_data["evaluator"] = {"labels": []}
    case_data["evidence"] = {"state": state, "artifacts": []}
    report = inventory(load_suite(make_suite([seal(case_data)])))
    assert report["roles"]["writer"][state] == 1
    assert sum(report["roles"]["writer"].values()) == 1
    assert report["history"][state] == 1
    assert report["evidence"][state] == 1


@pytest.mark.parametrize("state", ["unknown", "inapplicable", "invalid"])
def test_label_non_answers_remain_distinct(state, make_suite, case_data):
    case_data["evaluator"]["labels"][0]["state"] = state
    report = inventory(load_suite(make_suite([seal(case_data)])))
    assert report["labels"][state] == 1
    assert report["labels"]["known"] == 0


def test_family_split_counts_are_not_case_ids(make_suite, case_data):
    other = copy.deepcopy(case_data)
    other["case_id"] = "case-2"
    loaded = load_suite(make_suite([case_data, seal(other)]))
    report = inventory(loaded)
    assert report["case_count"] == 2
    assert report["source_family_count"] == 1
    assert report["splits"] == {"development": 2, "confirmation": 0}


def test_directory_symlinks_rejected_even_inside_root(make_suite, case_data):
    case_data["source"]["artifact"]["file"]["path"] = "linked/source.txt"
    path = make_suite([seal(case_data)])
    (path.parent / "linked").symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(SuiteError, match="unsafe_reference"):
        load_suite(path)


def test_directory_reference_rejected(make_suite):
    path = make_suite()
    (path.parent / "source.txt").unlink()
    (path.parent / "source.txt").mkdir()
    with pytest.raises(SuiteError, match="unsafe_reference"):
        load_suite(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX-only special file")
def test_fifo_is_rejected_without_opening(make_suite):
    path = make_suite()
    (path.parent / "source.txt").unlink()
    os.mkfifo(path.parent / "source.txt")
    with pytest.raises(SuiteError, match="unsafe_reference"):
        load_suite(path)


def test_total_byte_budget(make_suite, monkeypatch):
    import draftbench.loader as loader

    path = make_suite()
    monkeypatch.setattr(loader, "MAX_TOTAL_BYTES", path.stat().st_size + 1)
    with pytest.raises(SuiteError, match="input_limit"):
        load_suite(path)


def test_case_count_budget(make_suite, monkeypatch):
    import draftbench.loader as loader

    path = make_suite()
    monkeypatch.setattr(loader, "MAX_CASES", 0)
    with pytest.raises(SuiteError, match="case_count_limit"):
        load_suite(path)


@pytest.mark.parametrize(
    "text", [b"", b"\n", b"{}\n\n", b'{"a":1,"a":2}\n', b"\xff\n", b"[1,2]\n"]
)
def test_invalid_jsonl(make_suite, text):
    path = make_suite()
    (path.parent / "cases.jsonl").write_bytes(text)
    reseal_manifest(path, cases={"path": "cases.jsonl", "sha256": digest(text)})
    with pytest.raises(SuiteError):
        load_suite(path)


def test_deep_json_and_surrogates_rejected():
    with pytest.raises(ValueError):
        strict_json_loads("[" * 60 + "0" + "]" * 60)
    with pytest.raises(ValueError):
        strict_json_loads('"\\ud800"')
    with pytest.raises(ValueError):
        canonical_bytes({"a": [float("nan")]})


def test_manifest_nested_contracts(make_suite):
    path = make_suite()
    data = json.loads(path.read_text())
    for field, value in [
        ("schema_version", 1),
        ("identity", True),
        ("rights", None),
        ("cases", {"path": "cases.jsonl", "sha256": "f" * 64, "extra": "no"}),
    ]:
        altered = copy.deepcopy(data)
        altered[field] = value
        with pytest.raises(ValidationError):
            Suite.model_validate(altered)


@pytest.mark.parametrize(
    "path", ["CON", "aux.txt", "COM1.data", "nested/LPT9", "file.", "a" * 256]
)
def test_nonportable_file_names_rejected(case_data, path):
    case_data["source"]["artifact"]["file"]["path"] = path
    with pytest.raises(ValidationError, match="nonportable_reference"):
        Case.model_validate(seal(case_data))


def test_schema_validates_public_sample():
    from jsonschema import Draft202012Validator

    root = Path(__file__).parents[1]
    for name, model in [("case", Case), ("suite", Suite)]:
        schema = model.model_json_schema()
        Draft202012Validator.check_schema(schema)
        file = "cases.jsonl" if name == "case" else "suite.json"
        data = json.loads((root / "examples/synthetic" / file).read_text())
        Draft202012Validator(schema).validate(data)


def test_output_symlink_never_followed(make_suite, capsys):
    path = make_suite()
    destination = path.parent / "new-report.json"
    output = path.parent / "report-link.json"
    output.symlink_to(destination)
    assert main(["inventory", str(path), "--output", str(output)]) == 2
    assert not destination.exists()
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists"


def test_invalid_cli_arguments_do_not_echo_private_tokens(capsys):
    with pytest.raises(SystemExit) as result:
        main(["PRIVATE-ARGUMENT"])
    assert result.value.code == 2
    assert "PRIVATE-ARGUMENT" not in capsys.readouterr().err
