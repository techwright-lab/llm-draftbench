"""Synthetic native-replay wire fixtures shared by provider tests."""

import json
from pathlib import Path

from draftbench.adapters.replay_contract import (
    REPLAY_RESPONSE_FORMAT,
    replay_prompt,
    sha256_text,
)
from draftbench.identity import strict_json_loads

SUITE = Path(__file__).parents[1] / "examples/replay/suite.json"
SIDECAR_CANARY = "Synthetic private sidecar canary"
SYSTEM = "Synthetic system instructions."
USER = "Synthetic user request."
PROMPT = replay_prompt(
    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
    "SeoContentSchema",
)
ITEMS = ("promise_not_delivered", "unrun_protocol", "self_contradiction")


def case():
    return strict_json_loads(
        (SUITE.parent / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )


def draft_json(body="Synthetic fixture body."):
    return json.dumps(
        {
            "title": "Synthetic title",
            "body": body,
            "meta_title": "Synthetic meta title",
            "meta_description": "Synthetic meta description.",
            "tags": ["synthetic"],
        }
    )


def review_json(ids=ITEMS):
    return json.dumps(
        {
            "results": [
                {
                    "item_id": item,
                    "state": "pass",
                    "evidence": "",
                    "detail": "",
                    "owner": "none",
                }
                for item in ids
            ],
            "factual_issues": [],
        }
    )


def openai_native(model, text="Synthetic fixture text.", usage=None):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [
            {"id": "rs_fixture", "type": "reasoning", "summary": []},
            {
                "id": "msg_fixture",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "usage": usage,
    }


def openai_usage(i=10, o=8, cached=0, reasoning=5):
    return {
        "input_tokens": i,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": o,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": i + o,
    }


def output_text(wire):
    """Pick a schema-valid synthetic output for the schema a request names."""
    if "text" in wire:
        reviewer = wire["text"]["format"]["name"] == "ReviewLedgerSchema"
    else:
        reviewer = "results" in wire["output_config"]["format"]["schema"]["properties"]
    return review_json() if reviewer else draft_json()


def review_replay(draft_output, review_output, user="Synthetic TG revision prompt."):
    """A TG no-write review replay response bound to one lab chain."""
    revision = case()["generator"]["revision"]["input"]["messages"]
    return {
        "format": REPLAY_RESPONSE_FORMAT,
        "case_id": case()["case_id"],
        "tg_revision": "19370494",
        "draft_sha256": sha256_text(draft_output),
        "review_sha256": sha256_text(review_output),
        "review": {"verdict": "synthetic"},
        "revision": {
            "messages": [
                {"role": "system", "content": revision[0]["content"]},
                {"role": "user", "content": user},
            ]
        },
    }


def replay_for_run(run_dir):
    from draftbench.provider_reporting import snapshot_provider

    rows = snapshot_provider(run_dir)["work"]
    return review_replay(rows[0]["result"]["output"], rows[1]["result"]["output"])
