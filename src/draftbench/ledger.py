"""Private SQLite attempt ledger: immutable plan and append-only state history.

There is exactly one attempt per logical work item, and no implicit retry. A
reserved attempt has not been called; an in-flight attempt may have been called;
a result_saved attempt already has its durable artifact. Only the caller, while
holding its process-exclusive whole-run lock, may invoke :meth:`Ledger.recover`.

Digests are lowercase SHA-256 hex. IDs use the suite's bounded ASCII identifier
profile. No prompts, results, paths, or free-form diagnostics belong in this DB.
SQLite transactions serialize transitions across independent Ledger connections.
The file's containing directory must be controlled by the caller; these checks do
not provide isolation from an adversary able to replace files during SQLite I/O.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

MAX_PLAN_ROWS = 30_000
SCHEMA_VERSION = 1
STATES = (
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
)
_FAILURE_CODES = ("adapter_failed", "adapter_limited", "invalid_adapter_output")
_SKIP_CODES = (
    "input_unavailable",
    "input_unknown",
    "input_invalid",
    "input_inapplicable",
    "dependency_unresolved",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ATTEMPT = re.compile(r"[0-9a-f]{32}")
_FIELDS = ("state", "attempt_id", "request_digest", "result_digest", "error_code")
_MOVES = {
    "planned": {"reserved", "unavailable", "blocked"},
    "reserved": {"in_flight"},
    "in_flight": {"result_saved", "failed", "limited", "uncertain"},
    "result_saved": {"completed"},
}


class LedgerError(ValueError):
    """A fixed safe code, without paths, SQLite diagnostics, or private values."""


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _identifier(value, *, attempt=False):
    if not _valid(value, _ATTEMPT if attempt else _IDENTIFIER):
        raise LedgerError("ledger_invalid_identifier")


def _digest(value):
    if not _valid(value, _DIGEST):
        raise LedgerError("ledger_invalid_digest")


def _code(value, allowed):
    if type(value) is not str or value not in allowed:
        raise LedgerError("ledger_invalid_code")


def _validate_plan(plan):
    if type(plan) is not list or not 1 <= len(plan) <= MAX_PLAN_ROWS:
        raise LedgerError("ledger_invalid_plan")
    copied = []
    known = set()
    for row in plan:
        if (
            type(row) is not dict
            or set(row) != {"work_id", "case_id", "role", "dependencies"}
            or not _valid(row["work_id"], _IDENTIFIER)
            or not _valid(row["case_id"], _IDENTIFIER)
            or type(row["role"]) is not str
            or row["role"] not in ("writer", "reviewer", "revision")
            or type(row["dependencies"]) is not list
            or len(row["dependencies"]) > MAX_PLAN_ROWS
            or any(not _valid(dep, _IDENTIFIER) for dep in row["dependencies"])
            or len(set(row["dependencies"])) != len(row["dependencies"])
            or row["work_id"] in known
        ):
            raise LedgerError("ledger_invalid_plan")
        known.add(row["work_id"])
        copied.append({**row, "dependencies": list(row["dependencies"])})
    degree = {}
    children = {key: [] for key in known}
    for row in copied:
        degree[row["work_id"]] = len(row["dependencies"])
        for dep in row["dependencies"]:
            if dep not in known:
                raise LedgerError("ledger_invalid_plan")
            children[dep].append(row["work_id"])
    # Iterative Kahn traversal also handles the maximum-size chain without a
    # recursion limit or quadratic repeated scans.
    ready = deque(key for key, count in degree.items() if count == 0)
    seen = 0
    while ready:
        seen += 1
        for child in children[ready.popleft()]:
            degree[child] -= 1
            if degree[child] == 0:
                ready.append(child)
    if seen != len(copied):
        raise LedgerError("ledger_invalid_plan")
    return copied


def _sql_values(values):
    return ",".join(f"'{value}'" for value in values)


def _sql_id(column):
    return (
        f"typeof({column})='text' AND length({column}) BETWEEN 1 AND 128 "
        f"AND {column} NOT GLOB '*[^A-Za-z0-9._-]*' "
        f"AND substr({column},1,1) GLOB '[A-Za-z0-9]'"
    )


def _sql_hex(column, length=64):
    return (
        f"typeof({column})='text' AND length({column})={length} "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )


_TABLES = (
    f"""CREATE TABLE metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL,
        manifest_digest TEXT NOT NULL CHECK({_sql_hex("manifest_digest")})
    )""",
    f"""CREATE TABLE plan (
        position INTEGER NOT NULL UNIQUE CHECK(position>=0),
        work_id TEXT PRIMARY KEY NOT NULL CHECK({_sql_id("work_id")}),
        case_id TEXT NOT NULL CHECK({_sql_id("case_id")}),
        role TEXT NOT NULL CHECK(role IN ('writer','reviewer','revision'))
    )""",
    """CREATE TABLE dependencies (
        work_id TEXT NOT NULL REFERENCES plan(work_id),
        dependency_id TEXT NOT NULL REFERENCES plan(work_id),
        position INTEGER NOT NULL CHECK(position>=0),
        PRIMARY KEY(work_id,dependency_id), UNIQUE(work_id,position),
        CHECK(work_id<>dependency_id)
    )""",
    f"""CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL CHECK({_sql_hex("attempt_id", 32)}),
        work_id TEXT NOT NULL UNIQUE REFERENCES plan(work_id),
        request_digest TEXT NOT NULL CHECK({_sql_hex("request_digest")})
    )""",
    f"""CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(event_id>0),
        work_id TEXT NOT NULL REFERENCES plan(work_id),
        state TEXT NOT NULL CHECK(state IN ({_sql_values(STATES)})),
        attempt_id TEXT REFERENCES attempts(attempt_id),
        request_digest TEXT CHECK(request_digest IS NULL OR {_sql_hex("request_digest")}),
        result_digest TEXT CHECK(result_digest IS NULL OR {_sql_hex("result_digest")}),
        error_code TEXT,
        CHECK (
            (state='planned' AND attempt_id IS NULL AND request_digest IS NULL
                AND result_digest IS NULL AND error_code IS NULL)
            OR (state IN ('unavailable','blocked') AND attempt_id IS NULL
                AND request_digest IS NULL AND result_digest IS NULL
                AND error_code IS NOT NULL AND error_code IN ({_sql_values(_SKIP_CODES)}))
            OR (state IN ('reserved','in_flight') AND attempt_id IS NOT NULL
                AND request_digest IS NOT NULL AND result_digest IS NULL
                AND error_code IS NULL)
            OR (state IN ('result_saved','completed') AND attempt_id IS NOT NULL
                AND request_digest IS NOT NULL AND result_digest IS NOT NULL
                AND error_code IS NULL)
            OR (state IN ('failed','limited') AND attempt_id IS NOT NULL
                AND request_digest IS NOT NULL AND result_digest IS NULL
                AND error_code IS NOT NULL AND error_code IN ({_sql_values(_FAILURE_CODES)}))
            OR (state='uncertain' AND attempt_id IS NOT NULL
                AND request_digest IS NOT NULL AND result_digest IS NULL
                AND error_code IS NOT NULL AND error_code='interrupted')
        )
    )""",
    "CREATE INDEX events_work_latest ON events(work_id,event_id DESC)",
    """CREATE VIEW current_states AS
        SELECT p.work_id, e.state, e.attempt_id, e.request_digest,
               e.result_digest, e.error_code
        FROM plan p JOIN events e ON e.event_id=(
            SELECT event_id FROM events WHERE work_id=p.work_id
            ORDER BY event_id DESC LIMIT 1)
    """.strip(),
)
_GUARDS = (
    tuple(
        f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
        BEGIN SELECT RAISE(ABORT,'ledger_immutable'); END"""
        for table in ("metadata", "plan", "dependencies", "attempts", "events")
        for action in ("UPDATE", "DELETE")
    )
    + tuple(
        f"""CREATE TRIGGER {table}_no_insert BEFORE INSERT ON {table}
        BEGIN SELECT RAISE(ABORT,'ledger_immutable'); END"""
        for table in ("metadata", "plan", "dependencies")
    )
    + (
        """CREATE TRIGGER attempts_guard BEFORE INSERT ON attempts BEGIN
        SELECT CASE WHEN
            EXISTS(SELECT 1 FROM attempts WHERE rowid=NEW.rowid
                OR attempt_id=NEW.attempt_id OR work_id=NEW.work_id)
            OR NOT EXISTS(SELECT 1 FROM current_states WHERE work_id=NEW.work_id AND state='planned')
            OR EXISTS(SELECT 1 FROM dependencies d LEFT JOIN current_states c
                ON c.work_id=d.dependency_id WHERE d.work_id=NEW.work_id
                AND (c.state IS NULL OR c.state<>'completed'))
            THEN RAISE(ABORT,'ledger_invalid_transition') END;
    END""",
        """CREATE TRIGGER events_guard BEFORE INSERT ON events BEGIN
        SELECT CASE WHEN NEW.event_id<>-1 OR NOT EXISTS (
            SELECT 1 FROM current_states c WHERE c.work_id=NEW.work_id AND (
                (c.state='planned' AND NEW.state IN ('unavailable','blocked')
                    AND NOT EXISTS(SELECT 1 FROM attempts WHERE work_id=NEW.work_id))
                OR (c.state='planned' AND NEW.state='reserved'
                    AND EXISTS(SELECT 1 FROM attempts a WHERE a.attempt_id=NEW.attempt_id
                        AND a.work_id=NEW.work_id AND a.request_digest=NEW.request_digest)
                    AND NOT EXISTS(SELECT 1 FROM dependencies d LEFT JOIN current_states dc
                        ON dc.work_id=d.dependency_id WHERE d.work_id=NEW.work_id
                        AND (dc.state IS NULL OR dc.state<>'completed')))
                OR (c.attempt_id=NEW.attempt_id AND c.request_digest=NEW.request_digest AND (
                    (c.state='reserved' AND NEW.state='in_flight')
                    OR (c.state='in_flight' AND NEW.state IN ('result_saved','failed','limited','uncertain'))
                    OR (c.state='result_saved' AND NEW.state='completed'
                        AND c.result_digest=NEW.result_digest)
                ))
            )) THEN RAISE(ABORT,'ledger_invalid_transition') END;
    END""",
    )
)
_SCHEMA = _TABLES + _GUARDS


def _database_error(exc):
    code = getattr(exc, "sqlite_errorcode", 0) & 255
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return LedgerError("ledger_busy")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_SCHEMA):
        return LedgerError("ledger_corrupt")
    if isinstance(exc, sqlite3.IntegrityError):
        return LedgerError("ledger_invalid_transition")
    return LedgerError("ledger_io")


def _safe_path(value):
    try:
        path = Path(value).absolute()
        for parent in path.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise LedgerError("ledger_unsafe_path")
        _check_sidecars(path)
        return path
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, LedgerError):
            raise
        raise LedgerError("ledger_unsafe_path") from None


def _check_sidecars(path):
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            info = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LedgerError("ledger_unsafe_path")


def _same_file(path, expected):
    current = path.lstat()
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise LedgerError("ledger_unsafe_path")


def _check_readonly(path):
    """Fail closed before SQLite can recover/checkpoint an input.

    The caller must hold its whole-run/session lock throughout the read. Reject
    even cold/empty sidecars; deciding whether recovery is safe is not a reader's
    job. WAL-format databases without sidecars are refused too, avoiding shared
    memory creation. No immutable=1 shortcut may hide uncheckpointed state.
    """
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise LedgerError("ledger_requires_recovery")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        header = os.read(fd, 100)
    finally:
        os.close(fd)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise LedgerError("ledger_corrupt")
    if header[18:20] == b"\x02\x02":
        raise LedgerError("ledger_requires_recovery")
    if header[18:20] != b"\x01\x01":
        raise LedgerError("ledger_corrupt")


def _connect(path, info, *, readonly=False):
    connection = None
    try:
        if readonly:
            _check_readonly(path)
        # Both modes forbid creation; as_uri encodes paths, not URI options.
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            timeout=1.0,
            isolation_level=None,
        )
        _same_file(path, info)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
        # sqlite3 disables extension loading by default. Never enable it here.
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise


class Ledger:
    """One connection; use independent instances for concurrent threads/processes."""

    def __init__(self, connection, path, info, *, readonly=False):
        self._connection = connection
        self._path = path
        self._info = info
        self._closed = False
        self._readonly = readonly

    @classmethod
    def create(cls, path, plan: list[dict], manifest_digest: str) -> Ledger:
        plan = _validate_plan(plan)
        _digest(manifest_digest)
        path = _safe_path(path)
        fd = None
        ledger = None
        info = None
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
            ledger = cls(_connect(path, info), path, info)
            with ledger._transaction():
                for statement in _TABLES:
                    ledger._connection.execute(statement)
                ledger._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                ledger._connection.execute(
                    "INSERT INTO metadata VALUES (1,?,?)",
                    (SCHEMA_VERSION, manifest_digest),
                )
                ledger._connection.executemany(
                    "INSERT INTO plan(position,work_id,case_id,role) VALUES (?,?,?,?)",
                    (
                        (i, row["work_id"], row["case_id"], row["role"])
                        for i, row in enumerate(plan)
                    ),
                )
                ledger._connection.executemany(
                    "INSERT INTO dependencies(work_id,dependency_id,position) VALUES (?,?,?)",
                    (
                        (row["work_id"], dep, i)
                        for row in plan
                        for i, dep in enumerate(row["dependencies"])
                    ),
                )
                ledger._connection.executemany(
                    "INSERT INTO events(work_id,state) VALUES (?,'planned')",
                    ((row["work_id"],) for row in plan),
                )
                # Seal only after seeding every row, inside the SAME transaction.
                for statement in _GUARDS:
                    ledger._connection.execute(statement)
            parent_fd = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return ledger
        except BaseException as exc:
            if ledger is not None:
                ledger.close()
            if info is not None:
                try:
                    _same_file(path, info)
                    path.unlink()
                except (OSError, LedgerError):
                    pass
            if isinstance(exc, FileExistsError):
                raise LedgerError("ledger_exists") from None
            if isinstance(exc, sqlite3.Error):
                raise _database_error(exc) from None
            if isinstance(exc, OSError):
                raise LedgerError("ledger_io") from None
            raise
        finally:
            if fd is not None:
                os.close(fd)

    @classmethod
    def open(cls, path, *, readonly=False) -> Ledger:
        """Verify an existing DB; readonly requires the caller's whole-run lock."""
        path = _safe_path(path)
        fd = None
        ledger = None
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LedgerError("ledger_unsafe_path")
            fd = os.open(
                path,
                (os.O_RDONLY if readonly else os.O_RDWR)
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | os.O_CLOEXEC,
            )
            _same_file(path, os.fstat(fd))
            ledger = cls(
                _connect(path, info, readonly=readonly), path, info, readonly=readonly
            )
            with ledger._transaction(write=False):
                ledger._verify()
            return ledger
        except BaseException as exc:
            if ledger is not None:
                ledger.close()
            if isinstance(exc, sqlite3.Error):
                raise _database_error(exc) from None
            if isinstance(exc, OSError):
                raise LedgerError("ledger_io") from None
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if not self._closed:
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise _database_error(exc) from None
            self._closed = True

    def __enter__(self):
        if self._closed:
            raise LedgerError("ledger_closed")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @contextmanager
    def _transaction(self, *, write=True):
        if self._closed:
            raise LedgerError("ledger_closed")
        if write and self._readonly:
            raise LedgerError("ledger_readonly")
        try:
            _same_file(self._path, self._info)
            _check_sidecars(self._path)
            if self._readonly:
                _check_readonly(self._path)
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
        except sqlite3.Error as exc:
            raise _database_error(exc) from None
        except OSError:
            raise LedgerError("ledger_io") from None

    @property
    def manifest_digest(self) -> str:
        with self._transaction(write=False):
            return self._connection.execute(
                "SELECT manifest_digest FROM metadata"
            ).fetchone()[0]

    def _plan(self):
        rows = self._connection.execute(
            "SELECT work_id,case_id,role FROM plan ORDER BY position LIMIT ?",
            (MAX_PLAN_ROWS + 1,),
        ).fetchall()
        deps = {row["work_id"]: [] for row in rows}
        for row in self._connection.execute(
            "SELECT * FROM dependencies ORDER BY work_id,position"
        ):
            if row["work_id"] not in deps:
                raise LedgerError("ledger_corrupt")
            deps[row["work_id"]].append(row["dependency_id"])
        return [{**dict(row), "dependencies": deps[row["work_id"]]} for row in rows]

    def plan(self) -> list[dict]:
        with self._transaction(write=False):
            return self._plan()

    def _current(self, work_id):
        row = self._connection.execute(
            "SELECT * FROM current_states WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise LedgerError("ledger_unknown_work")
        return dict(row)

    def state(self, work_id) -> dict:
        _identifier(work_id)
        with self._transaction(write=False):
            current = self._current(work_id)
            return {key: current[key] for key in _FIELDS}

    def _append(
        self,
        work_id,
        state,
        attempt_id=None,
        request_digest=None,
        result_digest=None,
        error_code=None,
    ):
        self._connection.execute(
            """INSERT INTO events(work_id,state,attempt_id,request_digest,result_digest,error_code)
               VALUES (?,?,?,?,?,?)""",
            (work_id, state, attempt_id, request_digest, result_digest, error_code),
        )

    def reserve(self, work_id, request_digest, *, policy=None) -> str:
        _identifier(work_id)
        _digest(request_digest)
        with self._transaction():
            if self._current(work_id)["state"] != "planned":
                raise LedgerError("ledger_invalid_transition")
            unresolved = self._connection.execute(
                """SELECT 1 FROM dependencies d JOIN current_states c ON c.work_id=d.dependency_id
                   WHERE d.work_id=? AND c.state<>'completed' LIMIT 1""",
                (work_id,),
            ).fetchone()
            if unresolved:
                raise LedgerError("ledger_dependency_unresolved")
            if policy is not None:
                count = (
                    self._connection.execute(
                        "SELECT count(*) FROM attempts"
                    ).fetchone()[0]
                    + 1
                )
                if hasattr(policy, "admit"):
                    # Provider policy uses explicit currency and separate input/output
                    # prices; admission shares the durable reservation transaction.
                    policy.admit(count)
                else:
                    tokens = count * policy.reserved_tokens
                    from fractions import Fraction

                    # Exact decimal-string values, independent of host context.
                    cost = (
                        None
                        if policy.price_per_million is None
                        else Fraction(str(policy.price_per_million))
                        * tokens
                        / 1_000_000
                    )
                    if (
                        count > policy.max_requests
                        or tokens > policy.max_total_tokens
                        or (
                            policy.max_cost_usd is not None
                            and (
                                cost is None
                                or cost > Fraction(str(policy.max_cost_usd))
                            )
                        )
                    ):
                        raise LedgerError("admission_exhausted")
            attempt = uuid.uuid4().hex
            self._connection.execute(
                "INSERT INTO attempts(attempt_id,work_id,request_digest) VALUES (?,?,?)",
                (attempt, work_id, request_digest),
            )
            self._append(work_id, "reserved", attempt, request_digest)
            return attempt

    def _transition(self, attempt_id, source, target, *, result_digest=None, code=None):
        _identifier(attempt_id, attempt=True)
        with self._transaction():
            attempt = self._connection.execute(
                "SELECT work_id FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise LedgerError("ledger_unknown_attempt")
            current = self._current(attempt["work_id"])
            if current["state"] != source or current["attempt_id"] != attempt_id:
                raise LedgerError("ledger_invalid_transition")
            self._append(
                current["work_id"],
                target,
                attempt_id,
                current["request_digest"],
                current["result_digest"] if target == "completed" else result_digest,
                code,
            )

    def start(self, attempt_id):
        self._transition(attempt_id, "reserved", "in_flight")

    def record_result(self, attempt_id, result_digest):
        _digest(result_digest)
        self._transition(
            attempt_id, "in_flight", "result_saved", result_digest=result_digest
        )

    def complete(self, attempt_id):
        self._transition(attempt_id, "result_saved", "completed")

    def fail(self, attempt_id, code: str, *, limited=False):
        _code(code, _FAILURE_CODES)
        if type(limited) is not bool:
            raise LedgerError("ledger_invalid_state")
        self._transition(
            attempt_id, "in_flight", "limited" if limited else "failed", code=code
        )

    def skip(self, work_id, *, state: str, code: str):
        _identifier(work_id)
        if type(state) is not str or state not in ("unavailable", "blocked"):
            raise LedgerError("ledger_invalid_state")
        _code(code, _SKIP_CODES)
        with self._transaction():
            if self._current(work_id)["state"] != "planned":
                raise LedgerError("ledger_invalid_transition")
            self._append(work_id, state, error_code=code)

    def recover(self):
        """Mark only in-flight attempts uncertain. Caller MUST hold the run lock.

        Repeated recovery is a no-op. Reserved attempts remain safe to start and
        result_saved attempts remain safe to complete; neither is retried.
        """
        with self._transaction():
            rows = self._connection.execute(
                "SELECT * FROM current_states WHERE state='in_flight' ORDER BY work_id",
            ).fetchall()
            for row in rows:
                self._append(
                    row["work_id"],
                    "uncertain",
                    row["attempt_id"],
                    row["request_digest"],
                    error_code="interrupted",
                )

    def events(self) -> list[dict]:
        with self._transaction(write=False):
            return [
                dict(row)
                for row in self._connection.execute(
                    "SELECT * FROM events ORDER BY event_id"
                )
            ]

    def summary(self) -> dict:
        with self._transaction(write=False):
            counts = dict.fromkeys(STATES, 0)
            counts.update(
                self._connection.execute(
                    "SELECT state,count(*) FROM current_states GROUP BY state"
                )
            )
            work_count = self._connection.execute(
                "SELECT count(*) FROM plan"
            ).fetchone()[0]
            attempt_count = self._connection.execute(
                "SELECT count(*) FROM attempts"
            ).fetchone()[0]
            if sum(counts.values()) != work_count or attempt_count > work_count:
                raise LedgerError("ledger_corrupt")
            return {
                "work_count": work_count,
                "attempt_count": attempt_count,
                "states": counts,
            }

    def _verify(self):
        """Reject unknown schemas and inconsistent history before exposing data."""
        db = self._connection
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise LedgerError("ledger_corrupt" if version == 0 else "ledger_version")
        expected = {statement.split()[2]: statement.strip() for statement in _SCHEMA}
        actual = {
            row["name"]: row["sql"]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if actual != expected:
            raise LedgerError("ledger_corrupt")
        if [row[0] for row in db.execute("PRAGMA quick_check")] != ["ok"]:
            raise LedgerError("ledger_corrupt")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LedgerError("ledger_corrupt")
        metadata = db.execute("SELECT * FROM metadata").fetchall()
        if len(metadata) != 1 or metadata[0]["singleton"] != 1:
            raise LedgerError("ledger_corrupt")
        if metadata[0]["schema_version"] != SCHEMA_VERSION:
            raise LedgerError("ledger_version")
        try:
            _digest(metadata[0]["manifest_digest"])
            plan = _validate_plan(self._plan())
            self._verify_history(plan)
        except (LedgerError, KeyError, TypeError, ValueError):
            raise LedgerError("ledger_corrupt") from None

    def _verify_history(self, plan):
        dependencies = {row["work_id"]: row["dependencies"] for row in plan}
        attempts = {}
        for row in self._connection.execute(
            "SELECT * FROM attempts LIMIT ?", (MAX_PLAN_ROWS + 1,)
        ):
            if row["work_id"] not in dependencies or row["work_id"] in attempts:
                raise LedgerError("ledger_corrupt")
            _identifier(row["attempt_id"], attempt=True)
            _digest(row["request_digest"])
            attempts[row["work_id"]] = (row["attempt_id"], row["request_digest"])
        latest = {}
        reserved = set()
        count = 0
        for row in self._connection.execute("SELECT * FROM events ORDER BY event_id"):
            count += 1
            work, state = row["work_id"], row["state"]
            if count > 5 * len(plan) or work not in dependencies:
                raise LedgerError("ledger_corrupt")
            previous = latest.get(work)
            if previous is None:
                if state != "planned":
                    raise LedgerError("ledger_corrupt")
            elif state not in _MOVES.get(previous["state"], set()):
                raise LedgerError("ledger_corrupt")
            if row["attempt_id"] is not None:
                if attempts.get(work) != (row["attempt_id"], row["request_digest"]):
                    raise LedgerError("ledger_corrupt")
            if state == "reserved":
                if work in reserved or any(
                    latest.get(dep, {}).get("state") != "completed"
                    for dep in dependencies[work]
                ):
                    raise LedgerError("ledger_corrupt")
                reserved.add(work)
            if state == "completed" and (
                previous is None or row["result_digest"] != previous["result_digest"]
            ):
                raise LedgerError("ledger_corrupt")
            latest[work] = dict(row)
        if set(latest) != set(dependencies) or set(attempts) != reserved:
            raise LedgerError("ledger_corrupt")
