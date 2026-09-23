"""Persistence, immutable history, and fail-closed scheduling contracts."""

import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from draftbench.ledger import Ledger, LedgerError

MANIFEST = "a" * 64
REQUEST = "b" * 64
RESULT = "c" * 64
STATES = {
    "planned",
    "reserved",
    "in_flight",
    "result_saved",
    "completed",
    "failed",
    "limited",
    "uncertain",
    "unavailable",
    "blocked",
}


def item(work_id="write", *, dependencies=None, role="writer", case_id="case-1"):
    return {
        "work_id": work_id,
        "case_id": case_id,
        "role": role,
        "dependencies": [] if dependencies is None else dependencies,
    }


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def ledger(path):
    with Ledger.create(path, [item()], MANIFEST) as value:
        yield value


def finish(ledger, work_id):
    attempt = ledger.reserve(work_id, REQUEST)
    ledger.start(attempt)
    ledger.record_result(attempt, RESULT)
    ledger.complete(attempt)
    return attempt


def test_atomic_initial_plan_manifest_and_private_permissions(path):
    plan = [item(), item("review", role="reviewer", dependencies=["write"])]
    with Ledger.create(path, plan, MANIFEST) as ledger:
        assert ledger.manifest_digest == MANIFEST
        assert ledger.plan() == plan
        assert ledger.state("write") == {
            "state": "planned",
            "attempt_id": None,
            "request_digest": None,
            "result_digest": None,
            "error_code": None,
        }
        assert [event["state"] for event in ledger.events()] == ["planned", "planned"]
        assert {event["work_id"] for event in ledger.events()} == {"write", "review"}
        assert ledger.summary() == {
            "work_count": 2,
            "attempt_count": 0,
            "states": {name: int(name == "planned") * 2 for name in STATES},
        }
        # A distinct connection sees all initial rows and metadata together.
        with Ledger.open(path) as reader:
            assert reader.plan() == plan
            assert reader.manifest_digest == MANIFEST
        plan[0]["case_id"] = "different"
        view = ledger.plan()
        view[1]["dependencies"].clear()
        assert ledger.plan()[0]["case_id"] == "case-1"
        assert ledger.plan()[1]["dependencies"] == ["write"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with Ledger.open(path) as reopened:
        assert reopened.summary()["work_count"] == 2


def test_lifecycle_durable_append_only_history_and_dependency_gate(path):
    plan = [item(), item("review", role="reviewer", dependencies=["write"])]
    with Ledger.create(path, plan, MANIFEST) as ledger:
        with pytest.raises(LedgerError, match="^ledger_dependency_unresolved$"):
            ledger.reserve("review", REQUEST)
        assert ledger.summary()["attempt_count"] == 0
        attempt = ledger.reserve("write", REQUEST)
        assert re.fullmatch(r"[0-9a-f]{32}", attempt)
        assert ledger.state("write")["request_digest"] == REQUEST
        ledger.start(attempt)
        ledger.record_result(attempt, RESULT)
        assert ledger.state("write")["state"] == "result_saved"
    with Ledger.open(path) as ledger:
        ledger.complete(attempt)
        current = ledger.state("write")
        assert current == {
            "state": "completed",
            "attempt_id": attempt,
            "request_digest": REQUEST,
            "result_digest": RESULT,
            "error_code": None,
        }
        finish(ledger, "review")
        events = ledger.events()
        writer_events = [event for event in events if event["work_id"] == "write"]
        assert [event["state"] for event in writer_events] == [
            "planned",
            "reserved",
            "in_flight",
            "result_saved",
            "completed",
        ]
        assert writer_events[1]["result_digest"] is None
        assert writer_events[-1]["result_digest"] == RESULT
        assert len({event["event_id"] for event in events}) == len(events)
        assert ledger.summary()["states"]["completed"] == 2


def test_recover_only_in_flight_never_retries_or_loses_saved_results(path):
    names = ["planned", "reserved", "flight", "saved", "done"]
    with Ledger.create(path, [item(name) for name in names], MANIFEST) as ledger:
        reserved = ledger.reserve("reserved", REQUEST)
        flight = ledger.reserve("flight", REQUEST)
        ledger.start(flight)
        saved = ledger.reserve("saved", REQUEST)
        ledger.start(saved)
        ledger.record_result(saved, RESULT)
        finish(ledger, "done")
    with Ledger.open(path) as ledger:
        assert ledger.state("flight")["state"] == "in_flight"  # Open is not recovery.
        previous = ledger.events()
        ledger.recover()
        assert ledger.state("planned")["state"] == "planned"
        assert ledger.state("reserved")["state"] == "reserved"
        assert ledger.state("flight")["state"] == "uncertain"
        assert ledger.state("flight")["attempt_id"] == flight
        assert ledger.state("saved")["state"] == "result_saved"
        assert ledger.state("saved")["result_digest"] == RESULT
        assert ledger.state("done")["state"] == "completed"
        assert ledger.events()[:-1] == previous
        recovered = ledger.events()
        ledger.recover()
        assert ledger.events() == recovered
        with pytest.raises(LedgerError):
            ledger.reserve("flight", REQUEST)
        with pytest.raises(LedgerError):
            ledger.record_result(flight, RESULT)
        ledger.start(reserved)
        ledger.record_result(reserved, RESULT)
        ledger.complete(reserved)
        ledger.complete(saved)
        assert ledger.summary()["attempt_count"] == 4
    with Ledger.open(path) as ledger:
        assert ledger.summary()["states"]["uncertain"] == 1
        assert ledger.summary()["states"]["completed"] == 3


def test_every_state_and_count_reconciliation(path):
    with Ledger.create(
        path, [item(name) for name in sorted(STATES)], MANIFEST
    ) as ledger:
        for name in [
            "reserved",
            "result_saved",
            "completed",
            "failed",
            "limited",
            "uncertain",
        ]:
            attempt = ledger.reserve(name, REQUEST)
            if name == "reserved":
                continue
            ledger.start(attempt)
            if name in {"result_saved", "completed"}:
                ledger.record_result(attempt, RESULT)
            if name == "completed":
                ledger.complete(attempt)
            elif name in {"failed", "limited"}:
                ledger.fail(
                    attempt,
                    "adapter_failed" if name == "failed" else "adapter_limited",
                    limited=name == "limited",
                )
        ledger.recover()
        ledger.start(ledger.reserve("in_flight", REQUEST))
        ledger.skip("unavailable", state="unavailable", code="input_unavailable")
        ledger.skip("blocked", state="blocked", code="dependency_unresolved")
        summary = ledger.summary()
        assert summary == {
            "work_count": len(STATES),
            "attempt_count": 7,
            "states": dict.fromkeys(STATES, 1),
        }
        assert sum(summary["states"].values()) == summary["work_count"]
        for name in STATES:
            assert ledger.state(name)["state"] == name


@pytest.mark.parametrize(
    "code",
    ["input_unavailable", "input_unknown", "input_invalid", "input_inapplicable"],
)
def test_safe_unavailable_reason_codes(ledger, code):
    ledger.skip("write", state="unavailable", code=code)
    assert ledger.state("write")["error_code"] == code
    assert ledger.summary()["attempt_count"] == 0
    with pytest.raises(LedgerError):
        ledger.reserve("write", REQUEST)


@pytest.mark.parametrize(
    "code", ["adapter_failed", "adapter_limited", "invalid_adapter_output"]
)
def test_safe_failure_codes(ledger, code):
    attempt = ledger.reserve("write", REQUEST)
    ledger.start(attempt)
    ledger.fail(attempt, code)
    assert ledger.state("write")["state"] == "failed"
    assert ledger.state("write")["error_code"] == code
    assert ledger.state("write")["result_digest"] is None
    with pytest.raises(LedgerError):
        ledger.reserve("write", REQUEST)


@pytest.mark.parametrize(
    "stage",
    [
        "reserved",
        "in_flight",
        "result_saved",
        "completed",
        "failed",
        "limited",
        "uncertain",
    ],
)
def test_invalid_and_duplicate_transitions_never_append(ledger, stage):
    attempt = ledger.reserve("write", REQUEST)
    if stage != "reserved":
        ledger.start(attempt)
    if stage in {"result_saved", "completed"}:
        ledger.record_result(attempt, RESULT)
    if stage == "completed":
        ledger.complete(attempt)
    elif stage in {"failed", "limited"}:
        ledger.fail(attempt, "adapter_failed", limited=stage == "limited")
    elif stage == "uncertain":
        ledger.recover()
    actions = [
        lambda: ledger.reserve("write", REQUEST),
        lambda: ledger.skip("write", state="blocked", code="dependency_unresolved"),
    ]
    if stage != "reserved":
        actions.append(lambda: ledger.start(attempt))
    if stage != "in_flight":
        actions.extend(
            [
                lambda: ledger.record_result(attempt, RESULT),
                lambda: ledger.fail(attempt, "adapter_failed"),
            ]
        )
    if stage != "result_saved":
        actions.append(lambda: ledger.complete(attempt))
    before = ledger.events()
    for action in actions:
        with pytest.raises(LedgerError, match="^ledger_invalid_transition$"):
            action()
        assert ledger.events() == before
    assert ledger.summary()["attempt_count"] == 1


def test_skip_is_terminal_and_dependency_failure_never_counts_success(path):
    with Ledger.create(
        path, [item(), item("child", dependencies=["write"])], MANIFEST
    ) as ledger:
        ledger.skip("write", state="unavailable", code="input_unknown")
        with pytest.raises(LedgerError):
            ledger.reserve("child", REQUEST)
        ledger.skip("child", state="blocked", code="dependency_unresolved")
        before = ledger.events()
        with pytest.raises(LedgerError):
            ledger.skip("child", state="blocked", code="dependency_unresolved")
        assert ledger.events() == before
        assert ledger.summary()["states"]["completed"] == 0


@pytest.mark.parametrize(
    "plan",
    [
        [],
        (),
        None,
        [item(), item()],
        [item("")],
        [item("/private/path")],
        [item("x" * 129)],
        [item("bad\n")],
        [item(role="judge")],
        [item(case_id="../private")],
        [item(dependencies=["missing"])],
        [item(dependencies=["write"])],
        [item(dependencies="write")],
        [item(dependencies=[{}])],
        [item(), item("child", dependencies=["write", "write"])],
        [item("a", dependencies=["b"]), item("b", dependencies=["a"])],
        [{**item(), "private_prompt": "must not persist"}],
        [{"work_id": "write"}],
    ],
)
def test_invalid_plan_rejected_without_creating_database(path, plan):
    with pytest.raises(LedgerError, match="^ledger_invalid_plan$"):
        Ledger.create(path, plan, MANIFEST)
    assert not path.exists()


def test_plan_limit_and_iterative_long_dag_validation(path):
    plan = [
        item(f"w{i}", dependencies=[f"w{i - 1}"] if i else []) for i in range(30_000)
    ]
    with pytest.raises(LedgerError, match="^ledger_invalid_plan$"):
        Ledger.create(path, plan + [item("extra")], MANIFEST)
    assert not path.exists()
    with Ledger.create(path, plan, MANIFEST) as ledger:
        assert ledger.summary()["work_count"] == 30_000
        assert ledger.plan() == plan
    with Ledger.open(path) as ledger:
        assert ledger.summary()["states"]["planned"] == 30_000


@pytest.mark.parametrize(
    "bad", [None, 123, "", "a" * 63, "G" * 64, "a" * 64 + "\n", "secret prompt"]
)
def test_digests_are_strict_and_diagnostics_safe(path, bad):
    with pytest.raises(LedgerError, match="^ledger_invalid_digest$"):
        Ledger.create(path, [item()], bad)
    assert not path.exists()
    with Ledger.create(path, [item()], MANIFEST) as ledger:
        before = ledger.events()
        with pytest.raises(LedgerError, match="^ledger_invalid_digest$"):
            ledger.reserve("write", bad)
        assert ledger.events() == before
        attempt = ledger.reserve("write", REQUEST)
        ledger.start(attempt)
        with pytest.raises(LedgerError, match="^ledger_invalid_digest$"):
            ledger.record_result(attempt, bad)
        assert ledger.state("write")["state"] == "in_flight"


@pytest.mark.parametrize(
    "bad",
    ["secret diagnostic /private/key", "adapter_failed\n", "", "x" * 1000, None, {}],
)
def test_private_error_codes_never_persist(ledger, bad):
    before = ledger.events()
    with pytest.raises(LedgerError, match="^ledger_invalid_code$"):
        ledger.skip("write", state="unavailable", code=bad)
    assert ledger.events() == before
    attempt = ledger.reserve("write", REQUEST)
    ledger.start(attempt)
    before = ledger.events()
    with pytest.raises(LedgerError, match="^ledger_invalid_code$"):
        ledger.fail(attempt, bad)
    assert ledger.events() == before


def test_unknown_identifiers_and_invalid_skip_state_are_safe(ledger):
    for action in [
        lambda: ledger.state("unknown"),
        lambda: ledger.reserve("unknown", REQUEST),
    ]:
        with pytest.raises(LedgerError, match="^ledger_unknown_work$"):
            action()
    with pytest.raises(LedgerError, match="^ledger_unknown_attempt$"):
        ledger.start("0" * 32)
    with pytest.raises(LedgerError, match="^ledger_invalid_identifier$"):
        ledger.state("/private/path")
    with pytest.raises(LedgerError, match="^ledger_invalid_state$"):
        ledger.skip("write", state="completed", code="input_unavailable")
    with pytest.raises(LedgerError, match="^ledger_invalid_state$"):
        ledger.skip("write", state={}, code="input_unavailable")
    attempt = ledger.reserve("write", REQUEST)
    ledger.start(attempt)
    with pytest.raises(LedgerError, match="^ledger_invalid_state$"):
        ledger.fail(attempt, "adapter_failed", limited="yes")


def test_all_history_and_plan_metadata_sql_update_delete_and_replace_forbidden(path):
    plan = [item(), item("review", dependencies=["write"], role="reviewer")]
    with Ledger.create(path, plan, MANIFEST) as ledger:
        finish(ledger, "write")
        before = ledger.events()
        with sqlite3.connect(path) as raw:
            for table in ["metadata", "plan", "dependencies", "attempts", "events"]:
                for sql in [
                    f"UPDATE {table} SET rowid = rowid",
                    f"DELETE FROM {table}",
                ]:
                    with pytest.raises(sqlite3.IntegrityError):
                        raw.execute(sql)
            for table in ["metadata", "plan", "dependencies"]:
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
            # REPLACE must not bypass immutability even on a default connection
            # with recursive_triggers OFF.
            for table in ["attempts", "events"]:
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
        assert ledger.events() == before
        assert ledger.plan() == plan


def test_attempt_history_cannot_be_replaced_through_hidden_rowid(path):
    with Ledger.create(path, [item(), item("other")], MANIFEST) as ledger:
        attempt = finish(ledger, "write")
        with sqlite3.connect(path) as raw:
            old_rowid = raw.execute(
                "SELECT rowid FROM attempts WHERE attempt_id=?", (attempt,)
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT OR REPLACE INTO attempts(rowid,attempt_id,work_id,request_digest) VALUES (?,?,?,?)",
                    (old_rowid, "0" * 32, "other", REQUEST),
                )
        assert ledger.state("write")["attempt_id"] == attempt
    with Ledger.open(path) as ledger:
        assert ledger.summary()["attempt_count"] == 1


def test_sql_cannot_append_impossible_event_or_attempt(path, ledger):
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO events(work_id,state) VALUES ('write','completed')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO attempts(attempt_id,work_id,request_digest) VALUES (?,?,?)",
                ("0" * 32, "missing", REQUEST),
            )
    assert ledger.state("write")["state"] == "planned"


def test_second_create_never_clobbers_and_open_never_creates(path):
    with pytest.raises(LedgerError):
        Ledger.open(path)
    assert not path.exists()
    with Ledger.create(path, [item()], MANIFEST):
        original = path.read_bytes()
        with pytest.raises(LedgerError, match="^ledger_exists$"):
            Ledger.create(path, [item("other")], "d" * 64)
        assert path.read_bytes() == original


def test_symlinks_directories_fifos_and_sidecar_symlinks_rejected(tmp_path, path):
    with Ledger.create(path, [item()], MANIFEST):
        pass
    link = tmp_path / "link"
    link.symlink_to(path)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "absent")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for unsafe in [link, dangling, fifo, tmp_path]:
        with pytest.raises(LedgerError):
            Ledger.open(unsafe)
        with pytest.raises(LedgerError):
            Ledger.create(unsafe, [item()], MANIFEST)
    assert not (tmp_path / "absent").exists()
    target = tmp_path / "secret"
    target.write_bytes(b"do not change")
    journal = tmp_path / "ledger.sqlite3-journal"
    journal.symlink_to(target)
    with pytest.raises(LedgerError, match="^ledger_unsafe_path$"):
        Ledger.open(path)
    assert target.read_bytes() == b"do not change"


def test_parent_symlink_rejected(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(LedgerError, match="^ledger_unsafe_path$"):
        Ledger.create(link / "ledger.db", [item()], MANIFEST)


@pytest.mark.parametrize(
    "kind",
    [
        "random",
        "empty",
        "unrelated",
        "version",
        "metadata_version",
        "missing_trigger",
        "truncated",
    ],
)
def test_corruption_and_version_fail_closed_with_safe_codes(path, kind):
    if kind in {"random", "empty"}:
        path.write_bytes(b"private secret contents" if kind == "random" else b"")
    elif kind == "unrelated":
        with sqlite3.connect(path) as raw:
            raw.execute("CREATE TABLE other(value)")
    else:
        with Ledger.create(path, [item()], MANIFEST) as ledger:
            finish(ledger, "write")
        if kind == "truncated":
            path.write_bytes(path.read_bytes()[:200])
        else:
            with sqlite3.connect(path) as raw:
                if kind == "version":
                    raw.execute("PRAGMA user_version=999")
                elif kind == "metadata_version":
                    raw.execute("DROP TRIGGER metadata_no_update")
                    raw.execute("UPDATE metadata SET schema_version=999")
                else:
                    raw.execute("DROP TRIGGER events_no_delete")
    with pytest.raises(LedgerError) as caught:
        Ledger.open(path)
    assert str(caught.value) in {"ledger_corrupt", "ledger_version"}
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert str(path) not in str(caught.value)


def test_concurrent_reservation_has_one_winner(path):
    with Ledger.create(path, [item()], MANIFEST):
        pass
    gate = threading.Barrier(2)

    def reserve():
        with Ledger.open(path) as ledger:
            gate.wait(timeout=10)
            try:
                return ledger.reserve("write", REQUEST)
            except LedgerError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert results.count("ledger_invalid_transition") == 1
    assert sum(bool(re.fullmatch(r"[0-9a-f]{32}", result)) for result in results) == 1
    with Ledger.open(path) as ledger:
        assert ledger.summary()["attempt_count"] == 1
        assert [event["state"] for event in ledger.events()] == ["planned", "reserved"]


def test_busy_is_bounded_and_rolls_back_reservation(path, ledger):
    with sqlite3.connect(path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(LedgerError, match="^ledger_busy$"):
            ledger.reserve("write", REQUEST)
        blocker.rollback()
    assert ledger.summary()["attempt_count"] == 0
    assert ledger.state("write")["state"] == "planned"
    ledger.reserve("write", REQUEST)


def test_process_exit_without_close_preserves_result_saved(path):
    script = """
import os, sys
from draftbench.ledger import Ledger
ledger = Ledger.create(sys.argv[1], [{"work_id":"write", "case_id":"case-1",
    "role":"writer", "dependencies":[]}], "a" * 64)
attempt = ledger.reserve("write", "b" * 64)
ledger.start(attempt)
ledger.record_result(attempt, "c" * 64)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr.decode()
    with Ledger.open(path) as ledger:
        assert ledger.state("write")["state"] == "result_saved"
        ledger.recover()
        ledger.complete(ledger.state("write")["attempt_id"])
        assert ledger.state("write")["result_digest"] == RESULT


def test_initialization_failure_exposes_no_partial_plan_or_manifest(path, monkeypatch):
    original_connect = sqlite3.connect
    observations = []

    class InterruptedConnection(sqlite3.Connection):
        def executemany(self, sql, values):
            if sql.startswith("INSERT INTO events"):
                # Persist one initial event to the uncommitted transaction, then
                # inspect through an independent connection before interrupting.
                super().executemany(sql, [next(iter(values))])
                with original_connect(path) as reader:
                    observations.append(
                        reader.execute("SELECT name FROM sqlite_master").fetchall()
                    )
                raise sqlite3.OperationalError("private injected diagnostic")
            return super().executemany(sql, values)

        def rollback(self):
            super().rollback()
            observations.append(
                self.execute("SELECT name FROM sqlite_master").fetchall()
            )

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=InterruptedConnection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(LedgerError, match="^ledger_io$") as caught:
        Ledger.create(path, [item(), item("other")], MANIFEST)
    assert observations == [[], []]
    assert caught.value.__suppress_context__
    assert not path.exists()
    assert not path.with_name(path.name + "-journal").exists()


def test_transition_failure_rolls_back_attempt_and_preserves_saved_result(
    ledger, monkeypatch
):
    append = ledger._append

    def interrupt(*args, **kwargs):
        raise sqlite3.OperationalError("private injected diagnostic")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append", interrupt)
        with pytest.raises(LedgerError, match="^ledger_io$"):
            ledger.reserve("write", REQUEST)
    assert ledger.summary()["attempt_count"] == 0
    assert ledger.state("write")["state"] == "planned"
    assert len(ledger.events()) == 1
    attempt = ledger.reserve("write", REQUEST)
    ledger.start(attempt)
    ledger.record_result(attempt, RESULT)
    before = ledger.events()
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append", interrupt)
        with pytest.raises(LedgerError, match="^ledger_io$"):
            ledger.complete(attempt)
    assert ledger.events() == before
    assert ledger.state("write")["state"] == "result_saved"
    assert ledger.state("write")["result_digest"] == RESULT
    assert ledger._append == append
    ledger.complete(attempt)


def test_all_dependencies_required_and_non_topological_plan_order_retained(path):
    plan = [
        item("revision", role="revision", dependencies=["write", "review"]),
        item("review", role="reviewer"),
        item(),
    ]
    with Ledger.create(path, plan, MANIFEST) as ledger:
        finish(ledger, "write")
        with pytest.raises(LedgerError, match="^ledger_dependency_unresolved$"):
            ledger.reserve("revision", REQUEST)
        finish(ledger, "review")
        finish(ledger, "revision")
    with Ledger.open(path) as ledger:
        assert ledger.plan() == plan
        assert ledger.summary()["states"]["completed"] == 3


def test_corrupt_history_rejected_even_if_guard_restored(path):
    with Ledger.create(path, [item()], MANIFEST) as ledger:
        finish(ledger, "write")
    with sqlite3.connect(path) as raw:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='events_no_update'"
        ).fetchone()[0]
        raw.execute("DROP TRIGGER events_no_update")
        # Every individual row still passes its CHECK constraints, but the
        # completion digest no longer agrees with its durable result event.
        raw.execute(
            "UPDATE events SET result_digest=? WHERE state='completed'", ("d" * 64,)
        )
        raw.execute(trigger)
    with pytest.raises(LedgerError, match="^ledger_corrupt$"):
        Ledger.open(path)


def test_metadata_version_checked_independently(path):
    with Ledger.create(path, [item()], MANIFEST):
        pass
    with sqlite3.connect(path) as raw:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='metadata_no_update'"
        ).fetchone()[0]
        raw.execute("DROP TRIGGER metadata_no_update")
        raw.execute("UPDATE metadata SET schema_version=2")
        raw.execute(trigger)
    with pytest.raises(LedgerError, match="^ledger_version$"):
        Ledger.open(path)


def test_extension_loading_is_not_enabled(ledger):
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        ledger._connection.execute("SELECT load_extension('never-load-this')")


def test_uri_metacharacters_are_literal_file_names(tmp_path):
    path = tmp_path / "private?mode=memory#ledger.sqlite3"
    with Ledger.create(path, [item()], MANIFEST) as ledger:
        finish(ledger, "write")
    with Ledger.open(path) as ledger:
        assert ledger.summary()["states"]["completed"] == 1
    assert path.is_file()


def test_connection_safety_pragmas_and_context_cleanup(path):
    with Ledger.create(path, [item()], MANIFEST) as ledger:
        # Connection-local safety settings cannot be established by a separate reader.
        assert ledger._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert ledger._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert (
            ledger._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        )
        assert (
            0 < ledger._connection.execute("PRAGMA busy_timeout").fetchone()[0] <= 5000
        )
    ledger.close()
    with pytest.raises(LedgerError, match="^ledger_closed$"):
        ledger.summary()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()
