"""Synthetic prices/providers only. No provider SDK, credentials or network."""

import multiprocessing
import os
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from draftbench.campaign import CampaignBudget


def reservation(index=0, **changes):
    return dict(
        attempt_id=f"{index:032x}",
        run_digest=f"{index:064x}",
        request_digest="a" * 64,
        config_digest="b" * 64,
        provider="mock-a" if index % 2 else "mock-b",
        currency="USD",
        upper_bound=Decimal("10"),
        **changes,
    )


def worker(path, index, start, queue):
    start.wait()
    try:
        with CampaignBudget.open(path) as budget:
            budget.reserve(**reservation(index))
        queue.put("reserved")
    except ValueError as exc:
        queue.put(str(exc))


def crash_worker(path):
    with CampaignBudget.open(path) as budget:
        budget.reserve(**reservation())
        os._exit(17)


def test_two_mock_providers_share_one_fifty_dollar_ceiling(tmp_path):
    path = tmp_path / "campaign.db"
    with CampaignBudget.create(path) as budget:
        identity = budget.identity
        for i in range(5):
            budget.reserve(**reservation(i))
        assert budget.summary() == {
            **identity,
            "reservation_count": 5,
            "reserved_usd": "50.00000000000000",
        }
        with pytest.raises(ValueError, match="campaign_exhausted"):
            budget.reserve(**reservation(5))
    with CampaignBudget.open(path) as budget:
        assert budget.identity == identity
        budget.reserve(**reservation(0))  # idempotent even when exhausted
        assert budget.summary()["reservation_count"] == 5
    with pytest.raises(ValueError):
        CampaignBudget.create(path)


@pytest.mark.parametrize(
    "amount",
    [
        None,
        1.0,
        "10",
        True,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0"),
    ],
)
def test_unknown_or_non_exact_pricing_fails_closed(tmp_path, amount):
    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        args = reservation()
        args["upper_bound"] = amount
        with pytest.raises(ValueError):
            budget.reserve(**args)
        assert budget.summary()["reservation_count"] == 0


def test_currency_binding_and_exact_round_up(tmp_path):
    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        args = reservation()
        args["currency"] = "EUR"
        with pytest.raises(ValueError, match="campaign_currency"):
            budget.reserve(**args)
        args["currency"] = "USD"
        args["upper_bound"] = Decimal("49.999999999999991")
        with localcontext() as ctx:
            ctx.prec = 2
            budget.reserve(**args)
        assert budget.summary()["reserved_usd"] == "50.00000000000000"
        for field, value in [
            ("provider", "other"),
            ("run_digest", "c" * 64),
            ("request_digest", "c" * 64),
            ("config_digest", "c" * 64),
            ("upper_bound", Decimal("1")),
        ]:
            with pytest.raises(ValueError, match="campaign_binding_mismatch"):
                budget.reserve(**{**args, field: value})


def test_real_processes_contend_for_aggregate_budget(tmp_path):
    path = tmp_path / "campaign.db"
    CampaignBudget.create(path).close()
    ctx = multiprocessing.get_context("spawn")
    start, queue = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=worker, args=(path, i, start, queue)) for i in range(10)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert results.count("reserved") == 5
    assert results.count("campaign_exhausted") == 5
    with CampaignBudget.open(path) as budget:
        assert budget.summary()["reserved_usd"] == "50.00000000000000"


def test_committed_reservation_survives_process_death(tmp_path):
    path = tmp_path / "campaign.db"
    CampaignBudget.create(path).close()
    process = multiprocessing.get_context("spawn").Process(
        target=crash_worker, args=(path,)
    )
    process.start()
    process.join(30)
    assert process.exitcode == 17
    with CampaignBudget.open(path) as budget:
        budget.reserve(**reservation())
        assert budget.summary()["reservation_count"] == 1
        assert budget.summary()["reserved_usd"] == "10.00000000000000"


def test_workflow_reservation_precedes_dispatch_and_resume_requires_campaign(
    tmp_path, monkeypatch
):
    from draftbench import provider_workflow as workflow
    from draftbench.adapters import openai
    from draftbench.adapters.openai_contract import OpenAIPolicy

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
    monkeypatch.setattr(openai, "preflight", lambda transport: None)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(True)
        assert budget.summary()["reservation_count"] == len(calls)
        raise KeyboardInterrupt()

    monkeypatch.setattr(workflow, "invoke", interrupted)
    suite = Path(__file__).parents[1] / "examples/smoke/suite.json"
    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        for i in range(2):
            root = tmp_path / f"run-{i}"
            with pytest.raises(KeyboardInterrupt):
                workflow.run_openai(
                    suite, root, policy, transport=object(), campaign=budget
                )
            with pytest.raises(ValueError, match="campaign_required"):
                workflow.resume_openai(root, transport=object())
            workflow.resume_openai(root, transport=object(), campaign=budget)
        result = workflow.run_openai(
            suite, tmp_path / "run-3", policy, transport=object(), campaign=budget
        )
        assert result["attempt_count"] == 1  # local reservation cannot dispatch
        assert len(calls) == 2
        assert budget.summary()["reservation_count"] == 2
