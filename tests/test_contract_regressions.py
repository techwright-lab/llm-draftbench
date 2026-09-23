"""Cross-axis proofs for public fixture truthfulness and safe aggregate output."""

import copy
import json
from pathlib import Path

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.cli import main
from draftbench.loader import load_suite
from draftbench.models import Case, Source
from draftbench.report import generator_payload, inventory


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        {"state": "unknown"},
        {"artifact": None},
        {"state": True, "artifact": None},
        {"state": "unknown", "artifact": False},
        {"state": "unknown", "artifact": None, "extra": "no"},
    ],
)
def test_source_strict_structure(source):
    with pytest.raises(ValidationError):
        Source.model_validate(source)


def test_label_count_axes_aggregate_independently(case_data, make_suite):
    labels = []
    for index, (basis, state, value) in enumerate(
        [
            ("historical_exact", "unknown", None),
            ("historical_exact", "known", False),
            ("reconstructed", "known", 0),
            ("synthetic", "invalid", None),
        ]
    ):
        label = copy.deepcopy(case_data["evaluator"]["labels"][0])
        label.update(label_id=f"label-{index}", basis=basis, state=state, value=value)
        labels.append(label)
    case_data["evaluator"]["labels"] = labels
    report = inventory(load_suite(make_suite([seal(case_data)])))
    assert report["label_bases"] == {
        "historical_exact": 2,
        "reconstructed": 1,
        "synthetic": 1,
    }
    assert report["labels"] == {
        "known": 2,
        "unknown": 1,
        "inapplicable": 0,
        "invalid": 1,
    }


def test_public_sample_does_not_invent_model_identity_or_gold():
    path = Path(__file__).parents[1] / "examples/synthetic/suite.json"
    loaded = load_suite(path)
    case = loaded.cases[0]
    producer = case.history.drafts[0].producer
    assert producer.kind == "model"
    assert producer.requested_model.state == "unknown"
    assert producer.served_model.state == "unavailable"
    assert producer.requested_model.value is producer.served_model.value is None
    assert case.evidence.state == "unavailable"
    report = inventory(loaded)
    assert report["labels"]["known"] == 0
    assert report["label_bases"]["synthetic"] == 1
    assert report["execution_performed"] is False
    for role in ("writer", "reviewer", "revision"):
        assert report["replay_readiness"][role]["historical_exact_inputs_ready"] == 0
    payload = generator_payload(case, "writer")
    assert payload["source"] == {
        "state": "available",
        "file": case.source.artifact.file.model_dump(),
    }
    for key in ("metadata", "evaluator", "producer", "basis"):
        assert key not in json.dumps(payload)


def test_cli_does_not_leak_new_observation_errors(case_data, make_suite, capsys):
    case_data["metadata"]["domain"] = {"state": "unknown", "value": "PRIVATE-METADATA"}
    path = make_suite([seal(case_data)])
    assert main(["inventory", str(path)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"valid": False, "error": "invalid_case"}
    assert captured.out == ""
    assert "PRIVATE-METADATA" not in captured.err
    assert str(path) not in captured.err


def test_documented_identity_snippet(case_data):
    import re

    path = Path(__file__).parents[1] / "schemas/CONTRACT.md"
    snippets = re.findall(r"```python\n(.*?)```", path.read_text(), re.DOTALL)
    assert len(snippets) == 1
    namespace = {"case": case_data}
    exec(compile(snippets[0], "CONTRACT.md snippet", "exec"), namespace)
    assert (
        Case.model_validate_json(namespace["case_line"]).identity
        == case_data["identity"]
    )
