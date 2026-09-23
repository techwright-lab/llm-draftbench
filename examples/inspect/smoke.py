"""SDK-backed CLI journey with network sockets and DNS denied.

Usage: python examples/inspect/smoke.py SYNTHETIC_SUITE NEW_PRIVATE_DIRECTORY
Works with an installed wheel; does not depend on the checkout's Python sources.
Only anonymous local socketpair descriptors for asyncio wakeups are permitted.
"""

import contextlib
import io
import json
import socket
import sys
from pathlib import Path

import inspect_ai  # noqa: F401 -- load installed SDK before socket interception

from draftbench.cli import main
from draftbench.ledger import Ledger
from draftbench.reporting import read_report
from draftbench.workflow import _directory


def block_network():
    original = socket.socket

    class LocalSocket(original):
        def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
            if family != socket.AF_UNIX or fileno is None:
                raise AssertionError("network socket forbidden")
            super().__init__(family, type, proto, fileno=fileno)

        def connect(self, *args, **kwargs):
            raise AssertionError("connect forbidden")

        def connect_ex(self, *args, **kwargs):
            raise AssertionError("connect forbidden")

    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")

    socket.socket = LocalSocket
    socket.create_connection = denied
    socket.getaddrinfo = denied
    socket.gethostbyname = denied
    socket.gethostbyname_ex = denied


def call(args, expected=0):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = main([str(arg) for arg in args])
    assert status == expected, (args[0], status)
    return json.loads(output.getvalue())


def smoke(suite, destination):
    block_network()
    root = _directory(destination, create=True)
    run = root / "run"
    paused = call(
        [
            "run",
            suite,
            "--adapter",
            "inspect-fixture",
            "--output",
            run,
            "--max-steps",
            "1",
        ],
        3,
    )
    assert paused["attempt_count"] == 1
    completed = call(["resume", run])
    assert completed["complete"] and not completed["model_execution_performed"]
    with Ledger.open(run / "ledger.sqlite3", readonly=True) as ledger:
        before = ledger.events()
    assert call(["resume", run]) == completed
    call(["report", "prepare", run, "--output", root / "prepared"])
    call(["score", root / "prepared" / "bundle.json"])
    call(
        [
            "report",
            "render",
            "--run",
            run,
            "--bundle",
            root / "prepared" / "bundle.json",
            "--bindings",
            root / "prepared" / "bindings.json",
            "--output",
            root / "report",
        ]
    )
    call(["report", "replay", root / "report", "--output", root / "replay"])
    assert read_report(root / "report") == read_report(root / "replay")
    with Ledger.open(run / "ledger.sqlite3", readonly=True) as ledger:
        assert ledger.events() == before
    print(
        json.dumps(
            {
                "synthetic": True,
                "model_execution_performed": False,
                "adapter": completed["adapter"],
                "attempt_count": completed["attempt_count"],
                "completed": completed["states"]["completed"],
                "replay_equal": True,
                "network_sockets_blocked": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    smoke(Path(sys.argv[1]), Path(sys.argv[2]))
