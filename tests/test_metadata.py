import copy
import json

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.loader import load_suite
from draftbench.models import Case
from draftbench.report import generator_payload, inventory


def metadata():
    return {
        key: {"state": "known", "value": "PRIVATE-" + key}
        for key in (
            "content_type",
            "domain",
            "lane",
            "body_format",
            "source_system",
            "source_revision",
        )
    }


def test_metadata_required(case_data):
    case_data.pop("metadata", None)
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


def test_metadata_preserved_identified_and_not_projected(case_data, make_suite):
    case_data["metadata"] = metadata()
    case = Case.model_validate(seal(case_data))
    assert case.model_dump()["metadata"] == metadata()
    report = inventory(load_suite(make_suite([case_data])))
    assert "PRIVATE-" not in json.dumps(report)
    assert "PRIVATE-" not in json.dumps(generator_payload(case, "writer"))
    case_data["metadata"]["domain"]["value"] = "changed"
    with pytest.raises(ValidationError, match="identity_mismatch"):
        Case.model_validate(case_data)


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_metadata_explicit_missing(case_data, state):
    case_data["metadata"] = metadata()
    case_data["metadata"]["source_revision"] = {"state": state, "value": None}
    assert Case.model_validate(seal(case_data)).metadata.source_revision.state == state


@pytest.mark.parametrize(
    "bad",
    [
        {"state": "known", "value": None},
        {"state": "unknown", "value": "guess"},
        {"state": "unavailable", "value": "guess"},
        {"state": "known", "value": 1},
        {"state": "known", "value": True},
        {"state": "known", "value": ""},
        {"state": "known", "value": "x" * 513},
        {"state": "inapplicable", "value": None},
        {"state": "known", "value": "ok", "extra": "no"},
    ],
)
def test_metadata_strict_observations(case_data, bad):
    case_data["metadata"] = metadata()
    case_data["metadata"]["domain"] = copy.deepcopy(bad)
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))


@pytest.mark.parametrize(
    "mutation", [lambda m: m.pop("lane"), lambda m: m.update(extra="no")]
)
def test_metadata_strict_shape(case_data, mutation):
    case_data["metadata"] = metadata()
    mutation(case_data["metadata"])
    with pytest.raises(ValidationError):
        Case.model_validate(seal(case_data))
