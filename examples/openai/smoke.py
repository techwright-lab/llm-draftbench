"""Installed-package, synthetic-only OpenAI SDK smoke. No network or credentials."""

import contextlib
import io
import json
import socket
import ssl  # noqa: F401 -- load socket subclass before the network guard
import sys
from pathlib import Path

from draftbench.cli import main


def denied(*args, **kwargs):
    raise AssertionError("network forbidden")


socket.socket = denied
socket.create_connection = denied
socket.getaddrinfo = denied
suite, root = Path(sys.argv[1]), Path(sys.argv[2])
root.mkdir(mode=0o700)
policy = {
    "model": "gpt-4.1-2025-04-14",
    "account_route": "fixture-only",
    "organization": "org_fixture",
    "project": "proj_fixture",
    "currency": "XXX",
    "input_per_million": "1",
    "output_per_million": "1",
    "max_cost": "10",
    "context_window_tokens": 1047576,
    "max_output_tokens": 100,
    "max_requests": 3,
    "max_total_tokens": 4000000,
}
(root / "policy.json").write_text(json.dumps(policy))
(root / "rights.json").write_text(
    json.dumps(
        {
            "license": "CC0-1.0",
            "usage": "private",
            "authorization": "Synthetic wire fixture only, not rights to provider outputs.",
        }
    )
)


def cli(*args, expected=0):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = main(list(map(str, args)))
    assert status == expected, (status, args)
    return json.loads(out.getvalue())


run = root / "run"
cli(
    "openai",
    "fixture-run",
    suite,
    "--policy",
    root / "policy.json",
    "--output",
    run,
    "--max-steps",
    "1",
    expected=3,
)
final = cli("openai", "fixture-resume", run)
assert final["complete"] and final["attempt_count"] == 3
assert final["provenance"] == "fixture" and final["model_execution_performed"] is False
assert cli("openai", "report", run) == final
cli(
    "openai",
    "prepare",
    run,
    "--rights",
    root / "rights.json",
    "--output",
    root / "prepared",
)
score = cli("score", root / "prepared/bundle.json")
assert score["purpose"] == "synthetic_infrastructure"
cli(
    "report",
    "render",
    "--provider-run",
    run,
    "--bindings",
    root / "prepared/provider-bindings.json",
    "--bundle",
    root / "prepared/bundle.json",
    "--output",
    root / "report",
)
cli("report", "replay", root / "report", "--output", root / "replay")
assert (root / "report/report.json").read_bytes() == (
    root / "replay/report.json"
).read_bytes()
print(
    json.dumps(
        {
            "provenance": "fixture",
            "completed_attempts": 3,
            "score_cases": score["case_count"],
            "replay_equal": True,
            "model_execution_performed": False,
        }
    )
)
