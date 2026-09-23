"""Private, bounded annotation custody independent of task/rater semantics."""

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from draftbench import annotation_store as module
from draftbench.annotation_store import AnnotationStore, AnnotationStoreError
from draftbench.identity import canonical_bytes

MANIFEST = "a" * 64
SESSION = "session-1"


def record(record_id="label-1", *, kind="original", task_id="task-1", body=None):
    return {
        "record_id": record_id,
        "kind": kind,
        "task_id": task_id,
        "body": {"rating": 2, "note": "private custodian text"}
        if body is None
        else body,
    }


@pytest.fixture
def path(tmp_path):
    return tmp_path / "annotations.sqlite3"


@pytest.fixture
def store(path):
    with AnnotationStore.create(path, SESSION, MANIFEST) as value:
        yield value


def test_append_distinct_labels_and_adjudications_without_superseding(path, store):
    originals = [record(f"rater-{i}", body={"rating": i}) for i in range(20)]
    assert store.append(originals) == {"inserted": 20, "duplicates": 0}
    before = store.records()
    adjudications = [record(f"resolution-{i}", kind="adjudication") for i in range(3)]
    assert store.append(adjudications) == {"inserted": 3, "duplicates": 0}
    assert store.records("original") == before
    assert [row["record_id"] for row in store.records("adjudication")] == [
        row["record_id"] for row in adjudications
    ]
    assert store.summary() == {
        "record_count": 23,
        "original_count": 20,
        "adjudication_count": 3,
    }
    assert store.session_id == SESSION
    assert store.manifest_digest == MANIFEST
    for saved, original in zip(store.records(), originals + adjudications, strict=True):
        assert set(saved) == set(original) | {"received_at"}
        assert {key: saved[key] for key in original} == original
        timestamp = datetime.fromisoformat(saved["received_at"])
        assert timestamp.tzinfo is not None and timestamp.utcoffset() == timedelta(0)
    assert store.append([]) == {"inserted": 0, "duplicates": 0}
    with AnnotationStore.open(path) as reopened:
        assert reopened.records() == store.records()
        assert reopened.summary() == store.summary()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_canonical_idempotence_timestamp_and_defensive_copies(store, path):
    incoming = record(body={"z": [1, True, None, "e\u0301", "é"], "a": 2.0})
    before = datetime.now(timezone.utc)
    assert store.append([incoming]) == {"inserted": 1, "duplicates": 0}
    saved = store.get("label-1")
    assert (
        before
        <= datetime.fromisoformat(saved["received_at"])
        <= datetime.now(timezone.utc)
    )
    reordered = deepcopy(dict(reversed(list(incoming.items()))))
    reordered["body"] = dict(reversed(list(reordered["body"].items())))
    assert store.append([reordered]) == {"inserted": 0, "duplicates": 1}
    assert store.get("label-1") == saved
    incoming["body"]["z"].clear()
    exported = store.records()
    exported[0]["body"].clear()
    assert store.get("label-1") == saved
    assert store.get("missing") is None
    with AnnotationStore.open(path) as reopened:
        assert reopened.append([reordered]) == {"inserted": 0, "duplicates": 1}
        assert reopened.get("label-1") == saved


@pytest.mark.parametrize(
    "change", [{"body": {"rating": 3}}, {"kind": "adjudication"}, {"task_id": "task-2"}]
)
def test_conflicting_id_rolls_back_entire_batch_and_preserves_original(store, change):
    store.append([record()])
    before = store.records()
    with pytest.raises(AnnotationStoreError, match="^annotation_store_conflict$"):
        store.append([record("new"), {**record(), **change}])
    assert store.records() == before
    assert store.get("new") is None
    assert store.append([record("new"), record()]) == {"inserted": 1, "duplicates": 1}


@pytest.mark.parametrize("value", [1.0, True])
def test_json_equal_python_values_are_not_canonical_equal(store, value):
    store.append([record(body={"rating": 1})])
    with pytest.raises(AnnotationStoreError, match="^annotation_store_conflict$"):
        store.append([record(body={"rating": value})])


@pytest.mark.parametrize("different", [False, True])
def test_duplicate_ids_within_batch_rejected_even_when_identical(store, different):
    duplicate = record(body={"different": True}) if different else record()
    with pytest.raises(AnnotationStoreError, match="^annotation_store_duplicate_id$"):
        store.append([record(), duplicate])
    assert store.records() == []


@pytest.mark.parametrize("batch", [None, (), {}, "private input"])
def test_batch_type_is_strict(store, batch):
    with pytest.raises(AnnotationStoreError, match="^annotation_store_invalid_batch$"):
        store.append(batch)
    assert store.summary()["record_count"] == 0


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        {**record(), "extra": "secret"},
        {**record(), "received_at": "2020-01-01T00:00:00Z"},
        {**record(), "record_id": "../private"},
        {**record(), "record_id": "a" * 129},
        {**record(), "record_id": True},
        {**record(), "task_id": "task\n"},
        {**record(), "task_id": ""},
        {**record(), "kind": []},
        {**record(), "kind": "averaged"},
        {**record(), "body": []},
        {**record(), "body": None},
        record(body={"nested": (1, 2)}),
        record(body={"nested": b"private"}),
        record(body={"nested": {1: "key"}}),
        record(body={"nested": float("nan")}),
        record(body={"nested": float("inf")}),
        record(body={"nested": "\ud800"}),
    ],
)
def test_invalid_record_is_atomic_and_diagnostics_are_safe(store, invalid):
    with pytest.raises(
        AnnotationStoreError, match="^annotation_store_invalid_record$"
    ) as caught:
        store.append([record("valid"), invalid])
    assert caught.value.__context__ is None or caught.value.__suppress_context__
    assert store.records() == []


def test_cyclic_and_too_deep_json_are_safe(store):
    cyclic = {}
    cyclic["self"] = cyclic
    deep = {}
    for _ in range(60):
        deep = {"nested": deep}
    for body in (cyclic, deep):
        with pytest.raises(
            AnnotationStoreError, match="^annotation_store_invalid_record$"
        ):
            store.append([record(body=body)])
    assert store.records() == []


@pytest.mark.parametrize("field", ["session_id", "manifest_digest"])
@pytest.mark.parametrize(
    "bad", [None, True, "", "private /path", "z" * 129, "a" * 64 + "\n"]
)
def test_invalid_metadata_never_creates_file(path, field, bad):
    args = {"session_id": SESSION, "manifest_digest": MANIFEST, field: bad}
    with pytest.raises(AnnotationStoreError):
        AnnotationStore.create(path, **args)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["ORIGINAL", "", {}, 1])
def test_invalid_filter_is_safe(store, kind):
    with pytest.raises(AnnotationStoreError, match="^annotation_store_invalid_kind$"):
        store.records(kind)


def test_invalid_lookup_is_safe(store):
    with pytest.raises(
        AnnotationStoreError, match="^annotation_store_invalid_identifier$"
    ):
        store.get("private /path")


def test_raw_payload_readback_hashes_and_no_timestamp_in_digest(path, store):
    source = record(body={"unicode": "雪", "identity": "preserve nested identity"})
    store.append([source])
    with sqlite3.connect(path) as raw:
        payload, digest, timestamp = raw.execute(
            "SELECT payload,digest,received_at FROM records"
        ).fetchone()
    assert type(payload) is bytes
    assert payload == canonical_bytes(source)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert timestamp == store.get(source["record_id"])["received_at"]
    assert b"received_at" not in payload


@pytest.mark.parametrize("recursive", [0, 1])
def test_sql_metadata_and_records_cannot_update_delete_or_replace(
    path, store, recursive
):
    store.append([record()])
    before = store.records()
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA recursive_triggers={recursive}")
        for table in ("metadata", "records"):
            for sql in (
                f"UPDATE {table} SET rowid=rowid",
                f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(sql)
    assert store.records() == before
    assert store.session_id == SESSION
    assert store.manifest_digest == MANIFEST


@pytest.mark.parametrize("rowid", ["rowid", "_rowid_", "oid"])
def test_hidden_rowid_replace_cannot_clobber_original(path, store, rowid):
    store.append([record()])
    source = record("replacement", kind="adjudication", task_id="task-2")
    payload = canonical_bytes(source)
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA recursive_triggers=OFF")
        old_rowid, timestamp = raw.execute(
            "SELECT rowid,received_at FROM records"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                f"INSERT OR REPLACE INTO records({rowid},record_id,kind,task_id,payload,digest,received_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    old_rowid,
                    source["record_id"],
                    source["kind"],
                    source["task_id"],
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    timestamp,
                ),
            )
    assert store.get("label-1")["kind"] == "original"
    assert store.get("replacement") is None


def test_independent_connections_concurrent_idempotent_import(path, store):
    barrier = threading.Barrier(2)
    batch = [record(f"label-{i}") for i in range(30)]

    def append(_):
        with AnnotationStore.open(path) as other:
            barrier.wait(timeout=10)
            result = other.append(batch)
            return result, other.records()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))
    assert sorted(result["inserted"] for result, _ in results) == [0, 30]
    assert sorted(result["duplicates"] for result, _ in results) == [0, 30]
    assert results[0][1] == results[1][1] == store.records()


def test_conflicting_concurrent_batches_have_one_winner_and_no_partial_rows(
    path, store
):
    barrier = threading.Barrier(2)

    def append(i):
        with AnnotationStore.open(path) as other:
            barrier.wait(timeout=10)
            try:
                return other.append([record(f"unique-{i}"), record(body={"rating": i})])
            except AnnotationStoreError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))
    assert results.count("annotation_store_conflict") == 1
    assert results.count({"inserted": 2, "duplicates": 0}) == 1
    winner = store.get("label-1")["body"]["rating"]
    assert {row["record_id"] for row in store.records()} == {
        "label-1",
        f"unique-{winner}",
    }


def test_exclusive_concurrent_create_has_one_winner(path):
    barrier = threading.Barrier(2)

    def create(i):
        barrier.wait(timeout=10)
        try:
            with AnnotationStore.create(path, f"session-{i}", MANIFEST):
                return f"session-{i}"
        except AnnotationStoreError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert results.count("annotation_store_exists") == 1
    with AnnotationStore.open(path) as store:
        assert store.session_id in results


def test_concurrent_create_reports_exists_during_private_journal_write(path, store):
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE in_progress(value)")
        assert path.with_name(path.name + "-journal").exists()
        with pytest.raises(AnnotationStoreError, match="^annotation_store_exists$"):
            AnnotationStore.create(path, SESSION, MANIFEST)
        writer.rollback()
    assert store.summary()["record_count"] == 0


def test_nonclobber_private_file_and_open_never_creates(path):
    with pytest.raises(AnnotationStoreError):
        AnnotationStore.open(path)
    assert not path.exists()
    path.write_bytes(b"private preexisting file")
    path.chmod(0o600)
    before = path.stat()
    with pytest.raises(AnnotationStoreError, match="^annotation_store_exists$"):
        AnnotationStore.create(path, SESSION, MANIFEST)
    assert path.read_bytes() == b"private preexisting file"
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mode == before.st_mode


@pytest.mark.parametrize("mask", [0, 0o077, 0o777])
def test_exclusive_create_is_0600_regardless_of_umask(path, mask):
    previous = os.umask(mask)
    try:
        with AnnotationStore.create(path, SESSION, MANIFEST) as store:
            store.append([record()])
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "kind", ["symlink", "dangling", "fifo", "directory", "hardlink"]
)
def test_unsafe_database_types_are_rejected_without_mutation(path, tmp_path, kind):
    target = tmp_path / "private-target"
    target.write_bytes(b"private untouched")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "absent")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.link(target, path)
    for action in (
        lambda: AnnotationStore.open(path),
        lambda: AnnotationStore.create(path, SESSION, MANIFEST),
    ):
        with pytest.raises(AnnotationStoreError):
            action()
    assert target.read_bytes() == b"private untouched"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "directory"])
def test_unsafe_sidecars_rejected_before_create_open_or_append(
    path, tmp_path, suffix, kind
):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        target = tmp_path / "sidecar-target"
        target.write_bytes(b"do not clobber")
        sidecar = path.with_name(path.name + suffix)
        if kind == "symlink":
            sidecar.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(sidecar)
        elif kind == "directory":
            sidecar.mkdir()
        else:
            os.link(target, sidecar)
        for action in (
            lambda: AnnotationStore.open(path),
            lambda: AnnotationStore.create(path, SESSION, MANIFEST),
            lambda: store.append([record()]),
        ):
            with pytest.raises(
                AnnotationStoreError, match="^annotation_store_unsafe_path$"
            ):
                action()
        assert target.read_bytes() == b"do not clobber"
        # Restore the namespace before closing the legitimate connection.
        sidecar.rmdir() if kind == "directory" else sidecar.unlink()


def test_parent_symlink_and_uri_metacharacters(tmp_path):
    parent = tmp_path / "real"
    parent.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_unsafe_path$"):
        AnnotationStore.create(link / "annotations.db", SESSION, MANIFEST)
    path = parent / "private?mode=memory#annotations.sqlite3"
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        store.append([record()])
    with AnnotationStore.open(path) as store:
        assert store.summary()["record_count"] == 1


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o400, 0o700])
def test_open_and_existing_handle_refuse_permission_widening_without_chmod(
    path, store, mode
):
    path.chmod(mode)
    try:
        with pytest.raises(
            AnnotationStoreError, match="^annotation_store_unsafe_path$"
        ):
            AnnotationStore.open(path)
        with pytest.raises(
            AnnotationStoreError, match="^annotation_store_unsafe_path$"
        ):
            store.append([record()])
        assert stat.S_IMODE(path.stat().st_mode) == mode
    finally:
        path.chmod(0o600)


def test_path_substitution_refused_by_open_handle(path, store):
    original = path.with_name("original.sqlite3")
    path.rename(original)
    path.write_bytes(b"private replacement")
    path.chmod(0o600)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_unsafe_path$"):
        store.records()
    assert path.read_bytes() == b"private replacement"


def test_connection_pragmas_extensions_and_closed_handles(path):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        expected = {
            "trusted_schema": 0,
            "foreign_keys": 1,
            "recursive_triggers": 1,
            "synchronous": 2,
            "journal_mode": "delete",
        }
        for pragma, value in expected.items():
            assert store._connection.execute(f"PRAGMA {pragma}").fetchone()[0] == value
        assert (
            0 < store._connection.execute("PRAGMA busy_timeout").fetchone()[0] <= 5000
        )
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            store._connection.execute("SELECT load_extension('never-load-this')")
    store.close()
    for action in (
        store.__enter__,
        store.summary,
        store.records,
        lambda: store.append([]),
        lambda: store.get("missing"),
        lambda: store.session_id,
        lambda: store.manifest_digest,
    ):
        with pytest.raises(AnnotationStoreError, match="^annotation_store_closed$"):
            action()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-journal").exists()


def test_busy_is_bounded_and_failed_append_is_retryable(path, store):
    with sqlite3.connect(path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(AnnotationStoreError, match="^annotation_store_busy$"):
            store.append([record()])
        blocker.rollback()
    assert store.records() == []
    assert store.append([record()]) == {"inserted": 1, "duplicates": 0}


def resealed_update(path, table, sql, parameters=()):
    with sqlite3.connect(path) as raw:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (f"{table}_no_update",)
        ).fetchone()[0]
        raw.execute(f"DROP TRIGGER {table}_no_update")
        raw.execute("PRAGMA ignore_check_constraints=ON")
        raw.execute(sql, parameters)
        raw.execute(trigger)


@pytest.mark.parametrize(
    "kind",
    [
        "random",
        "empty",
        "unrelated",
        "version",
        "metadata_version",
        "missing_trigger",
        "extra_table",
        "truncated",
    ],
)
def test_open_rejects_corrupt_or_unknown_schema(path, kind):
    if kind in ("random", "empty"):
        path.write_bytes(b"private secrets" if kind == "random" else b"")
        path.chmod(0o600)
    elif kind == "unrelated":
        with sqlite3.connect(path) as raw:
            raw.execute("CREATE TABLE other(value)")
        path.chmod(0o600)
    else:
        with AnnotationStore.create(path, SESSION, MANIFEST) as store:
            store.append([record()])
        if kind == "truncated":
            path.write_bytes(path.read_bytes()[:200])
        elif kind == "metadata_version":
            resealed_update(path, "metadata", "UPDATE metadata SET schema_version=999")
        else:
            with sqlite3.connect(path) as raw:
                raw.execute(
                    {
                        "version": "PRAGMA user_version=999",
                        "missing_trigger": "DROP TRIGGER records_no_delete",
                        "extra_table": "CREATE TABLE unrecognized(private)",
                    }[kind]
                )
    with pytest.raises(AnnotationStoreError) as caught:
        AnnotationStore.open(path)
    assert str(caught.value) in {"annotation_store_corrupt", "annotation_store_version"}
    assert str(path) not in str(caught.value) and "private" not in str(caught.value)
    assert caught.value.__context__ is None or caught.value.__suppress_context__


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("UPDATE metadata SET session_id=?", ("../private",)),
        ("UPDATE metadata SET manifest_digest=?", ("A" * 64,)),
        ("UPDATE metadata SET singleton=2", ()),
    ],
)
def test_open_validates_metadata_after_triggers_are_resealed(path, sql, parameters):
    AnnotationStore.create(path, SESSION, MANIFEST).close()
    resealed_update(path, "metadata", sql, parameters)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)


@pytest.mark.parametrize(
    "column,value",
    [
        ("digest", "0" * 64),
        ("kind", "adjudication"),
        ("task_id", "task-2"),
        ("record_id", "changed-id"),
        ("payload", b'{"record_id":1,"record_id":2}'),
        ("received_at", "2020-01-01T00:00:00.000000+01:00"),
        ("received_at", "2020-99-01T00:00:00.000000+00:00"),
        ("received_at", "2020-01-01T00:00:00"),
        ("received_at", 123),
        ("received_at", b"2020-01-01T00:00:00.000000+00:00"),
    ],
)
def test_open_rechecks_digests_projection_and_timestamp(path, column, value):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        store.append([record()])
    resealed_update(path, "records", f"UPDATE records SET {column}=?", (value,))
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)


def test_noncanonical_and_duplicate_key_payload_rejected_even_with_matching_digest(
    path,
):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        store.append([record()])
    for payload in (
        b" " + canonical_bytes(record()),
        b'{"record_id":"a","record_id":"b"}',
    ):
        resealed_update(
            path,
            "records",
            "UPDATE records SET payload=?,digest=?",
            (payload, hashlib.sha256(payload).hexdigest()),
        )
        with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
            AnnotationStore.open(path)


def test_batch_limit_and_exact_boundary(store):
    assert module.MAX_BATCH_RECORDS == 4096
    batch = [record(f"label-{i}", body={}) for i in range(module.MAX_BATCH_RECORDS)]
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append(batch + [record("extra")])
    assert store.records() == []
    assert store.append(batch) == {"inserted": len(batch), "duplicates": 0}
    assert store.summary()["record_count"] == len(batch)


def sized_record(size, record_id="sized"):
    source = record(record_id, body={"private": ""})
    source["body"]["private"] = "x" * (size - len(canonical_bytes(source)))
    assert len(canonical_bytes(source)) == size
    return source


def test_actual_per_record_byte_limit_exact_boundary_and_atomic_reject(store, path):
    assert module.MAX_RECORD_BYTES == 8 * 1024 * 1024
    exact = sized_record(module.MAX_RECORD_BYTES)
    oversized = sized_record(module.MAX_RECORD_BYTES + 1)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("first"), oversized])
    assert store.records() == []
    store.append([exact])
    with AnnotationStore.open(path) as reopened:
        assert reopened.get("sized")["body"] == exact["body"]


def test_total_retained_byte_limit_counts_canonical_utf8_and_not_duplicates(
    store, monkeypatch, path
):
    assert module.MAX_TOTAL_BYTES == 64 * 1024 * 1024
    sources = [record("a", body={"unicode": "雪"}), record("b", body={"unicode": "é"})]
    limit = sum(len(canonical_bytes(source)) for source in sources)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", limit)
    store.append([sources[0]])
    assert store.append(sources) == {"inserted": 1, "duplicates": 1}
    before = store.records()
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("overflow", body={})])
    assert store.records() == before
    with AnnotationStore.open(path) as reopened:
        assert reopened.records() == before
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", limit - 1)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)


def test_retained_row_limit_and_open_limit_check(store, path, monkeypatch):
    assert module.MAX_RECORDS == 100_000
    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    store.append([record("a")])
    assert store.append([record("a"), record("b")]) == {"inserted": 1, "duplicates": 1}
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("c")])
    assert store.summary()["record_count"] == 2
    monkeypatch.setattr(module, "MAX_RECORDS", 1)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)


def test_oversized_corrupt_payload_is_rejected_before_fetching_it(
    store, path, monkeypatch
):
    store.append([record()])
    monkeypatch.setattr(module, "MAX_RECORD_BYTES", 1)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        store.records()


def test_safe_error_class_does_not_echo_arbitrary_input():
    assert str(AnnotationStoreError("private prompt /path")) == "annotation_store_io"
    assert str(AnnotationStoreError({"private": True})) == "annotation_store_io"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_orphan_regular_sidecar_is_not_consumed_by_create(path, suffix):
    sidecar = path.with_name(path.name + suffix)
    sidecar.write_bytes(b"private existing sidecar")
    sidecar.chmod(0o600)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_unsafe_path$"):
        AnnotationStore.create(path, SESSION, MANIFEST)
    assert not path.exists()
    assert sidecar.read_bytes() == b"private existing sidecar"


def test_summary_queries_lengths_and_counts_not_private_blobs(store):
    store.append([record()])
    statements = []
    store._connection.set_trace_callback(statements.append)
    try:
        assert store.summary() == {
            "record_count": 1,
            "original_count": 1,
            "adjudication_count": 0,
        }
    finally:
        store._connection.set_trace_callback(None)
    selects = [sql.lower() for sql in statements if sql.lower().startswith("select")]
    assert selects and all("select *" not in sql for sql in selects)
    assert all("select payload" not in sql for sql in selects)
    assert all("private custodian text" not in sql for sql in statements)


def test_actual_total_retained_64_mib_boundary(store, path):
    # Use the production constants, not only monkeypatched small-limit tests.
    sources = [sized_record(module.MAX_RECORD_BYTES, f"large-{i}") for i in range(8)]
    assert (
        sum(len(canonical_bytes(source)) for source in sources)
        == module.MAX_TOTAL_BYTES
    )
    assert store.append(sources) == {"inserted": 8, "duplicates": 0}
    assert store.append([sources[0]]) == {"inserted": 0, "duplicates": 1}
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("overflow", body={})])
    with AnnotationStore.open(path) as reopened:
        assert reopened.summary()["record_count"] == 8
        assert reopened.get("large-7")["body"] == sources[-1]["body"]


def test_actual_retained_100000_row_boundary(store, path):
    for start in range(0, module.MAX_RECORDS, module.MAX_BATCH_RECORDS):
        batch = [
            record(f"row-{i}", body={})
            for i in range(
                start, min(start + module.MAX_BATCH_RECORDS, module.MAX_RECORDS)
            )
        ]
        assert store.append(batch) == {"inserted": len(batch), "duplicates": 0}
    assert store.summary()["record_count"] == 100_000
    assert store.append([record("row-0", body={})]) == {"inserted": 0, "duplicates": 1}
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("overflow", body={})])
    with AnnotationStore.open(path) as reopened:
        assert reopened.summary()["record_count"] == 100_000
        assert reopened.get("row-99999")["body"] == {}


def test_late_batch_capacity_failure_rolls_back_previous_insert(store, monkeypatch):
    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    store.append([record("retained")])
    before = store.records()
    with pytest.raises(AnnotationStoreError, match="^annotation_store_limit$"):
        store.append([record("first"), record("overflow")])
    assert store.records() == before


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("UPDATE records SET payload=?", ("not a blob",)),
        ("UPDATE records SET position=0", ()),
        ("UPDATE records SET kind=?", ("invalid",)),
    ],
)
def test_corrupt_row_types_and_bounds_fail_closed(path, sql, parameters):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        store.append([record()])
    resealed_update(path, "records", sql, parameters)
    with pytest.raises(AnnotationStoreError, match="^annotation_store_corrupt$"):
        AnnotationStore.open(path)


def test_failed_create_does_not_remove_a_replacement_inode(path, monkeypatch):
    original = path.with_name("interrupted-original.sqlite3")
    connect = sqlite3.connect

    def fail(*args, **kwargs):
        path.rename(original)
        path.write_bytes(b"private replacement must remain")
        path.chmod(0o600)
        raise sqlite3.OperationalError("private initialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", fail)
        with pytest.raises(AnnotationStoreError, match="^annotation_store_io$"):
            AnnotationStore.create(path, SESSION, MANIFEST)
    assert sqlite3.connect == connect
    assert path.read_bytes() == b"private replacement must remain"
    assert original.exists()


def test_initialization_failure_is_atomic_and_cleanup_is_own_inode_only(
    path, monkeypatch
):
    connect = sqlite3.connect
    observed = []

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("CREATE TRIGGER"):
                with connect(path) as reader:
                    observed.append(
                        reader.execute("SELECT name FROM sqlite_master").fetchall()
                    )
                raise sqlite3.OperationalError("private sqlite diagnostics")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=Interrupted)
    )
    with pytest.raises(AnnotationStoreError, match="^annotation_store_io$") as caught:
        AnnotationStore.create(path, SESSION, MANIFEST)
    assert observed == [[]]
    assert caught.value.__suppress_context__
    assert not path.exists()
    assert not path.with_name(path.name + "-journal").exists()


def test_mid_batch_sql_failure_rolls_back_and_other_reader_never_sees_partial(
    path, monkeypatch
):
    connect = sqlite3.connect
    observations = []

    class Interrupted(sqlite3.Connection):
        insert_count = 0

        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO records"):
                self.insert_count += 1
                if self.insert_count == 2:
                    with connect(path) as reader:
                        observations.append(
                            reader.execute("SELECT count(*) FROM records").fetchone()[0]
                        )
                    raise sqlite3.OperationalError("private interrupted import")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=Interrupted)
    )
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        with pytest.raises(AnnotationStoreError, match="^annotation_store_io$"):
            store.append([record("a"), record("b")])
        assert observations == [0]
        assert store.records() == []
        assert store.append([record("a"), record("b")]) == {
            "inserted": 2,
            "duplicates": 0,
        }


def test_subprocess_exit_preserves_committed_batch_and_timestamp(path):
    script = """
import os, sys
from draftbench.annotation_store import AnnotationStore
store = AnnotationStore.create(sys.argv[1], "session-1", "a" * 64)
store.append([{"record_id":"label-1", "task_id":"task-1", "kind":"original", "body":{}}])
print(store.get("label-1")["received_at"], flush=True)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr.decode()
    with AnnotationStore.open(path) as store:
        assert store.get("label-1")["received_at"] == result.stdout.decode().strip()
        assert store.append([record(body={})]) == {"inserted": 0, "duplicates": 1}


def test_subprocess_death_mid_import_rolls_back_whole_batch(path):
    with AnnotationStore.create(path, SESSION, MANIFEST) as store:
        store.append([record("retained")])
    script = """
import os, sqlite3, sys
from draftbench.annotation_store import AnnotationStore
connect = sqlite3.connect
class Interrupted(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql.startswith("INSERT INTO records"):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **kw: connect(*a, **kw, factory=Interrupted)
store = AnnotationStore.open(sys.argv[1])
store.append([{"record_id":str(i), "task_id":"task-1", "kind":"original", "body":{}} for i in range(2)])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, timeout=20
    )
    assert result.returncode == 73, result.stderr.decode()
    with AnnotationStore.open(path) as store:
        assert [row["record_id"] for row in store.records()] == ["retained"]
        assert store.append([record("retry")]) == {"inserted": 1, "duplicates": 0}
