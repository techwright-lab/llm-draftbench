import json

import pytest
from test_judge_adapters import jev_native, verdict_json
from test_judge_panel import BRIEF, CANDIDATES, JUDGES

from draftbench.cli import main
from draftbench.judge import JudgeError
from draftbench.judge.panel import build_panel, write_panel
from draftbench.judge.report import bradley_terry, build_report, read_ledger
from draftbench.judge.run import preflight, run

pytest.importorskip("httpx")
pytest.importorskip("openai")
pytest.importorskip("anthropic")


def make_panel(tmp_path):
    panel, custody = build_panel(brief=BRIEF, candidates=CANDIDATES, judges=JUDGES)
    root = write_panel(tmp_path / "panel", panel, custody)
    return root, panel, custody


def transports(panel, *, jev_choice="a", fail_first=None):
    import httpx
    from replay_fixtures import openai_native, openai_usage

    by_presentation = {p["presentation_id"]: p for p in panel["presentations"]}
    state = {"jev": 0}

    def jev_handler(request):
        state["jev"] += 1
        if fail_first and state["jev"] <= fail_first:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json=jev_native(jev_choice))

    def oa_handler(request):
        body = json.loads(request.content)["input"][0]["content"]
        presentation = next(
            p
            for pid, p in by_presentation.items()
            if panel["candidates"][p["a"]]["body"] in body.split("## Draft B")[0]
            and panel["candidates"][p["b"]]["body"] in body.split("## Draft B")[1]
        )
        return httpx.Response(
            200,
            json=openai_native(
                "gpt-6.1-sol",
                text=verdict_json(panel, presentation, "a", "a"),
                usage=openai_usage(i=40, o=10),
            ),
        )

    def an_handler(request):
        body = json.loads(request.content)["messages"][0]["content"][0]["text"]
        presentation = next(
            p
            for pid, p in by_presentation.items()
            if panel["candidates"][p["a"]]["body"] in body.split("## Draft B")[0]
            and panel["candidates"][p["b"]]["body"] in body.split("## Draft B")[1]
        )
        return httpx.Response(
            200,
            json={
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {
                        "type": "text",
                        "text": verdict_json(panel, presentation, "b", "b"),
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 60,
                    "output_tokens": 12,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        )

    return {
        "jev": httpx.MockTransport(jev_handler),
        "oa": httpx.MockTransport(oa_handler),
        "an": httpx.MockTransport(an_handler),
    }


def test_run_resume_and_report(tmp_path):
    root, panel, custody = make_panel(tmp_path)
    with pytest.raises(JudgeError, match="approval_mismatch"):
        run(root, env_file=None, approve="wrong", transports=transports(panel))
    first = run(
        root,
        env_file=None,
        approve=panel["digest"],
        max_requests=4,
        transports=transports(panel, fail_first=1),
    )
    assert first["sent_this_invocation"] == 4 and first["completed_requests"] == 3
    assert first["halted"] == "max_requests" and first["complete"] is False
    rows = read_ledger(root)
    assert (
        len(rows) == 4
        and rows[0]["status"] == "uncertain"
        and rows[0]["error_code"] == "provider_http_error"
    )
    assert all("native_output" in r for r in rows)
    second = run(
        root, env_file=None, approve=panel["digest"], transports=transports(panel)
    )
    assert second["complete"] is True and second["completed_requests"] == 18
    assert second["sent_this_invocation"] == 15
    third = run(
        root, env_file=None, approve=panel["digest"], transports=transports(panel)
    )
    assert third["sent_this_invocation"] == 0
    assert preflight(root)["completed_requests"] == 18

    report = build_report(root, custody=custody)
    assert report["completed_requests"] == 18
    jev_tally = report["judges"]["jev"]["criteria"]["clarity"]
    # "a" every time means the judge followed position, never the candidate.
    assert (
        jev_tally["pairs_position_biased"] == 3 and jev_tally["pairs_consistent"] == 0
    )
    assert jev_tally["counts"] == {"a": 6, "b": 0, "tie": 0, "unknown": 0, "missing": 0}
    assert report["judges"]["jev"]["usage"]["estimated_usd"] == "0.0003024"
    assert report["judges"]["oa"]["usage"]["estimated_usd"] is None
    assert report["agreement"]["clarity"]["oa|an"] == {
        "compared": 6,
        "agree": 0,
        "rate": 0.0,
    }
    assert report["agreement"]["clarity"]["jev|oa"]["rate"] == 1.0
    assert set(report["candidates"].values()) == set(CANDIDATES)


def test_bradley_terry_orders_a_clear_winner():
    ids = ["x", "y", "z"]
    comparisons = (
        [("x", "y", 1.0)] * 4
        + [("x", "z", 1.0)] * 4
        + [("y", "z", 1.0)] * 3
        + [("z", "y", 1.0)]
    )
    strength = bradley_terry(ids, comparisons)
    assert strength["x"] > strength["y"] > strength["z"]
    assert abs(sum(strength.values())) < 1e-6


def test_cli_report_writes_json_and_markdown_once(tmp_path, capsys):
    root, panel, _ = make_panel(tmp_path)
    run(
        root,
        env_file=None,
        approve=panel["digest"],
        transports=transports(panel, jev_choice="tie"),
    )
    output = tmp_path / "report.json"
    assert main(["judge", "report", "--panel", str(root), "--output", str(output)]) == 0
    out = capsys.readouterr().out
    assert "model-one" in out and "Draft one" not in out
    assert output.with_suffix(".md").read_text().startswith("# Pairwise judge report")
    assert main(["judge", "report", "--panel", str(root), "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists"


def test_cli_run_refuses_without_valid_credentials(tmp_path, capsys):
    root, panel, _ = make_panel(tmp_path)
    env = tmp_path / "judge.env"
    env.write_text("DRAFTBENCH_TYPESAFE_API_KEY=abc\n")
    env.chmod(0o600)
    assert (
        main(
            [
                "judge",
                "run",
                "--panel",
                str(root),
                "--env-file",
                str(env),
                "--approve",
                panel["digest"],
                "--quiet-progress",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "judge_refused"
    assert not (root / "ledger.jsonl").exists()
