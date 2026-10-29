import json

import pytest
from replay_fixtures import openai_native, openai_usage
from test_judge_adapters import google_native, jev_score_native
from test_judge_panel import GOOGLE, JUDGES

from draftbench.cli import main
from draftbench.judge import JudgeError, generative
from draftbench.judge.criteria import BRIEF_CRITERIA
from draftbench.judge.panel import (
    build_brief_panel,
    load_corpus_briefs,
    write_panel,
)
from draftbench.judge.report import build_report
from draftbench.judge.run import run

pytest.importorskip("httpx")
pytest.importorskip("openai")
pytest.importorskip("anthropic")

BRIEFS = {
    "draft-1": "- Target word count: 1700\nTarget article length: 900 words.",
    "draft-2": "- Target word count: 1500\nWrite about one thing for founders.",
}


def score_json(panel, cid, *, level="2", quote=None):
    body = panel["candidates"][cid]["body"]
    return json.dumps(
        {
            "scores": [
                {
                    "criterion": item["criterion_id"],
                    "level": level,
                    "evidence": quote or body.splitlines()[0],
                    "reason": "Synthetic.",
                }
                for item in panel["criteria"]
            ]
        }
    )


def write_corpus(path, briefs):
    lines = []
    for case_id, text in briefs.items():
        case = {
            "case_id": case_id,
            "generator": {
                "writer": {
                    "input": {
                        "messages": [
                            {"role": "system", "content": "System."},
                            {"role": "user", "content": text},
                        ]
                    }
                }
            },
        }
        lines.append(json.dumps(case) + "\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_brief_panel_is_pointwise_only_with_brief_criteria():
    panel, custody = build_brief_panel(briefs=BRIEFS, judges=JUDGES)
    assert panel["subject"] == "brief" and panel["presentations"] == []
    assert [c["criterion_id"] for c in panel["criteria"]] == [
        c["criterion_id"] for c in BRIEF_CRITERIA
    ]
    assert set(custody["candidates"].values()) == set(BRIEFS)


def test_generative_score_request_shapes():
    panel, _ = build_brief_panel(briefs=BRIEFS, judges=JUDGES + [GOOGLE])
    cid = sorted(panel["candidates"])[0]
    oa, an, go = (j for j in panel["judges"] if j["kind"] != "typesafe")
    openai_request = generative.native_score_request(oa, panel, cid)
    assert openai_request["instructions"].startswith("You score the input a writer")
    assert openai_request["text"]["format"]["name"] == "PointwiseScoreSchema"
    assert "## Text under review" in openai_request["input"][0]["content"]
    assert "  3: Every part agrees" in openai_request["input"][0]["content"]
    level = openai_request["text"]["format"]["schema"]["properties"]["scores"]
    assert level["items"]["properties"]["level"]["enum"] == ["0", "1", "2", "3"]
    anthropic_request = generative.native_score_request(an, panel, cid)
    assert anthropic_request["system"][0]["text"].startswith("You score the input")
    google_request = generative.native_score_request(go, panel, cid)
    assert google_request["config"]["system_instruction"].startswith("You score")


def test_google_score_parses_and_paraphrase_is_unknown():
    import httpx

    pytest.importorskip("google.genai")
    panel, _ = build_brief_panel(briefs=BRIEFS, judges=[GOOGLE])
    cid = sorted(panel["candidates"])[0]
    good = generative.invoke_score(
        panel["judges"][0],
        panel,
        cid,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=google_native(score_json(panel, cid, level="1"))
            )
        ),
    )
    assert good["status"] == "success", good.get("error_code")
    assert good["verdicts"]["brief_consistency"]["score"] == 1.0
    paraphrase = generative.invoke_score(
        panel["judges"][0],
        panel,
        cid,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=google_native(score_json(panel, cid, quote="Not in it."))
            )
        ),
    )
    assert paraphrase["verdicts"]["brief_focus"]["score"] is None
    assert paraphrase["verdicts"]["brief_focus"]["evidence_found"] is False


def test_brief_run_scores_every_judge_and_reports_unknowns(tmp_path):
    import httpx

    panel, custody = build_brief_panel(briefs=BRIEFS, judges=JUDGES)
    root = write_panel(tmp_path / "briefs", panel, custody)
    by_body = {panel["candidates"][cid]["body"]: cid for cid in panel["candidates"]}
    first = sorted(panel["candidates"])[0]

    def openai_handler(request):
        text = json.loads(request.content)["input"][0]["content"]
        cid = next(c for body, c in by_body.items() if body in text)
        quote = None if cid == first else "Paraphrase only."
        return httpx.Response(
            200,
            json=openai_native(
                "gpt-6.1-sol",
                text=score_json(panel, cid, level="3", quote=quote),
                usage=openai_usage(i=40, o=10),
            ),
        )

    def jev_handler(request):
        state = json.loads(request.content)["state"]
        assert set(state) == {"writer_constraints", "writer_input"}
        return httpx.Response(
            200,
            json=jev_score_native(1, [c["criterion_id"] for c in BRIEF_CRITERIA]),
        )

    fixtures = {
        "jev": httpx.MockTransport(jev_handler),
        "oa": httpx.MockTransport(openai_handler),
    }
    with pytest.raises(JudgeError, match="pointwise_only_panel"):
        run(root, env_file=None, approve=panel["digest"], transports=fixtures)
    result = run(
        root,
        env_file=None,
        approve=panel["digest"],
        transports=fixtures,
        judges=["jev", "oa"],
        mode="pointwise",
    )
    assert result["complete"] is True and result["planned_requests"] == 4
    report = build_report(root, custody=custody)
    assert report["subject"] == "brief"
    scores = report["judges"]["oa"]["pointwise"]["brief_satisfiable"]
    assert scores[first]["mean"] == 3.0
    other = next(cid for cid in panel["candidates"] if cid != first)
    assert scores[other] == {"unknown": 1}
    jev_scores = report["judges"]["jev"]["pointwise"]["brief_latitude"]
    assert jev_scores[first]["mean"] == 1.0


def test_load_corpus_briefs_and_cli_prepare(tmp_path, capsys):
    corpus = tmp_path / "cases.jsonl"
    write_corpus(corpus, BRIEFS)
    assert load_corpus_briefs(corpus, case_ids=["draft-2"]) == {
        "draft-2": BRIEFS["draft-2"]
    }
    with pytest.raises(JudgeError, match="unknown_case"):
        load_corpus_briefs(corpus, case_ids=["draft-9"])
    judges = tmp_path / "judges.json"
    judges.write_text(json.dumps(JUDGES), encoding="utf-8")
    output = tmp_path / "panel"
    code = main(
        [
            "judge",
            "prepare",
            "--corpus",
            str(corpus),
            "--judges",
            str(judges),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["candidate_count"] == 2 and summary["presentation_count"] == 0
    assert summary["subject"] == "brief" and summary["planned_requests"] == 6
    assert summary["criteria"][0] == "brief_consistency"
    assert (
        main(
            [
                "judge",
                "prepare",
                "--case",
                "draft-1",
                "--judges",
                str(judges),
                "--output",
                str(tmp_path / "other"),
            ]
        )
        == 2
    )
