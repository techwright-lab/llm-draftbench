import copy
import json

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.identity import canonical_bytes, identity, strict_json_loads
from draftbench.loader import SuiteError, load_suite
from draftbench.models import Case, Suite
from draftbench.report import generator_payload, inventory


def test_valid_synthetic_and_order_preservation(make_suite):
    loaded = load_suite(make_suite())
    assert len(loaded.cases) == 1
    assert [u.unit_id for u in loaded.cases[0].history.drafts[0].units] == [
        "post-1",
        "post-2",
    ]
    report = inventory(loaded)
    assert report["case_count"] == 1
    assert report["roles"]["writer"]["synthetic"] == 1
    assert report["roles"]["reviewer"]["unknown"] == 1
    assert report["roles"]["revision"]["inapplicable"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.pop("rights"),
        lambda c: c.pop("source"),
        lambda c: c.update(extra="no"),
        lambda c: c["generator"]["writer"]["input"]["messages"][0].update(content=12),
        lambda c: c["history"]["drafts"][0].update(version=True),
        lambda c: c["history"]["drafts"][0].update(version="1"),
        lambda c: c["evidence"].update(state="available"),
        lambda c: c["generator"]["writer"].update(state="unknown"),
        lambda c: c["evaluator"]["labels"][0].update(value=False),
        lambda c: c["generator"]["writer"]["input"].update(evidence_ids=["missing"]),
        lambda c: c["history"]["drafts"][0]["units"].append(
            c["history"]["drafts"][0]["units"][0]
        ),
        lambda c: c["source"]["artifact"]["producer"].update(kind="model"),
    ],
)
def test_strict_nested_contracts(case_data, mutation):
    mutation(case_data)
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/a",
        "../a",
        "a/../b",
        "https://example.org/a",
        "file:///a",
        "C:/a",
        "a\\b",
        "./a",
        "a//b",
    ],
)
def test_portable_references_reject_unsafe_paths(case_data, path):
    case_data["source"]["artifact"]["file"]["path"] = path
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


def test_identity_changes_and_strict_json(case_data):
    original = case_data["identity"]
    assert identity(case_data) == original
    case_data["history"]["drafts"][0]["units"].reverse()
    assert identity(case_data) != original
    with pytest.raises(ValidationError):
        Case.model_validate(case_data)
    assert canonical_bytes({"z": 1, "a": "é"}) == '{"a":"é","z":1}'.encode()
    for value in [float("nan"), float("inf"), {1: "x"}, (1, 2)]:
        with pytest.raises(ValueError):
            canonical_bytes(value)
    for text in ['{"a":1,"a":2}', '{"a": NaN}', '{"a": 1e999}', "\ufeff{}"]:
        with pytest.raises(ValueError):
            strict_json_loads(text)


def test_duplicate_case_ids(make_suite, case_data):
    with pytest.raises(SuiteError, match="duplicate_case_id"):
        load_suite(make_suite([case_data, case_data]))


def test_split_family_collision(make_suite, case_data):
    other = copy.deepcopy(case_data)
    other.update(case_id="case-2", split="confirmation")
    with pytest.raises(SuiteError, match="split_family_collision"):
        load_suite(make_suite([case_data, seal(other)]))


def test_synthetic_cannot_claim_confirmation(make_suite, case_data):
    case_data["split"] = "confirmation"
    with pytest.raises(SuiteError, match="synthetic_confirmation"):
        load_suite(make_suite([seal(case_data)]))


def test_raw_hash_detects_source_and_case_changes(make_suite):
    path = make_suite()
    (path.parent / "source.txt").write_text("changed")
    with pytest.raises(SuiteError, match="artifact_hash_mismatch"):
        load_suite(path)
    path = make_suite()
    with (path.parent / "cases.jsonl").open("ab") as f:
        f.write(b" ")
    with pytest.raises(SuiteError, match="artifact_hash_mismatch"):
        load_suite(path)


def test_symlink_escape_rejected(make_suite, tmp_path):
    path = make_suite()
    target = tmp_path.parent / (tmp_path.name + "-outside.txt")
    target.write_bytes(b"Synthetic source.\n")
    (tmp_path / "source.txt").unlink()
    (tmp_path / "source.txt").symlink_to(target)
    with pytest.raises(SuiteError, match="unsafe_reference"):
        load_suite(path)


def test_bounded_reads(make_suite, monkeypatch):
    import draftbench.loader as loader

    path = make_suite()
    monkeypatch.setattr(loader, "MAX_FILE_BYTES", 10)
    with pytest.raises(SuiteError, match="input_limit"):
        load_suite(path)


def test_replayability_distinguishes_current_and_historical(make_suite, case_data):
    packet = case_data["generator"]["writer"]["input"]
    packet["basis"] = "historical_exact"
    exact = seal(copy.deepcopy(case_data))
    case_data["case_id"] = "case-2"
    packet["basis"] = "reconstructed"
    loaded = load_suite(make_suite([exact, seal(case_data)]))
    report = inventory(loaded)
    assert report["roles"]["writer"]["historical_exact"] == 1
    assert report["roles"]["writer"]["reconstructed"] == 1
    assert report["roles"]["writer"]["synthetic"] == 0
    assert report["execution_performed"] is False


def test_generator_boundary(case_data):
    case_data["generator"]["writer"]["input"]["draft_ids"] = ["draft-1"]
    case_data["evaluator"]["labels"][0].update(state="known", value="SECRET-EVALUATOR")
    case = Case.model_validate(seal(case_data))
    payload = generator_payload(case, "writer")
    text = json.dumps(payload)
    assert "SECRET-EVALUATOR" not in text
    assert "evaluator" not in text
    assert "producer" not in text
    assert payload["drafts"][0]["units"][1]["content"] == "Second synthetic post."
    assert generator_payload(case, "reviewer") is None


def test_exported_schema_matches_models():
    from pathlib import Path

    for name, model in [("case", Case), ("suite", Suite)]:
        exported = json.loads(
            (Path(__file__).parents[1] / "schemas" / f"{name}.schema.json").read_text()
        )
        assert exported == model.model_json_schema()
