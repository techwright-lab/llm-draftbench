"""Real crashed SQLite inputs must never be recovered by report readers."""

# ruff: noqa: F811
import sqlite3
import subprocess
import sys

import pytest
from test_offline_workflow import smoke_suite  # noqa: F401
from test_reporting import annotated, prepared, render_inputs  # noqa: F401

from draftbench.annotation_store import AnnotationStore, AnnotationStoreError
from draftbench.ledger import Ledger, LedgerError
from draftbench.reporting import build_report


def inventory(root):
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def crash(path, mode):
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
if sys.argv[2] == 'journal':
    conn.execute('PRAGMA cache_size=10')
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('CREATE TABLE crash_padding(value BLOB)')
    conn.execute('INSERT INTO crash_padding VALUES (zeroblob(2000000))')
else:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('PRAGMA user_version=1')
os._exit(0)
""",
            str(path),
            mode,
        ],
        check=True,
    )
    sidecar = path.with_name(path.name + ("-journal" if mode == "journal" else "-wal"))
    data = sidecar.read_bytes()
    if mode == "journal":
        assert data[:8] == bytes.fromhex("d9d505f920a163d7")
        assert len(data) > 512
    else:
        assert data[:4] in (bytes.fromhex("377f0682"), bytes.fromhex("377f0683"))
        assert len(data) > 32


@pytest.mark.parametrize("mode", ["journal", "wal"])
@pytest.mark.parametrize("source", ["run", "annotation"])
def test_crashed_report_input_refused_without_mutation(
    prepared, tmp_path, mode, source
):
    inputs = render_inputs(prepared)
    root, name, error, code = (
        prepared[0],
        "ledger.sqlite3",
        LedgerError,
        "ledger_requires_recovery",
    )
    if source == "annotation":
        session, selection = annotated(prepared, tmp_path)
        inputs.update(session=session, selection_path=selection)
        root, name = session, "annotations.sqlite3"
        error, code = AnnotationStoreError, "annotation_store_requires_recovery"
    crash(root / name, mode)
    before = inventory(root)
    try:
        with pytest.raises(error, match=f"^{code}$"):
            build_report(**inputs)
    finally:
        assert inventory(root) == before


@pytest.mark.parametrize("source", ["run", "annotation"])
def test_readonly_open_cannot_write_and_report_still_works(prepared, tmp_path, source):
    inputs = render_inputs(prepared)
    root, name, cls = prepared[0], "ledger.sqlite3", Ledger
    if source == "annotation":
        session, selection = annotated(prepared, tmp_path)
        inputs.update(session=session, selection_path=selection)
        root, name, cls = session, "annotations.sqlite3", AnnotationStore
    path = root / name
    path.chmod(0o400)
    before = inventory(root)
    with cls.open(path, readonly=True) as store:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store._connection.execute("CREATE TABLE forbidden(value)")
        if source == "run":
            with pytest.raises(LedgerError, match="^ledger_readonly$"):
                store.recover()
        else:
            with pytest.raises(
                AnnotationStoreError, match="^annotation_store_readonly$"
            ):
                store.append([])
    assert build_report(**inputs)["execution_status"] == "complete"
    assert inventory(root) == before


@pytest.mark.parametrize(
    "cls,name,error,prefix",
    [
        (Ledger, "ledger.sqlite3", LedgerError, "ledger"),
        (
            AnnotationStore,
            "annotations.sqlite3",
            AnnotationStoreError,
            "annotation_store",
        ),
    ],
)
@pytest.mark.parametrize(
    "state", ["-journal", "-wal", "-shm", "bare_wal", "missing", "corrupt"]
)
def test_readonly_preflight_never_reaches_sqlite(
    prepared, tmp_path, monkeypatch, cls, name, error, prefix, state
):
    root = prepared[0]
    if cls is AnnotationStore:
        root, _ = annotated(prepared, tmp_path)
    path = root / name
    code = "requires_recovery"
    if state.startswith("-"):
        sidecar = path.with_name(path.name + state)
        sidecar.touch(mode=0o600)
    elif state == "bare_wal":
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        assert not path.with_name(path.name + "-wal").exists()
        assert path.read_bytes()[18:20] == b"\x02\x02"
    elif state == "missing":
        path.unlink()
        code = "io"
    else:
        path.write_bytes(b"not a SQLite database")
        code = "corrupt"
    before = inventory(root)

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must refuse before SQLite opens the input")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(error, match=f"^{prefix}_{code}$"):
        cls.open(path, readonly=True)
    assert inventory(root) == before
