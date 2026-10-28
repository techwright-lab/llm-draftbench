"""Tracking mode removes local resource caps, not custody or type validation."""

import pytest

from draftbench.adapters.openai_contract import _usage
from draftbench.adapters.pilot_policy import GPT6Policy


def policy(**changes):
    return GPT6Policy.model_validate(
        {
            "model": "gpt-6.1-sol",
            "account_route": "synthetic",
            "organization": "org-synthetic",
            "project": "proj_synthetic",
            "currency": "USD",
            "pricing_provenance": "operator-reviewed-v1",
            "input_per_million": "100",
            "output_per_million": "500",
            "tracking_only": True,
            "max_cost": None,
            "max_requests": None,
            "max_total_tokens": None,
            "max_input_bytes": None,
            "max_output_tokens": None,
            "timeout_seconds": None,
            "reasoning_effort": "low",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_cost", "50"),
        ("max_requests", 32),
        ("max_total_tokens", 1000000),
        ("max_input_bytes", 100000),
        ("timeout_seconds", 300),
        ("max_output_tokens", 16000),
    ],
)
def test_tracking_mode_refuses_hidden_local_caps(field, value):
    with pytest.raises(ValueError):
        policy(**{field: value})


def test_tracking_long_context_retains_usage_without_guessing_multiplier():
    usage = {"input_tokens": 300000, "output_tokens": 50000, "total_tokens": 350000}
    measured, estimate = _usage({"usage": usage}, policy())
    assert measured == usage
    assert estimate is None


def test_tracking_openai_context_is_not_inherited_from_legacy_models():
    assert policy().context_window_tokens is None


def test_tracking_mode_is_explicit_not_inferred_from_null_caps():
    with pytest.raises(ValueError):
        policy(tracking_only=False)
