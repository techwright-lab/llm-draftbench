"""Versioned five-model, text-only pilot policies; archived prices are not approval."""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..models import Contract, Digest
from .openai_contract import Money, Positive
from .token_budget import TokenBudget

# Standard global USD / million. Source ledger: schemas/FIVE_MODEL_CONTRACT.md.
RATES = {
    "gpt-6-astra": ("10", "50"),
    "gpt-6-sol": ("2", "10"),
    "gpt-6-luna": ("0.1", "0.5"),
    "claude-opus-5-5": ("4", "20"),
    "claude-sonnet-5": ("2", "10"),
}


def served_model_matches(requested, served):
    # A dated snapshot of the requested alias bills at the alias tariff. Any
    # other served id is a different model and must never be accepted.
    return type(served) is str and (
        served == requested
        or re.fullmatch(re.escape(requested) + r"-(?:\d{4}-\d{2}-\d{2}|\d{8})", served)
        is not None
    )


class PilotPolicy(TokenBudget, Contract):
    account_route: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
    currency: Literal["USD"]
    price_unit: Literal["currency_per_million_tokens"] = "currency_per_million_tokens"
    pricing_provenance: Literal["public-docs-2026-09-23-v1"]
    # Independent trusted-host tariff review; never inferred from archived docs.
    verified_tariff_digest: Digest | None = None
    input_per_million: Money
    output_per_million: Money
    max_cost: Money
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=32768)]
    max_requests: Annotated[int, Field(strict=True, ge=1, le=30000)]
    max_total_tokens: Positive
    max_input_bytes: Annotated[int, Field(strict=True, ge=1, le=100000)] = 100000
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30

    @model_validator(mode="before")
    @classmethod
    def frozen_rates(cls, value):
        if isinstance(value, dict) and value.get("model") in RATES:
            value = dict(value)
            i, o = RATES[value["model"]]
            value.setdefault("input_per_million", i)
            value.setdefault("output_per_million", o)
            if (value["input_per_million"], value["output_per_million"]) != (i, o):
                raise ValueError("tariff_mismatch")
        return value

    @model_validator(mode="after")
    def positive_cap(self):
        if Decimal(self.max_cost) <= 0:
            raise ValueError("positive_explicit_pricing_required")
        return self


class GPT6Policy(PilotPolicy):
    contract: Literal["openai-gpt6-text-v1"] = "openai-gpt6-text-v1"
    model: Literal["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
    organization: Annotated[str, Field(pattern=r"^org_[A-Za-z0-9_-]+$")]
    project: Annotated[str, Field(pattern=r"^proj_[A-Za-z0-9_-]+$")]
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    context_window_tokens: Literal[922000] = 922000

    @model_validator(mode="after")
    def effort(self):
        if self.model == "gpt-6-astra" and self.reasoning_effort == "none":
            raise ValueError("astra_requires_reasoning")
        return self

    def reservation_total(self, count):
        # No guessed tokenizer bound: reserve the full documented maximum input
        # at long-context cache-write rate (2 * 1.25), and 1.5x output. Requests
        # remain short, standard, with no explicit caching or tools. This is
        # deliberately more conservative than the observed short-context cost.
        return self.token_cost(
            count * self.context_window_tokens * 5,
            count * self.max_output_tokens * 3,
            denominator=2,
        )


class AnthropicPolicy(PilotPolicy):
    contract: Literal["anthropic-messages-text-v1"] = "anthropic-messages-text-v1"
    model: Literal["claude-opus-5-5", "claude-sonnet-5"]
    context_window_tokens: Literal[1000000] = 1000000
    thinking: Literal["adaptive", "disabled"] = "adaptive"
    effort: Literal["low", "medium", "high", "xhigh", "max"]

    @model_validator(mode="after")
    def thinking_mode(self):
        if self.model == "claude-opus-5-5" and self.thinking != "adaptive":
            raise ValueError("opus_requires_adaptive")
        return self

    def reservation_total(self, count):
        # Full context at maximum 1h cache-write rate. Cache is never requested,
        # but retaining this bound avoids discount/usage assumptions in admission.
        return self.token_cost(
            count * self.context_window_tokens * 2, count * self.max_output_tokens
        )
