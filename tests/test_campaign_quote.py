from decimal import ROUND_DOWN, Decimal, localcontext

from draftbench.adapters.openai_contract import OpenAIPolicy


def test_reservation_quote_cannot_round_down_in_host_decimal_context():
    policy = OpenAIPolicy(
        model="gpt-4.1-2025-04-14",
        account_route="mock",
        project="proj_mock",
        organization="org_mock",
        currency="USD",
        input_per_million="20",
        output_per_million="8",
        max_cost="50",
        context_window_tokens=1047576,
        max_output_tokens=100,
        max_requests=3,
        max_total_tokens=4000000,
    )
    expected = Decimal("20.95232")
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        assert policy.reservation_cost == expected
