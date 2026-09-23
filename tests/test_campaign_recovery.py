"""Crash boundaries and SDK-free campaign custody; synthetic quotes only."""

import multiprocessing
import os
from pathlib import Path

import pytest

from draftbench import provider_workflow as workflow
from draftbench.adapters import openai
from draftbench.adapters.pilot_policy import GPT6Policy
from draftbench.campaign import CampaignBudget
from draftbench.provider_reporting import report_openai


@pytest.fixture
def policy():
    return GPT6Policy(
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


@pytest.fixture
def suite():
    return Path(__file__).parents[1] / "examples/smoke/suite.json"


@pytest.mark.parametrize(
    "boundary,initial_count",
    [("reserved", 0), ("campaign_reserved", 1), ("in_flight", 1)],
)
def test_crash_between_databases_never_refunds_or_double_reserves(
    tmp_path, monkeypatch, suite, policy, boundary, initial_count
):
    monkeypatch.setattr(openai, "preflight", lambda _: None)
    calls = []

    def crash_call(*args, **kwargs):
        calls.append(True)
        raise KeyboardInterrupt()

    monkeypatch.setattr(workflow, "invoke", crash_call)

    def checkpoint(stage, work):
        if stage == boundary:
            raise KeyboardInterrupt()

    path = tmp_path / "campaign.db"
    root = tmp_path / "run"
    with CampaignBudget.create(path) as budget:
        with pytest.raises(KeyboardInterrupt):
            workflow.run_openai(
                suite,
                root,
                policy,
                transport=object(),
                campaign=budget,
                checkpoint=checkpoint,
            )
        assert budget.summary()["reservation_count"] == initial_count
    with CampaignBudget.open(path) as budget:
        if boundary == "in_flight":
            result = workflow.resume_openai(root, transport=object(), campaign=budget)
            assert result["states"]["uncertain"] == 1
            assert calls == []
        else:
            with pytest.raises(KeyboardInterrupt):
                workflow.resume_openai(root, transport=object(), campaign=budget)
            assert len(calls) == 1
        assert budget.summary()["reservation_count"] == 1
        before = budget.summary()
        workflow.resume_openai(root, transport=object(), campaign=budget)
        assert budget.summary() == before
    # Read-only run custody requires neither the campaign DB nor generation SDK.
    path.unlink()
    assert report_openai(root)["states"]["uncertain"] == 1


def test_campaign_identity_frozen_in_approval_and_resume(tmp_path, policy, suite):
    with CampaignBudget.create(tmp_path / "one.db") as one:
        with CampaignBudget.create(tmp_path / "two.db") as two:
            root = tmp_path / "run"
            a = workflow.approval_scope(suite, root, policy, campaign=one)
            b = workflow.approval_scope(suite, root, policy, campaign=two)
            assert a["binding"] != b["binding"]
            assert a["scope"]["manifest"]["campaign"] == one.identity
            workflow.run_openai(
                suite, root, policy, transport=object(), campaign=one, max_steps=0
            )
            with pytest.raises(ValueError, match="campaign_identity_mismatch"):
                workflow.resume_openai(root, transport=object(), campaign=two)
            unscoped = tmp_path / "unscoped"
            with pytest.raises(ValueError, match="campaign_required"):
                workflow.run_openai(
                    suite, unscoped, policy, transport=object(), max_steps=0
                )
            assert not unscoped.exists()


def uncommitted_crash(path):
    with CampaignBudget.open(path) as budget:
        with budget._transaction():
            budget._connection.execute(
                "INSERT INTO reservations VALUES (?,?,?,?,?,?,?)",
                ("0" * 32, "a" * 64, "b" * 64, "c" * 64, "mock-a", "d" * 64, 10**14),
            )
            os._exit(19)


def test_hot_journal_recovers_without_manufacturing_reservation(tmp_path):
    path = tmp_path / "campaign.db"
    CampaignBudget.create(path).close()
    process = multiprocessing.get_context("spawn").Process(
        target=uncommitted_crash, args=(path,)
    )
    process.start()
    process.join(30)
    assert process.exitcode == 19
    with CampaignBudget.open(path) as budget:
        assert budget.summary()["reservation_count"] == 0


def test_sql_cannot_reset_delete_or_escalate_ceiling(tmp_path):
    import sqlite3

    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        for statement in (
            "UPDATE campaign SET ceiling=10000000000000000",
            "DELETE FROM campaign",
            "INSERT OR REPLACE INTO campaign VALUES (1,'other','USD',5000000000000000)",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                budget._connection.execute(statement)
        assert budget.summary()["ceiling_usd"] == "50.00"


def test_ambiguous_commit_acknowledgement_retains_reservation(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from decimal import Decimal

    from test_campaign import reservation

    path = tmp_path / "campaign.db"
    with CampaignBudget.create(path) as budget:
        original = budget._transaction

        @contextmanager
        def lost_acknowledgement(*args, **kwargs):
            with original(*args, **kwargs):
                yield
            raise OSError("synthetic acknowledgement loss after durable commit")

        with monkeypatch.context() as scoped:
            scoped.setattr(budget, "_transaction", lost_acknowledgement)
            with pytest.raises(OSError):
                budget.reserve(**reservation())
    with CampaignBudget.open(path) as budget:
        budget.reserve(**reservation())
        assert budget.summary()["reservation_count"] == 1
        assert Decimal(budget.summary()["reserved_usd"]) == Decimal("10")


def test_unknown_or_wrong_campaign_cannot_dispatch(tmp_path, policy, suite):
    with CampaignBudget.create(tmp_path / "campaign.db") as budget:
        data = policy.model_dump()
        data["currency"] = "EUR"
        with pytest.raises(ValueError, match="USD"):
            workflow.run_openai(
                suite, tmp_path / "bad", data, transport=object(), campaign=budget
            )
        assert not (tmp_path / "bad").exists()
