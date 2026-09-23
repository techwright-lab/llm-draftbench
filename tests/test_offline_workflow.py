import copy
import json
import subprocess
import sys

import pytest
from conftest import seal

from draftbench.adapters.fake import AdapterLimited, FakeAdapter
from draftbench.ledger import Ledger
from draftbench.store import ArtifactStore
from draftbench.workflow import RunError, resume_synthetic, run_synthetic


@pytest.fixture
def smoke_suite(make_suite, case_data):
    packet = copy.deepcopy(case_data["generator"]["writer"])
    case_data["generator"]["reviewer"] = copy.deepcopy(packet)
    case_data["generator"]["revision"] = copy.deepcopy(packet)
    return make_suite([seal(case_data)])


def ledger_at(directory):
    return Ledger.open(directory / "ledger.sqlite3")


def test_complete_chain_preserves_exact_parents_and_does_not_repeat(
    smoke_suite, tmp_path, monkeypatch
):
    calls = []
    invoke = FakeAdapter.invoke

    def counted(self, request):
        calls.append(copy.deepcopy(request))
        return invoke(self, request)

    monkeypatch.setattr(FakeAdapter, "invoke", counted)
    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out)
    assert result["states"]["completed"] == result["work_count"] == 3
    assert result["attempt_count"] == 3
    assert result["model_execution_performed"] is False
    assert result["synthetic_execution_performed"] is True
    assert [call["role"] for call in calls] == ["writer", "reviewer", "revision"]
    assert all("evaluator" not in json.dumps(call) for call in calls)
    with ledger_at(out) as ledger:
        plan = ledger.plan()
        assert len(plan) == 3
        states = [ledger.state(work["work_id"]) for work in plan]
        store = ArtifactStore(out / "objects")
        output = [store.get_json(state["result_digest"]) for state in states]
        assert output[1]["parent_digests"] == {"draft": states[0]["result_digest"]}
        assert output[2]["parent_digests"] == {
            "draft": states[0]["result_digest"],
            "review": states[1]["result_digest"],
        }
        assert len(output[0]["units"]) == len(output[2]["units"]) == 2
        assert output[2]["units"][0]["unit_id"] == output[0]["units"][0]["unit_id"]
        events = ledger.events()
    assert resume_synthetic(out)["states"]["completed"] == 3
    assert len(calls) == 3
    with ledger_at(out) as ledger:
        assert ledger.events() == events


def test_all_planned_work_durable_before_first_dispatch(smoke_suite, tmp_path):
    out = tmp_path / "run"
    seen = []

    def checkpoint(name, context):
        if name == "reserved":
            with ledger_at(out) as ledger:
                assert len(ledger.plan()) == 3
                assert ledger.summary()["work_count"] == 3
                seen.append(name)

    run_synthetic(smoke_suite, out, checkpoint=checkpoint)
    assert len(seen) == 3


@pytest.mark.parametrize(
    "stage,expected_attempts,completed,uncertain",
    [
        ("reserved", 3, 3, 0),
        ("in_flight", 1, 0, 1),
        ("artifact_saved", 1, 0, 1),
        ("result_saved", 3, 3, 0),
        ("completed", 3, 3, 0),
    ],
)
def test_real_process_death_and_resume(
    smoke_suite, tmp_path, stage, expected_attempts, completed, uncertain
):
    out = tmp_path / "run"
    script = """
import os, socket, sys
from draftbench.workflow import run_synthetic

def deny(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.socket = socket.create_connection = socket.getaddrinfo = deny

def kill(name, context):
    if name == sys.argv[3]:
        os._exit(71)
run_synthetic(sys.argv[1], sys.argv[2], checkpoint=kill)
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(smoke_suite), str(out), stage],
        capture_output=True,
        timeout=30,
    )
    assert killed.returncode == 71, killed.stderr.decode()
    summary = resume_synthetic(out)
    assert summary["attempt_count"] == expected_attempts
    assert summary["states"]["completed"] == completed
    assert summary["states"]["uncertain"] == uncertain
    assert sum(summary["states"].values()) == 3
    if uncertain:
        assert summary["states"]["blocked"] == 2
        assert summary["synthetic_execution_performed"] is None
    else:
        assert summary["synthetic_execution_performed"] is True
    assert resume_synthetic(out) == summary


def test_resume_does_not_require_original_suite_files(smoke_suite, tmp_path):
    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out, max_steps=1)
    assert result["states"]["planned"] == 2
    for name in ("suite.json", "cases.jsonl", "source.txt"):
        (smoke_suite.parent / name).unlink()
    assert resume_synthetic(out)["states"]["completed"] == 3


@pytest.mark.parametrize("marker_kind", ["directory", "worktree_file"])
def test_resume_rejects_run_that_became_git_root(smoke_suite, tmp_path, marker_kind):
    out = tmp_path / "run"
    run_synthetic(smoke_suite, out, max_steps=0)
    marker = out / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /not-an-export-destination\n")
    with ledger_at(out) as ledger:
        before = ledger.events()
    with pytest.raises(RunError, match="run_inside_git"):
        resume_synthetic(out)
    with ledger_at(out) as ledger:
        assert ledger.events() == before


def test_unstarted_summary_does_not_claim_execution(smoke_suite, tmp_path):
    assert (
        run_synthetic(smoke_suite, tmp_path / "run", max_steps=0)[
            "synthetic_execution_performed"
        ]
        is False
    )


@pytest.mark.parametrize("limited", [False, True])
def test_failed_and_limited_attempts_remain_in_denominator(
    smoke_suite, tmp_path, monkeypatch, limited
):
    def fail(self, request):
        raise AdapterLimited() if limited else RuntimeError("PRIVATE-ADAPTER-FAILURE")

    monkeypatch.setattr(FakeAdapter, "invoke", fail)
    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out)
    assert result["states"]["limited" if limited else "failed"] == 1
    assert result["states"]["blocked"] == 2
    assert result["attempt_count"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert resume_synthetic(out) == result


def test_missing_inputs_are_not_passes(make_suite, tmp_path):
    summary = run_synthetic(make_suite(), tmp_path / "run")
    assert summary["states"]["completed"] == 1
    assert summary["states"]["unavailable"] == 2
    assert summary["attempt_count"] == 1


def test_real_evaluation_suites_rejected_without_output(smoke_suite, tmp_path):
    manifest = json.loads(smoke_suite.read_text())
    manifest["purpose"] = "evaluation"
    smoke_suite.write_text(json.dumps(seal(manifest)))
    out = tmp_path / "run"
    with pytest.raises(RunError, match="synthetic_suite_required"):
        run_synthetic(smoke_suite, out)
    assert not out.exists()


def test_existing_directory_and_git_destinations_rejected(smoke_suite, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "sentinel").write_text("keep")
    with pytest.raises(RunError):
        run_synthetic(smoke_suite, existing)
    assert (existing / "sentinel").read_text() == "keep"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(RunError, match="run_inside_git"):
        run_synthetic(smoke_suite, repo / "run")
    assert not (repo / "run").exists()


def test_tampered_frozen_input_fails_before_dispatch(
    smoke_suite, tmp_path, monkeypatch
):
    out = tmp_path / "run"
    run_synthetic(smoke_suite, out, max_steps=0)
    with ledger_at(out) as ledger:
        digest = ledger.manifest_digest
    (out / "objects" / digest).write_bytes(b"tampered")
    monkeypatch.setattr(
        FakeAdapter, "invoke", lambda *args: pytest.fail("must not dispatch")
    )
    with pytest.raises((RunError, ValueError)):
        resume_synthetic(out)


def test_corrupt_completed_output_fails_closed(smoke_suite, tmp_path):
    out = tmp_path / "run"
    run_synthetic(smoke_suite, out)
    with ledger_at(out) as ledger:
        digest = ledger.state(ledger.plan()[0]["work_id"])["result_digest"]
    (out / "objects" / digest).write_bytes(b"tampered")
    with pytest.raises((RunError, ValueError)):
        resume_synthetic(out)


def test_private_modes_and_symlink_state_rejection(smoke_suite, tmp_path):
    out = tmp_path / "run"
    run_synthetic(smoke_suite, out, max_steps=0)
    assert out.stat().st_mode & 0o777 == 0o700
    for path in out.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    state = out / "ledger.sqlite3"
    moved = out / "renamed.sqlite3"
    state.rename(moved)
    state.symlink_to(moved)
    with pytest.raises((RunError, ValueError)):
        resume_synthetic(out)


def test_second_resumer_is_rejected_while_run_lock_is_held(smoke_suite, tmp_path):
    import fcntl

    out = tmp_path / "run"
    run_synthetic(smoke_suite, out, max_steps=0)
    with (out / "run.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RunError, match="run_busy"):
            resume_synthetic(out)
