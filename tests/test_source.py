import copy

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.loader import SuiteError, load_suite
from draftbench.models import Case
from draftbench.report import generator_payload, inventory


@pytest.mark.parametrize("state", ["unavailable", "unknown", "invalid", "inapplicable"])
def test_missing_source_loadable_and_not_replay_ready(case_data, make_suite, state):
    case_data["source"] = {"state": state, "artifact": None}
    case_data["generator"]["writer"]["input"]["basis"] = "historical_exact"
    case_data["evidence"]["state"] = "inapplicable"
    path = make_suite([seal(case_data)])
    (path.parent / "source.txt").unlink()
    loaded = load_suite(path)
    report = inventory(loaded)
    assert report["source"][state] == 1
    assert report["roles"]["writer"]["historical_exact"] == 1
    assert report["replay_readiness"]["writer"]["historical_exact_inputs_ready"] == 0
    assert report["replay_readiness"]["writer"]["not_ready"] == 1
    assert generator_payload(loaded.cases[0], "writer")["source"] == {
        "state": state,
        "file": None,
    }


def test_missing_sources_do_not_bypass_family_split(case_data, make_suite):
    case_data["source"] = {"state": "unknown", "artifact": None}
    other = copy.deepcopy(case_data)
    other.update(case_id="case-2", split="confirmation")
    with pytest.raises(SuiteError, match="split_family_collision"):
        load_suite(make_suite([seal(case_data), seal(other)]))


def test_missing_sources_have_no_hash_collision(case_data, make_suite):
    case_data["source"] = {"state": "unknown", "artifact": None}
    other = copy.deepcopy(case_data)
    other.update(case_id="case-2", source_family="family-2", split="confirmation")
    path = make_suite([seal(case_data), seal(other)])
    import json

    manifest = json.loads(path.read_text())
    manifest["purpose"] = "evaluation"
    path.write_text(json.dumps(seal(manifest)))
    assert len(load_suite(path).cases) == 2


@pytest.mark.parametrize(
    "state", ["available", "unavailable", "unknown", "invalid", "inapplicable"]
)
def test_source_availability_consistency(case_data, state):
    source = case_data["source"]
    artifact = source["artifact"]
    case_data["source"] = {
        "state": state,
        "artifact": None if state == "available" else artifact,
    }
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


@pytest.mark.parametrize(
    "state,ready",
    [
        ("available", 1),
        ("inapplicable", 1),
        ("unknown", 0),
        ("unavailable", 0),
        ("invalid", 0),
    ],
)
def test_replay_readiness_requires_evidence(case_data, make_suite, state, ready):
    case_data["generator"]["writer"]["input"]["basis"] = "historical_exact"
    source = case_data["source"]
    artifact = copy.deepcopy(source["artifact"])
    artifact["artifact_id"] = "evidence-1"
    case_data["evidence"] = {
        "state": state,
        "artifacts": [artifact] if state == "available" else [],
    }
    report = inventory(load_suite(make_suite([seal(case_data)])))
    assert report["roles"]["writer"]["historical_exact"] == 1
    assert (
        report["replay_readiness"]["writer"]["historical_exact_inputs_ready"] == ready
    )
    assert sum(report["replay_readiness"]["writer"].values()) == 1
    assert report["replay_readiness"]["reviewer"]["not_ready"] == 1


def test_source_state_is_identified(case_data):
    case_data["source"] = {"state": "unknown", "artifact": None}
    Case.model_validate(seal(case_data))
    case_data["source"]["state"] = "unavailable"
    with pytest.raises(ValidationError, match="identity_mismatch"):
        Case.model_validate(case_data)
