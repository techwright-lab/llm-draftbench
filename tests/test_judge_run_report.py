import json

import pytest
from test_judge_adapters import jev_native, verdict_json
from test_judge_panel import BRIEF, CANDIDATES, GOOGLE, JUDGES

from draftbench.cli import main
from draftbench.judge import JudgeError
from draftbench.judge.panel import build_panel, write_panel
from draftbench.judge.report import (
    bradley_terry,
    build_report,
    read_ledger,
    render_markdown,
)
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


def test_pointwise_run_scores_each_draft_and_reports_rank_agreement(tmp_path):
    import httpx
    from test_judge_adapters import jev_score_native

    root, panel, custody = make_panel(tmp_path)
    ids = sorted(panel["candidates"])
    level_by_draft = {
        panel["candidates"][cid]["body"]: level for cid, level in zip(ids, (3, 1, 2))
    }

    def handler(request):
        body = json.loads(request.content)
        assert set(body["state"]) == {"brief", "draft"}
        return httpx.Response(
            200, json=jev_score_native(level_by_draft[body["state"]["draft"]])
        )

    fixtures = {"jev": httpx.MockTransport(handler)}
    with pytest.raises(JudgeError, match="invalid_mode"):
        run(
            root,
            env_file=None,
            approve=panel["digest"],
            transports=fixtures,
            mode="pointwise",
            reps=0,
            judges=["jev"],
        )
    result = run(
        root,
        env_file=None,
        approve=panel["digest"],
        transports=fixtures,
        mode="pointwise",
        reps=2,
        judges=["jev"],
    )
    assert result["mode"] == "pointwise" and result["judges"] == ["jev"]
    assert result["planned_requests"] == 6 and result["complete"] is True
    assert (
        run(
            root,
            env_file=None,
            approve=panel["digest"],
            transports=fixtures,
            mode="pointwise",
            reps=2,
            judges=["jev"],
        )["sent_this_invocation"]
        == 0
    )

    run(
        root,
        env_file=None,
        approve=panel["digest"],
        transports=transports(panel),
        judges=["jev"],
    )
    report = build_report(root, custody=custody)
    pointwise = report["judges"]["jev"]["pointwise"]["clarity"]
    assert pointwise[ids[0]] == {"mean": 3.0, "min": 3.0, "max": 3.0, "reps": 2}
    assert pointwise[ids[1]]["mean"] == 1.0
    assert report["judges"]["oa"]["pointwise"] is None
    assert "jev:pointwise|jev:pairwise|clarity" in report["rank_agreement"]
    assert report["judges"]["jev"]["criteria"]["clarity"]["counts"]["a"] == 6


def blind_panel(tmp_path, name, *, favourite):
    candidates = {
        "gpt-6.1-sol": f"Sol body for {name}. One sentence.",
        "claude-opus-5-5": f"Opus body for {name}. Another sentence.",
        "claude-haiku-5-5": f"Haiku body for {name}. Third sentence.",
    }
    panel, custody = build_panel(
        brief=BRIEF, candidates=candidates, judges=JUDGES + [GOOGLE]
    )
    root = write_panel(tmp_path / name, panel, custody)
    best = {n: cid for cid, n in custody["candidates"].items()}[favourite]
    rows = []
    for p in panel["presentations"]:
        for judge in ("jev", "oa", "an", "go"):
            choice = "a" if best not in (p["a"], p["b"]) or p["a"] == best else "b"
            rows.append(
                {
                    "key": f"{p['presentation_id']}:{judge}",
                    "judge_id": judge,
                    "status": "success",
                    "verdicts": {
                        c["criterion_id"]: {"choice": choice} for c in panel["criteria"]
                    },
                }
            )
    (root / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return root, custody, best


def test_family_blind_drops_own_family_verdicts_and_pools_the_rest(tmp_path):
    root, custody, sol = blind_panel(tmp_path, "one", favourite="gpt-6.1-sol")
    report = build_report(root, custody=custody)
    oa = report["judges"]["oa"]["family_blind"]["clarity"]
    assert sol not in oa["strength"] and oa["scored_presentations"] == 2
    an = report["judges"]["an"]["family_blind"]["clarity"]
    assert an["strength"] is None and an["counts"]["missing"] == 0
    assert (
        report["judges"]["go"]["family_blind"]["clarity"]["scored_presentations"] == 6
    )
    assert report["judges"]["jev"]["family_blind"] is None

    consensus = report["family_blind_consensus"]
    assert consensus["judges"] == ["an", "go", "oa"]
    clarity = consensus["criteria"]["clarity"]
    assert clarity["scored_presentations"] == 8
    assert max(clarity["strength"], key=clarity["strength"].get) == sol
    assert "Family-blind consensus" in render_markdown(report)


def test_family_blind_needs_custody(tmp_path):
    root, panel, _ = make_panel(tmp_path)
    report = build_report(root)
    assert report["family_blind_consensus"] is None
    assert all(j["family_blind"] is None for j in report["judges"].values())


def test_combine_pools_cases_by_model_name_and_reports_case_agreement(tmp_path, capsys):
    one, _, _ = blind_panel(tmp_path, "one", favourite="gpt-6.1-sol")
    two, _, _ = blind_panel(tmp_path, "two", favourite="gpt-6.1-sol")
    three, _, _ = blind_panel(tmp_path, "three", favourite="claude-opus-5-5")
    output = tmp_path / "combined.json"
    code = main(
        [
            "judge",
            "combine",
            "--panel",
            str(one),
            "--panel",
            str(two),
            "--panel",
            str(three),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    combined = json.loads(output.read_text())
    assert combined["models"] == sorted(
        ["gpt-6.1-sol", "claude-opus-5-5", "claude-haiku-5-5"]
    )
    clarity = combined["criteria"]["clarity"]
    assert clarity["scored_presentations"] == 24
    assert max(clarity["strength"], key=clarity["strength"].get) == "gpt-6.1-sol"
    agreement = list(clarity["case_rank_agreement"].values())
    assert len(agreement) == 3 and max(agreement) == 1.0 and min(agreement) < 1.0
    assert "across cases" in output.with_suffix(".md").read_text()
    assert main(["judge", "combine", "--panel", str(one), "--output", str(output)]) == 2
    assert "two_panels_required" in capsys.readouterr().err
