import hashlib

import pytest

from draftbench.adapters.fake import FakeAdapter, FakeResult
from draftbench.identity import canonical_bytes


def test_fake_chain_is_deterministic_ordered_and_explicitly_unmeasured():
    adapter = FakeAdapter()
    request = {"role": "writer", "parent_digests": {}, "parents": {}}
    draft = adapter.invoke(request)
    assert draft == adapter.invoke(request)
    assert FakeResult.model_validate(draft).synthetic is True
    assert (
        draft["request_digest"] == hashlib.sha256(canonical_bytes(request)).hexdigest()
    )
    assert [unit["unit_id"] for unit in draft["units"]] == ["unit-1", "unit-2"]
    draft_digest = hashlib.sha256(canonical_bytes(draft)).hexdigest()
    review_request = {
        "role": "reviewer",
        "parents": {"draft": draft},
        "parent_digests": {"draft": draft_digest},
    }
    review = adapter.invoke(review_request)
    FakeResult.model_validate(review)
    assert review["units"] == []
    assert "Not an editorial or factual assessment" in review["feedback"]
    assert "score" not in review
    revision_request = {
        "role": "revision",
        "parents": {"draft": draft, "review": review},
        "parent_digests": {
            "draft": draft_digest,
            "review": hashlib.sha256(canonical_bytes(review)).hexdigest(),
        },
    }
    revision = adapter.invoke(revision_request)
    FakeResult.model_validate(revision)
    assert [unit["unit_id"] for unit in revision["units"]] == ["unit-1", "unit-2"]
    assert draft == adapter.invoke(request)


@pytest.mark.parametrize(
    "mutation",
    [
        {"synthetic": False},
        {"adapter": "openai"},
        {"parent_digests": {"draft": "a" * 64}},
        {"units": []},
        {"score": 100},
        {"request_digest": "not-a-digest"},
    ],
)
def test_fake_results_reject_invalid_or_model_shaped_artifacts(mutation):
    value = FakeAdapter().invoke(
        {"role": "writer", "parent_digests": {}, "parents": {}}
    )
    value.update(mutation)
    with pytest.raises(ValueError):
        FakeResult.model_validate(value)
