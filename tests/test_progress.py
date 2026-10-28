"""Progress rendering never writes private content or alters run control flow."""

import io

from draftbench.progress import terminal_progress


def test_progress_flushes_to_stderr_only(monkeypatch, capsys):
    times = iter([10.0, 12.5])
    monkeypatch.setattr("draftbench.progress.time.monotonic", lambda: next(times))
    terminal_progress()(
        {
            "model": "gpt-6-luna",
            "stage": "writer",
            "state": "running",
            "campaign": {"reservation_count": 0, "reserved_usd": "0"},
            "private_output": "PRIVATE CANARY",
        }
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2.5s" in captured.err
    assert "gpt-6-luna | writer | running" in captured.err
    assert "not billed cost" in captured.err
    assert "PRIVATE CANARY" not in captured.err


def test_closed_progress_pipe_does_not_interrupt_run(monkeypatch):
    class ClosedPipe(io.StringIO):
        def write(self, value):
            raise BrokenPipeError

    monkeypatch.setattr("draftbench.progress.sys.stderr", ClosedPipe())
    terminal_progress()(
        {
            "model": "gpt-6-luna",
            "stage": "writer",
            "state": "running",
            "campaign": {"reservation_count": 0, "reserved_usd": "0"},
        }
    )
