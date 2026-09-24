"""Installed-wheel CLI smoke: one shared synthetic campaign, sockets denied."""

import contextlib
import io
import json
import socket
import ssl  # noqa: F401 -- initialize before socket guard
import sys
from decimal import Decimal
from pathlib import Path

from draftbench.adapters.replay_contract import REPLAY_RESPONSE_FORMAT, sha256_text
from draftbench.campaign import CampaignBudget
from draftbench.cli import main
from draftbench.identity import strict_json_loads
from draftbench.provider_reporting import snapshot_provider


def denied(*args, **kwargs):
    raise AssertionError("network forbidden")


socket.socket = denied
socket.create_connection = denied
socket.getaddrinfo = denied
suite, root = map(Path, sys.argv[1:])
root.mkdir(mode=0o700)
campaign_path = root / "campaign.sqlite3"
with CampaignBudget.create(campaign_path):
    pass
models = (
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "claude-opus-5-5",
    "claude-sonnet-5",
)
rights = root / "rights.json"
rights.write_text(
    json.dumps(
        {
            "license": "CC0-1.0",
            "usage": "private",
            "authorization": "Synthetic wire fixtures only; not provider output rights.",
        }
    )
)


def review_replay(run):
    """Synthetic stand-in for the TG review replay response file."""
    rows = snapshot_provider(run)["work"]
    case = strict_json_loads(
        (suite.parent / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    system = case["generator"]["revision"]["input"]["messages"][0]["content"]
    path = root / (run.name + "-review-replay.json")
    path.write_text(
        json.dumps(
            {
                "format": REPLAY_RESPONSE_FORMAT,
                "case_id": case["case_id"],
                "tg_revision": "19370494",
                "draft_sha256": sha256_text(rows[0]["result"]["output"]),
                "review_sha256": sha256_text(rows[1]["result"]["output"]),
                "review": {"verdict": "synthetic"},
                "revision": {
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "Synthetic revision prompt."},
                    ]
                },
            }
        )
    )
    return path


def cli(*args, allowed=(0,)):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(list(map(str, args)))
    assert code in allowed, (args, code)
    return json.loads(output.getvalue())


for model in models:
    provider = "openai" if model.startswith("gpt") else "anthropic"
    policy = dict(
        model=model,
        account_route="fixture-only",
        currency="USD",
        max_cost="50",
        max_output_tokens=100,
        max_requests=4,
        max_total_tokens=4100000,
        pricing_provenance="public-docs-2026-09-23-v1",
    )
    if provider == "openai":
        policy.update(
            contract="openai-gpt6-text-v1",
            organization="org_fixture",
            project="proj_fixture",
            reasoning_effort="medium",
        )
    else:
        policy.update(
            contract="anthropic-messages-text-v1",
            effort="medium" if "opus" in model else "high",
        )
    policy_path = root / (model + ".json")
    policy_path.write_text(json.dumps(policy))
    first = cli(
        provider,
        "fixture-run",
        suite,
        "--policy",
        policy_path,
        "--output",
        root / model,
        "--max-steps",
        "1",
        "--campaign",
        campaign_path,
        allowed=(3,),
    )
    assert first["states"]["completed"] == 1

# Let cheaper models progress first. Exhaustion is a successful safety outcome,
# not a fabricated completion; all five have genuine mocked SDK artifacts.
for model in reversed(models):
    provider = "openai" if model.startswith("gpt") else "anthropic"
    run = root / model
    final = cli(
        provider, "fixture-resume", run, "--campaign", campaign_path, allowed=(0, 3)
    )
    if [row["state"] for row in snapshot_provider(run)["work"]][1:3] == [
        "completed",
        "planned",
    ]:
        final = cli(
            provider,
            "fixture-resume",
            run,
            "--campaign",
            campaign_path,
            "--revision-input",
            review_replay(run),
            allowed=(0, 3),
        )
    assert not final["model_execution_performed"]
    assert cli(provider, "report", run) == final
    prep = root / (model + "-prepared")
    cli(provider, "prepare", run, "--rights", rights, "--output", prep)
    cli(
        "report",
        "render",
        "--provider-run",
        run,
        "--bundle",
        prep / "bundle.json",
        "--bindings",
        prep / "provider-bindings.json",
        "--output",
        root / (model + "-report"),
    )
    cli(
        "report",
        "replay",
        root / (model + "-report"),
        "--output",
        root / (model + "-replay"),
    )
    assert (root / (model + "-report") / "report.json").read_bytes() == (
        root / (model + "-replay") / "report.json"
    ).read_bytes()

with CampaignBudget.open(campaign_path) as campaign:
    summary = campaign.summary()
    assert Decimal(summary["reserved_usd"]) <= Decimal("50")
print(
    json.dumps(
        {
            "models": list(models),
            "campaign": summary,
            "model_execution_performed": False,
            "replay_equal": True,
            "socket_access": False,
        }
    )
)
