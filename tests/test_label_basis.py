import json

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.loader import load_suite
from draftbench.models import Case
from draftbench.report import generator_payload, inventory


@pytest.mark.parametrize("basis", ["historical_exact", "reconstructed", "synthetic"])
@pytest.mark.parametrize(
    "state,value",
    [("known", False), ("unknown", None), ("inapplicable", None), ("invalid", None)],
)
def test_label_basis_independent_of_answer_and_producer(
    case_data, make_suite, basis, state, value
):
    label = case_data["evaluator"]["labels"][0]
    label.update(basis=basis, state=state, value=value)
    case = Case.model_validate(seal(case_data))
    assert case.evaluator.labels[0].basis == basis
    assert case.evaluator.labels[0].producer.kind == "synthetic"
    report = inventory(load_suite(make_suite([case_data])))
    assert report["labels"][state] == 1
    assert report["label_bases"][basis] == 1
    assert sum(report["label_bases"].values()) == 1
    assert "basis" not in json.dumps(generator_payload(case, "writer"))


def test_label_basis_required(case_data):
    case_data["evaluator"]["labels"][0].pop("basis", None)
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


@pytest.mark.parametrize("basis", [None, 1, True, "unknown", "gold"])
def test_label_basis_strict(case_data, basis):
    case_data["evaluator"]["labels"][0]["basis"] = basis
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


def test_label_basis_identity(case_data):
    label = case_data["evaluator"]["labels"][0]
    label["basis"] = "historical_exact"
    Case.model_validate(seal(case_data))
    label["basis"] = "reconstructed"
    with pytest.raises(ValidationError, match="identity_mismatch"):
        Case.model_validate(case_data)
