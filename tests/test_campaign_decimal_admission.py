"""Budget gates and their accounting must not inherit host Decimal arithmetic."""

from decimal import ROUND_DOWN, ROUND_UP, Decimal, Inexact, Rounded, localcontext
from pathlib import Path

import pytest

from draftbench.adapters.openai_contract import _usage
from draftbench.adapters.pilot_policy import GPT6Policy
from draftbench.campaign import CampaignBudget
from draftbench.ledger import Ledger
from draftbench.provider_workflow import report_openai, resume_openai, run_openai


def policy(**changes):
    return GPT6Policy.model_validate(
        dict(
            model="gpt-6-astra",
            account_route="mock",
            project="proj_mock",
            organization="org_mock",
            currency="USD",
            max_cost="50",
            max_output_tokens=100,
            max_requests=3,
            max_total_tokens=4000000,
            pricing_provenance="public-docs-2026-09-23-v1",
            reasoning_effort="medium",
        )
        | changes
    )


@pytest.mark.parametrize("traps", [False, True])
@pytest.mark.parametrize("rounding", [ROUND_DOWN, ROUND_UP])
@pytest.mark.parametrize(
    "count,cap,allowed", [(1, "23", False), (2, "46", False), (2, "46.115", True)]
)
def test_exact_admission(count, cap, allowed, traps, rounding):
    p = policy(max_cost=cap)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = rounding
        ctx.traps[Inexact] = ctx.traps[Rounded] = traps
        before = ctx.copy()
        if allowed:
            p.admit(count)
        else:
            with pytest.raises(ValueError, match="^admission_exhausted$"):
                p.admit(count)
        assert ctx.prec == before.prec
        assert ctx.rounding == before.rounding
        assert ctx.traps == before.traps
        assert ctx.flags == before.flags


@pytest.mark.parametrize("cap,expected_count", [("23", 0), ("46", 1)])
@pytest.mark.parametrize("traps", [False, True])
def test_campaign_run_cap_before_dispatch_and_resume(
    tmp_path, cap, expected_count, traps
):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("openai")
    from draftbench.adapters.openai_fixture import fixture_transport

    p = policy(max_cost=cap)
    fixture = fixture_transport(p.model)
    calls = []

    def handler(request):
        calls.append(request)
        return fixture.handle_request(request)

    transport = httpx.MockTransport(handler)
    root = tmp_path / "run"
    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.rounding = ROUND_DOWN
            ctx.traps[Inexact] = ctx.traps[Rounded] = traps
            result = run_openai(suite, root, p, transport=transport, campaign=budget)
            assert len(calls) == expected_count
            assert result["attempt_count"] == expected_count
            assert Decimal(result["reserved_cost"]) == Decimal(
                "23.0575" if expected_count else "0"
            )
            assert resume_openai(root, transport=transport, campaign=budget) == result
            assert report_openai(root) == result
            assert len(calls) == expected_count
            summary = budget.summary()
            assert summary["reservation_count"] == expected_count
            assert Decimal(summary["reserved_usd"]) == Decimal(result["reserved_cost"])
        assert report_openai(root) == result


def test_quote_and_usage_ignore_exponent_limits_and_traps():
    p = policy()
    native = {
        "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    }
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.Emax = 1
        ctx.Emin = -1
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert p.reservation_cost == Decimal("23.0575")
        assert _usage(native, p)[1] == "0.00031"
        p.admit(2)
        assert not any(ctx.flags.values())


@pytest.mark.parametrize("traps", [False, True])
def test_sibling_fixture_ledger_cap_is_exact(tmp_path, traps):
    from draftbench.adapters.inspect import InspectPolicy

    p = InspectPolicy(
        price_per_million=20,
        max_cost_usd=20,
        max_input_bytes=1000000,
        max_output_tokens=100,
    )
    plan = [{"work_id": "w", "case_id": "c", "role": "writer", "dependencies": []}]
    with Ledger.create(tmp_path / "ledger.db", plan, "a" * 64) as ledger:
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.rounding = ROUND_DOWN
            ctx.traps[Inexact] = ctx.traps[Rounded] = traps
            with pytest.raises(ValueError, match="^admission_exhausted$"):
                ledger.reserve("w", "b" * 64, policy=p)
        assert ledger.summary()["attempt_count"] == 0
