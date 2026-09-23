import json

import pytest
from conftest import seal
from pydantic import ValidationError

from draftbench.models import Case, Producer
from draftbench.report import generator_payload


def observed(state, value=None):
    return {"state": state, "value": value}


def partial_model():
    return {
        "kind": "model",
        "name": observed("known", "PRIVATE-provider"),
        "version": observed("unavailable"),
        "requested_model": observed("known", "PRIVATE-requested"),
        "served_model": observed("unknown"),
    }


def test_partial_model_preserves_known_ids_and_boundary(case_data):
    producer = partial_model()
    case_data["history"]["drafts"][0]["producer"] = producer
    case_data["generator"]["writer"]["input"]["draft_ids"] = ["draft-1"]
    case = Case.model_validate(seal(case_data))
    assert case.history.drafts[0].producer.model_dump() == producer
    assert "PRIVATE-" not in json.dumps(generator_payload(case, "writer"))
    case_data["history"]["drafts"][0]["producer"]["served_model"] = observed(
        "unavailable"
    )
    with pytest.raises(ValidationError, match="identity_mismatch"):
        Case.model_validate(case_data)


@pytest.mark.parametrize(
    "field", ["name", "version", "requested_model", "served_model"]
)
@pytest.mark.parametrize("state", ["known", "unknown", "unavailable"])
def test_each_producer_field_independent(field, state):
    producer = partial_model()
    producer[field] = observed(state, "preserved-id" if state == "known" else None)
    assert Producer.model_validate(producer).model_dump() == producer


@pytest.mark.parametrize(
    "field", ["name", "version", "requested_model", "served_model"]
)
@pytest.mark.parametrize(
    "bad",
    [
        observed("known"),
        observed("unknown", "guess"),
        observed("unavailable", "guess"),
        observed("known", 12),
        observed("known", True),
        observed("known", ""),
        observed("known", "x" * 513),
        observed("inapplicable"),
        {"state": "unknown", "value": None, "extra": "no"},
    ],
)
def test_model_producer_rejects_contradictions(field, bad):
    producer = partial_model()
    producer[field] = bad
    with pytest.raises(ValidationError):
        Producer.model_validate(producer)


@pytest.mark.parametrize("kind", ["human", "workflow", "synthetic"])
def test_non_model_has_inapplicable_model_fields(kind):
    producer = partial_model()
    producer.update(
        kind=kind,
        requested_model=observed("inapplicable"),
        served_model=observed("inapplicable"),
    )
    assert Producer.model_validate(producer).kind == kind
    for field in ("requested_model", "served_model"):
        for state in ("known", "unknown", "unavailable"):
            bad = dict(producer)
            bad[field] = observed(state, "model" if state == "known" else None)
            with pytest.raises(ValidationError):
                Producer.model_validate(bad)


@pytest.mark.parametrize(
    "field", ["kind", "name", "version", "requested_model", "served_model"]
)
def test_producer_fields_required(field):
    producer = partial_model()
    del producer[field]
    with pytest.raises(ValidationError):
        Producer.model_validate(producer)
