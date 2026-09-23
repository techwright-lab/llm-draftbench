"""Private append-only custody for original annotations and adjudications.

The caller binds records to tasks, raters and a manifest; this module only checks
strict JSON envelopes and preserves every distinct record ID. Adjudications never
replace, average or mutate originals. Idempotency compares the entire canonical
input, excluding the server-assigned, first-insert UTC ``received_at`` timestamp.

The caller must control the containing directory and its ancestors (and enforce
outside-Git/session-directory policy). Ordinary owner-only 0600 files, no-follow
opens, inode checks and unsafe-sidecar rejection are defense in depth, not a
sandbox against a hostile same-user process replacing files during SQLite I/O.
Safe private rollback journals are allowed on writable open for crash recovery.
Read-only opens require the caller's session lock and refuse all sidecars and WAL
headers without recovery. A new store refuses all pre-existing sidecars.
Operators who disable/reseal SQL guards or rewrite the database are outside the
immutability threat model; reopen still verifies schema, metadata, canonical
payloads, hashes, bounds and timestamps.

One connection per thread/process. Independent connections serialize whole-batch
imports using BEGIN IMMEDIATE. ``records()`` is a deliberate private custodian
export bounded by 64 MiB of canonical payloads, not a public reporting surface.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .identity import canonical_bytes, strict_json_loads
from .ledger import LedgerError
from .ledger import _check_readonly as _ledger_check_readonly
from .ledger import _connect as _ledger_connect
from .ledger import _safe_path as _ledger_safe_path
from .ledger import _same_file as _ledger_same_file

SCHEMA_VERSION = 1
MAX_BATCH_RECORDS = 4096
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KINDS = ("original", "adjudication")
_FIELDS = {"record_id", "kind", "task_id", "body"}
_SIDECARS = ("-journal", "-wal", "-shm")
_ERROR_CODES = frozenset(
    {
        "annotation_store_io",
        "annotation_store_readonly",
        "annotation_store_requires_recovery",
        "annotation_store_exists",
        "annotation_store_unsafe_path",
        "annotation_store_busy",
        "annotation_store_corrupt",
        "annotation_store_version",
        "annotation_store_closed",
        "annotation_store_invalid_identifier",
        "annotation_store_invalid_digest",
        "annotation_store_invalid_kind",
        "annotation_store_invalid_record",
        "annotation_store_invalid_batch",
        "annotation_store_duplicate_id",
        "annotation_store_conflict",
        "annotation_store_limit",
    }
)


class AnnotationStoreError(ValueError):
    """An allowlisted safe code, never a path, payload or underlying diagnostic."""

    def __init__(self, code: str):
        super().__init__(
            code
            if type(code) is str and code in _ERROR_CODES
            else "annotation_store_io"
        )


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _identifier(value):
    if not _valid(value, _IDENTIFIER):
        raise AnnotationStoreError("annotation_store_invalid_identifier")


def _digest(value):
    if not _valid(value, _DIGEST):
        raise AnnotationStoreError("annotation_store_invalid_digest")


def _kind(value):
    if type(value) is not str or value not in _KINDS:
        raise AnnotationStoreError("annotation_store_invalid_kind")


def _encode_record(record):
    if (
        type(record) is not dict
        or set(record) != _FIELDS
        or not _valid(record["record_id"], _IDENTIFIER)
        or not _valid(record["task_id"], _IDENTIFIER)
        or type(record["kind"]) is not str
        or record["kind"] not in _KINDS
        or type(record["body"]) is not dict
    ):
        raise AnnotationStoreError("annotation_store_invalid_record")
    try:
        payload = canonical_bytes(record)
    except (ValueError, TypeError, RecursionError):
        raise AnnotationStoreError("annotation_store_invalid_record") from None
    if len(payload) > MAX_RECORD_BYTES:
        raise AnnotationStoreError("annotation_store_limit")
    return payload


def _prepare_batch(records):
    if type(records) is not list:
        raise AnnotationStoreError("annotation_store_invalid_batch")
    if len(records) > MAX_BATCH_RECORDS:
        raise AnnotationStoreError("annotation_store_limit")
    prepared, known, total = [], set(), 0
    for record in records:
        payload = _encode_record(record)
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            # Unique batch IDs necessarily occupy at least this many retained
            # bytes, even when some are already present as exact duplicates.
            raise AnnotationStoreError("annotation_store_limit")
        # Own a strict-JSON snapshot, never retain caller-owned mutable objects.
        copied = strict_json_loads(payload.decode("utf-8"))
        if not isinstance(copied, dict):
            raise AnnotationStoreError("annotation_store_invalid_record")
        if copied["record_id"] in known:
            raise AnnotationStoreError("annotation_store_duplicate_id")
        known.add(copied["record_id"])
        prepared.append((copied, payload, hashlib.sha256(payload).hexdigest()))
    return prepared


def _sql_id(column):
    return (
        f"typeof({column})='text' AND length(CAST({column} AS BLOB)) BETWEEN 1 AND 128 "
        f"AND {column} NOT GLOB '*[^A-Za-z0-9._-]*' "
        f"AND substr({column},1,1) GLOB '[A-Za-z0-9]'"
    )


def _sql_digest(column):
    return (
        f"typeof({column})='text' AND length(CAST({column} AS BLOB))=64 "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )


_TABLES = (
    f"""CREATE TABLE metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL CHECK(typeof(schema_version)='integer'),
        session_id TEXT NOT NULL CHECK({_sql_id("session_id")}),
        manifest_digest TEXT NOT NULL CHECK({_sql_digest("manifest_digest")})
    )""",
    f"""CREATE TABLE records (
        position INTEGER PRIMARY KEY CHECK(position>0),
        record_id TEXT NOT NULL CHECK({_sql_id("record_id")}),
        kind TEXT NOT NULL CHECK(kind IN ('original','adjudication')),
        task_id TEXT NOT NULL CHECK({_sql_id("task_id")}),
        payload BLOB NOT NULL CHECK(typeof(payload)='blob'
            AND length(payload) BETWEEN 1 AND {MAX_RECORD_BYTES}),
        digest TEXT NOT NULL CHECK({_sql_digest("digest")}),
        received_at TEXT NOT NULL CHECK(typeof(received_at)='text'
            AND length(CAST(received_at AS BLOB))=32)
    )""",
    "CREATE UNIQUE INDEX records_record_id ON records(record_id)",
    "CREATE INDEX records_kind_position ON records(kind,position)",
)
_GUARDS = tuple(
    f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
        BEGIN SELECT RAISE(ABORT,'annotation_store_immutable'); END"""
    for table in ("metadata", "records")
    for action in ("UPDATE", "DELETE")
) + (
    """CREATE TRIGGER metadata_no_insert BEFORE INSERT ON metadata
        BEGIN SELECT RAISE(ABORT,'annotation_store_immutable'); END""",
    # REPLACE's implicit DELETE does not fire DELETE triggers on connections
    # with recursive_triggers OFF. Guard both unique ID and integer rowid before
    # insert, including attempts to replace through rowid/_rowid_/oid aliases.
    """CREATE TRIGGER records_guard BEFORE INSERT ON records BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM records
            WHERE record_id=NEW.record_id OR rowid=NEW.rowid)
        THEN RAISE(ABORT,'annotation_store_immutable') END;
    END""",
)
_SCHEMA = _TABLES + _GUARDS


def _database_error(exc):
    code = getattr(exc, "sqlite_errorcode", 0) & 255
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return AnnotationStoreError("annotation_store_busy")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_SCHEMA):
        return AnnotationStoreError("annotation_store_corrupt")
    if isinstance(exc, sqlite3.IntegrityError):
        return AnnotationStoreError("annotation_store_corrupt")
    return AnnotationStoreError("annotation_store_io")


@contextmanager
def _safe_errors():
    """Translate reused filesystem helpers without exposing their error domain."""
    try:
        yield
    except LedgerError as exc:
        code = {
            "ledger_unsafe_path": "annotation_store_unsafe_path",
            "ledger_busy": "annotation_store_busy",
            "ledger_corrupt": "annotation_store_corrupt",
            "ledger_requires_recovery": "annotation_store_requires_recovery",
        }.get(str(exc), "annotation_store_io")
        raise AnnotationStoreError(code) from None
    except sqlite3.Error as exc:
        raise _database_error(exc) from None
    except FileExistsError:
        raise AnnotationStoreError("annotation_store_exists") from None
    except OSError:
        raise AnnotationStoreError("annotation_store_io") from None


def _private_file(info, *, readonly=False):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in ((0o400, 0o600) if readonly else (0o600,))
        or info.st_uid != os.geteuid()
    ):
        raise AnnotationStoreError("annotation_store_unsafe_path")


def _safe_path(path, *, creating=False):
    path = _ledger_safe_path(path)
    for suffix in _SIDECARS:
        try:
            info = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _private_file(info)
        if creating:
            try:
                path.lstat()
            except FileNotFoundError:
                # Never let SQLite consume another operation's orphan sidecar.
                raise AnnotationStoreError("annotation_store_unsafe_path") from None
            # An existing database may be a concurrent creator with a journal.
            # Leave it alone: O_EXCL below reports the stable exists result.
    return path


class AnnotationStore:
    """One private SQLite connection. Use create/open and close or a with block."""

    def __init__(self, connection, path, info, *, readonly=False):
        self._connection = connection
        self._path = path
        self._info = info
        self._closed = False
        self._readonly = readonly

    @classmethod
    def create(cls, path, session_id: str, manifest_digest: str) -> AnnotationStore:
        """Exclusively create a new 0600 database; never clobber existing files."""
        _identifier(session_id)
        _digest(manifest_digest)
        with _safe_errors():
            path = _safe_path(path, creating=True)
            fd, store, info = None, None, None
            try:
                fd = os.open(
                    path,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_CLOEXEC,
                    0o600,
                )
                info = os.fstat(fd)
                os.fchmod(fd, 0o600)
                store = cls(_ledger_connect(path, info), path, info)
                with store._transaction():
                    for statement in _TABLES:
                        store._connection.execute(statement)
                    store._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    store._connection.execute(
                        "INSERT INTO metadata VALUES (1,?,?,?)",
                        (SCHEMA_VERSION, session_id, manifest_digest),
                    )
                    for statement in _GUARDS:
                        store._connection.execute(statement)
                parent_fd = os.open(
                    path.parent,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                return store
            except BaseException:
                if store is not None:
                    store.close()
                if info is not None:
                    try:
                        _ledger_same_file(path, info)
                        path.unlink()
                    except (OSError, LedgerError):
                        pass
                raise
            finally:
                if fd is not None:
                    os.close(fd)

    @classmethod
    def open(cls, path, *, readonly=False) -> AnnotationStore:
        """Verify an existing store; readonly requires the caller's session lock."""
        with _safe_errors():
            path = _safe_path(path)
            fd, store = None, None
            try:
                info = path.lstat()
                _private_file(info, readonly=readonly)
                fd = os.open(
                    path,
                    (os.O_RDONLY if readonly else os.O_RDWR)
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_CLOEXEC,
                )
                _private_file(os.fstat(fd), readonly=readonly)
                _ledger_same_file(path, os.fstat(fd))
                store = cls(
                    _ledger_connect(path, info, readonly=readonly),
                    path,
                    info,
                    readonly=readonly,
                )
                with store._transaction(write=False):
                    store._verify()
                return store
            except BaseException:
                if store is not None:
                    store.close()
                raise
            finally:
                if fd is not None:
                    os.close(fd)

    def _check_open(self):
        if self._closed:
            raise AnnotationStoreError("annotation_store_closed")

    def close(self):
        """Close once; repeated close is safe."""
        if not self._closed:
            with _safe_errors():
                self._connection.close()
                self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @contextmanager
    def _transaction(self, *, write=True):
        self._check_open()
        if write and self._readonly:
            raise AnnotationStoreError("annotation_store_readonly")
        with _safe_errors():
            _safe_path(self._path)
            if self._readonly:
                _ledger_check_readonly(self._path)
            _ledger_same_file(self._path, self._info)
            _private_file(self._path.lstat(), readonly=self._readonly)
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def _metadata(self):
        db = self._connection
        count, invalid = db.execute(
            """SELECT count(*),coalesce(sum(CASE WHEN singleton<>1
                OR typeof(schema_version)<>'integer'
                OR typeof(session_id)<>'text'
                OR length(CAST(session_id AS BLOB)) NOT BETWEEN 1 AND 128
                OR typeof(manifest_digest)<>'text'
                OR length(CAST(manifest_digest AS BLOB))<>64
                THEN 1 ELSE 0 END),0) FROM metadata"""
        ).fetchone()
        if count != 1 or invalid:
            raise AnnotationStoreError("annotation_store_corrupt")
        row = db.execute("SELECT * FROM metadata").fetchone()
        if row["schema_version"] != SCHEMA_VERSION:
            raise AnnotationStoreError("annotation_store_version")
        if not _valid(row["session_id"], _IDENTIFIER) or not _valid(
            row["manifest_digest"], _DIGEST
        ):
            raise AnnotationStoreError("annotation_store_corrupt")
        return row

    @property
    def session_id(self) -> str:
        with self._transaction(write=False):
            return self._metadata()["session_id"]

    @property
    def manifest_digest(self) -> str:
        with self._transaction(write=False):
            return self._metadata()["manifest_digest"]

    def _bounds(self):
        # Do not fetch private blobs to discover that a corrupt DB exceeds the
        # read budget. SQL aggregates operate on lengths/types before any export.
        count, total, largest, invalid = self._connection.execute(
            """SELECT count(*),coalesce(sum(length(payload)),0),
                coalesce(max(length(payload)),0),coalesce(sum(CASE WHEN
                typeof(payload)<>'blob' OR length(payload)<1
                OR typeof(position)<>'integer' OR position<1
                OR typeof(record_id)<>'text'
                OR length(CAST(record_id AS BLOB)) NOT BETWEEN 1 AND 128
                OR typeof(task_id)<>'text'
                OR length(CAST(task_id AS BLOB)) NOT BETWEEN 1 AND 128
                OR typeof(kind)<>'text' OR kind NOT IN ('original','adjudication')
                OR typeof(digest)<>'text' OR length(CAST(digest AS BLOB))<>64
                OR typeof(received_at)<>'text' OR length(CAST(received_at AS BLOB))<>32
                THEN 1 ELSE 0 END),0) FROM records"""
        ).fetchone()
        if (
            invalid
            or count > MAX_RECORDS
            or total > MAX_TOTAL_BYTES
            or largest > MAX_RECORD_BYTES
        ):
            raise AnnotationStoreError("annotation_store_corrupt")
        return count, total

    @staticmethod
    def _decode(row):
        try:
            payload = row["payload"]
            if type(payload) is not bytes or len(payload) > MAX_RECORD_BYTES:
                raise ValueError
            value = strict_json_loads(payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            if (
                _encode_record(value) != payload
                or hashlib.sha256(payload).hexdigest() != row["digest"]
                or any(
                    value[key] != row[key] for key in ("record_id", "kind", "task_id")
                )
            ):
                raise ValueError
            timestamp = row["received_at"]
            if type(timestamp) is not str or len(timestamp) != 32:
                raise ValueError
            parsed = datetime.fromisoformat(timestamp)
            if (
                parsed.tzinfo is None
                or parsed.utcoffset() != timedelta(0)
                or parsed.isoformat(timespec="microseconds") != timestamp
            ):
                raise ValueError
            return {**value, "received_at": timestamp}
        except (ValueError, TypeError, KeyError, RecursionError):
            raise AnnotationStoreError("annotation_store_corrupt") from None

    def append(self, records: list[dict]) -> dict:
        """Atomically import a bounded batch; conflicting IDs abort the batch."""
        self._check_open()
        prepared = _prepare_batch(records)
        with self._transaction():
            count, total = self._bounds()
            inserted, duplicates = 0, 0
            for record, payload, digest in prepared:
                existing = self._connection.execute(
                    "SELECT * FROM records WHERE record_id=?", (record["record_id"],)
                ).fetchone()
                if existing is not None:
                    self._decode(existing)
                    # Compare exact bytes as well as the hash, not Python JSON
                    # equality (where True == 1 == 1.0) or just task identity.
                    if existing["digest"] != digest or existing["payload"] != payload:
                        raise AnnotationStoreError("annotation_store_conflict")
                    duplicates += 1
                    continue
                if (
                    count + inserted + 1 > MAX_RECORDS
                    or total + len(payload) > MAX_TOTAL_BYTES
                ):
                    raise AnnotationStoreError("annotation_store_limit")
                self._connection.execute(
                    "INSERT INTO records(record_id,kind,task_id,payload,digest,received_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        record["record_id"],
                        record["kind"],
                        record["task_id"],
                        payload,
                        digest,
                        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    ),
                )
                inserted += 1
                total += len(payload)
            return {"inserted": inserted, "duplicates": duplicates}

    def records(self, kind: str | None = None) -> list[dict]:
        """Export private records in insertion order, optionally filtered by kind."""
        self._check_open()
        if kind is not None:
            _kind(kind)
        with self._transaction(write=False):
            self._bounds()
            cursor = self._connection.execute(
                "SELECT * FROM records "
                + ("WHERE kind=? " if kind is not None else "")
                + "ORDER BY position",
                (kind,) if kind is not None else (),
            )
            return [self._decode(row) for row in cursor]

    def get(self, record_id: str) -> dict | None:
        """Return one verified private record, or None for a valid unknown ID."""
        self._check_open()
        _identifier(record_id)
        with self._transaction(write=False):
            self._bounds()
            row = self._connection.execute(
                "SELECT * FROM records WHERE record_id=?", (record_id,)
            ).fetchone()
            return self._decode(row) if row is not None else None

    def summary(self) -> dict:
        """Return only counts; never materialize or aggregate private labels."""
        with self._transaction(write=False):
            count, _ = self._bounds()
            counts = dict(
                self._connection.execute(
                    "SELECT kind,count(*) FROM records GROUP BY kind"
                )
            )
            return {
                "record_count": count,
                "original_count": counts.get("original", 0),
                "adjudication_count": counts.get("adjudication", 0),
            }

    def _verify(self):
        db = self._connection
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise AnnotationStoreError(
                "annotation_store_corrupt"
                if version == 0
                else "annotation_store_version"
            )
        # Explicit indexes mean no implicit sqlite_* objects need an exception.
        # Compare ALL schema objects, including unrecognized views and triggers.
        expected = {
            sql.split()[3 if sql.startswith("CREATE UNIQUE INDEX") else 2]: sql.strip()
            for sql in _SCHEMA
        }
        actual = {
            row["name"]: row["sql"]
            for row in db.execute("SELECT name,sql FROM sqlite_master")
        }
        if actual != expected:
            raise AnnotationStoreError("annotation_store_corrupt")
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise AnnotationStoreError("annotation_store_corrupt")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AnnotationStoreError("annotation_store_corrupt")
        self._metadata()
        self._bounds()
        for row in db.execute("SELECT * FROM records ORDER BY position"):
            self._decode(row)
