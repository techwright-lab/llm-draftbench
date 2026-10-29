import json

import pytest

from draftbench.cli import main
from draftbench.judge import JudgeError
from draftbench.judge.panel import build_panel, read_panel, write_panel

JUDGES = [
    {"judge_id": "jev", "kind": "typesafe", "model": "jev-1.14.0"},
    {"judge_id": "oa", "kind": "openai", "model": "gpt-6.1-sol"},
    {"judge_id": "an", "kind": "anthropic", "model": "claude-sonnet-5-5"},
]
GOOGLE = {"judge_id": "go", "kind": "google", "model": "gemini-4-argon"}
BRIEF = "## Approved Brand Brief\n- Title: Synthetic brief\n"
CANDIDATES = {
    "model-one": "Draft one body. It has a second sentence.",
    "model-two": "Draft two body. It differs here.",
    "model-three": "Draft three body. Another text entirely.",
}


def test_google_judge_defaults():
    panel, _ = build_panel(brief=BRIEF, candidates=CANDIDATES, judges=[GOOGLE])
    assert panel["judges"] == [
        {
            "judge_id": "go",
            "kind": "google",
            "model": "gemini-4-argon",
            "thinking_level": "low",
            "max_output_tokens": 8000,
        }
    ]


def test_panel_is_blinded_and_deterministic():
    panel, custody = build_panel(brief=BRIEF, candidates=CANDIDATES, judges=JUDGES)
    again, _ = build_panel(
        brief=BRIEF, candidates=dict(reversed(CANDIDATES.items())), judges=JUDGES
    )
    assert panel["digest"] == again["digest"]
    assert "model-one" not in json.dumps(panel)
    assert set(custody["candidates"].values()) == set(CANDIDATES)
    assert len(panel["presentations"]) == 6
    ids = sorted(panel["candidates"])
    assert [p["presentation_id"] for p in panel["presentations"][:2]] == [
        f"{ids[0]}-{ids[1]}",
        f"{ids[1]}-{ids[0]}",
    ]
    assert panel["judges"][1]["reasoning_effort"] == "low"
    assert panel["judges"][2]["max_tokens"] == 4000


@pytest.mark.parametrize(
    "candidates, judges, code",
    [
        ({"only": "one"}, JUDGES, "invalid_candidates"),
        ({"a": "same", "b": "same"}, JUDGES, "duplicate_candidate"),
        ({"bad name": "x", "b": "y"}, JUDGES, "invalid_candidate_name"),
        (
            CANDIDATES,
            [{"judge_id": "x", "kind": "other", "model": "m"}],
            "invalid_judges",
        ),
        (
            CANDIDATES,
            [{"judge_id": "x", "kind": "openai", "model": "m", "extra": 1}],
            "invalid_judges",
        ),
        (
            CANDIDATES,
            [
                {"judge_id": "x", "kind": "typesafe", "model": "m"},
                {"judge_id": "x", "kind": "typesafe", "model": "m"},
            ],
            "invalid_judges",
        ),
    ],
)
def test_panel_rejects_bad_inputs(candidates, judges, code):
    with pytest.raises(JudgeError, match=code):
        build_panel(brief=BRIEF, candidates=candidates, judges=judges)


def test_write_and_read_panel_round_trip(tmp_path):
    panel, custody = build_panel(brief=BRIEF, candidates=CANDIDATES, judges=JUDGES)
    root = write_panel(tmp_path / "panel", panel, custody)
    assert (root / "panel.json").stat().st_mode & 0o777 == 0o600
    _, loaded = read_panel(root)
    assert loaded == panel
    (root / "panel.json").write_text(json.dumps({**panel, "brief": "tampered"}))
    with pytest.raises(JudgeError, match="panel_digest_mismatch"):
        read_panel(root)


def test_cli_prepare_is_offline_and_echoes_no_content(tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF)
    args = [
        "judge",
        "prepare",
        "--brief",
        str(brief),
        "--judges",
        str(tmp_path / "judges.json"),
        "--output",
        str(tmp_path / "panel"),
    ]
    (tmp_path / "judges.json").write_text(json.dumps(JUDGES))
    for name, body in CANDIDATES.items():
        path = tmp_path / f"{name}.md"
        path.write_text(body)
        args += ["--candidate", f"{name}={path}"]
    assert main(args) == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["planned_requests"] == 18 and summary["candidate_count"] == 3
    assert "Draft one" not in out and "model-one" not in out
    assert main(["judge", "preflight", "--panel", str(tmp_path / "panel")]) == 0
    assert json.loads(capsys.readouterr().out)["approval_digest"] == summary["digest"]
