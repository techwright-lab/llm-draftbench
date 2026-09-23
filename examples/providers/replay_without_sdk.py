"""Run with a core-only installed wheel against smoke.py's saved fixture run."""

import contextlib
import importlib.abc
import importlib.util
import io
import json
import socket
import ssl  # noqa: F401 -- load socket subclass before the network guard
import sys
from pathlib import Path

assert importlib.util.find_spec("openai") is None
assert importlib.util.find_spec("anthropic") is None
assert importlib.util.find_spec("httpx") is None


class NoGeneration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            "openai",
            "anthropic",
            "draftbench.adapters.anthropic",
            "httpx",
            "draftbench.adapters.openai",
            "draftbench.provider_workflow",
        }:
            raise AssertionError("generation import forbidden")


def denied(*args, **kwargs):
    raise AssertionError("network forbidden")


sys.meta_path.insert(0, NoGeneration())
socket.socket = denied
socket.create_connection = denied
socket.getaddrinfo = denied

from draftbench.cli import main  # noqa: E402

source, target = map(Path, sys.argv[1:3])
model = sys.argv[3]
provider = "openai" if model.startswith("gpt") else "anthropic"
target.mkdir(mode=0o700)
run = source / model
before = {
    p.relative_to(run): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
    for p in run.rglob("*")
    if p.is_file()
}


def cli(*args):
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(list(map(str, args))) == 0


cli(provider, "report", run)
cli(
    provider,
    "prepare",
    run,
    "--rights",
    source / "rights.json",
    "--output",
    target / "prepared",
)
cli(
    "report",
    "render",
    "--provider-run",
    run,
    "--bundle",
    target / "prepared/bundle.json",
    "--bindings",
    target / "prepared/provider-bindings.json",
    "--output",
    target / "report",
)
cli("report", "replay", target / "report", "--output", target / "replay")
assert (target / "report/report.json").read_bytes() == (
    target / "replay/report.json"
).read_bytes()
assert before == {
    p.relative_to(run): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
    for p in run.rglob("*")
    if p.is_file()
}
report = json.loads((target / "report/report.json").read_text())
assert report["custody"] == "verified_provider_preparation"
assert report["provenance"] == "fixture"
assert report["model_execution_performed"] is False
print(
    json.dumps(
        {
            "sdk_absent": True,
            "generation_imports": False,
            "custody": report["custody"],
            "replay_equal": True,
            "run_unchanged": True,
        }
    )
)
