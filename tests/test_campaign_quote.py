from decimal import ROUND_DOWN, Decimal, localcontext

from draftbench.adapters.pilot_policy import GPT6Policy


def test_reservation_quote_cannot_round_down_in_host_decimal_context():
    policy = GPT6Policy(
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
    expected = Decimal("23.0575")
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        assert policy.reservation_cost == expected
