"""Synthetic counter shapes observed at the provider boundary; no private text."""

from types import SimpleNamespace

import pytest

from draftbench.adapters.anthropic_contract import _usage


def policy():
    return SimpleNamespace(
        context_window_tokens=1000000,
        max_output_tokens=128000,
        token_cost=lambda i, o: "0.1",
    )


def test_standard_speed_is_metadata_not_a_token_counter():
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "speed": "standard",
        "service_tier": "standard",
    }
    measured, estimate = _usage({"usage": usage}, policy())
    assert measured == usage
    assert estimate == "0.1"


@pytest.mark.parametrize("speed", ["fast", True, 1, {}])
def test_unknown_speed_is_not_priced_as_standard(speed):
    with pytest.raises(ValueError, match="invalid_usage"):
        _usage(
            {"usage": {"input_tokens": 100, "output_tokens": 20, "speed": speed}},
            policy(),
        )
