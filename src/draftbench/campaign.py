"""Provider-neutral, offline shared USD admission; never dispatches or refunds.

One explicitly created database is the authority for one campaign. All runs must
use it; creating another file is a new authority, NOT a way to resume. Trusted
host code owns this association and must not replace/restore/copy the database.
SQLite FULL synchronous transactions on a local filesystem serialize processes.
An acknowledged or ambiguously committed reservation is retained forever, even
if local admission, dispatch, result persistence or billing later fails.
"""

import hashlib
import os
import sqlite3
import uuid
from decimal import Decimal

from .identity import canonical_bytes
from .ledger import (
    Ledger,
    LedgerError,
    _connect,
    _database_error,
    _digest,
    _identifier,
    _safe_path,
    _sql_hex,
    _sql_id,
)

_SCALE = 10**14
_CEILING = 50 * _SCALE
_TABLES = (
    """CREATE TABLE campaign (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        campaign_id TEXT NOT NULL,
        currency TEXT NOT NULL CHECK(currency='USD'),
        ceiling INTEGER NOT NULL CHECK(ceiling=5000000000000000)
    )""",
    f"""CREATE TABLE reservations (
        attempt_id TEXT PRIMARY KEY NOT NULL CHECK({_sql_hex("attempt_id", 32)}),
        run_digest TEXT NOT NULL CHECK({_sql_hex("run_digest")}),
        request_digest TEXT NOT NULL CHECK({_sql_hex("request_digest")}),
        config_digest TEXT NOT NULL CHECK({_sql_hex("config_digest")}),
        provider TEXT NOT NULL CHECK({_sql_id("provider")}),
        binding TEXT NOT NULL CHECK({_sql_hex("binding")}),
        units INTEGER NOT NULL CHECK(typeof(units)='integer' AND units>0
            AND units<=5000000000000000)
    )""",
)
_GUARDS = tuple(
    f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
        BEGIN SELECT RAISE(ABORT,'campaign_immutable'); END"""
    for table in ("campaign", "reservations")
    for action in ("UPDATE", "DELETE")
) + (
    """CREATE TRIGGER campaign_no_insert BEFORE INSERT ON campaign
        BEGIN SELECT RAISE(ABORT,'campaign_immutable'); END""",
    """CREATE TRIGGER reservations_guard BEFORE INSERT ON reservations BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM reservations WHERE attempt_id=NEW.attempt_id)
            OR NEW.units + (SELECT coalesce(sum(units),0) FROM reservations)
                > (SELECT ceiling FROM campaign WHERE singleton=1)
            THEN RAISE(ABORT,'campaign_exhausted') END;
    END""",
)


_TRACKING_TABLES = (
    _TABLES[0].replace(
        "ceiling INTEGER NOT NULL CHECK(ceiling=5000000000000000)",
        "ceiling INTEGER CHECK(ceiling IS NULL)",
    ),
    _TABLES[1].replace(
        "units INTEGER NOT NULL CHECK(typeof(units)='integer' AND units>0\n            AND units<=5000000000000000)",
        "units INTEGER CHECK(units IS NULL)",
    ),
)
_TRACKING_GUARDS = _GUARDS[:-1] + (
    """CREATE TRIGGER reservations_guard BEFORE INSERT ON reservations BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM reservations WHERE attempt_id=NEW.attempt_id)
            THEN RAISE(ABORT,'campaign_duplicate') END;
    END""",
)


def _units(amount):
    # Do not accept floats or unknown quotes. Integer arithmetic is independent
    # of the caller's Decimal context; round UP, never under-reserve fractions.
    if (
        type(amount) is not Decimal
        or not amount.is_finite()
        or not 0 < amount <= 50
        or len(amount.as_tuple().digits) > 100
        or not -100 <= amount.as_tuple().exponent <= 2
    ):
        raise ValueError("campaign_invalid_upper_bound")
    numerator, denominator = amount.as_integer_ratio()
    return (numerator * _SCALE + denominator - 1) // denominator, (
        numerator,
        denominator,
    )


def _money(units):
    return f"{units // _SCALE}.{units % _SCALE:014d}"


class CampaignBudget(Ledger):
    """Reuse the ledger's checked file lifecycle and transaction primitives only.

    `reserve` is admission, not authorization to call a provider. Dispatch still
    requires a durable local attempt/start and the existing trusted approval.
    There is deliberately no release, settlement, reset or ceiling-update API.
    """

    @classmethod
    def create(cls, path, *, tracking_only=False):
        if type(tracking_only) is not bool:
            raise ValueError("invalid_tracking_mode")
        path = _safe_path(path)
        fd = None
        budget = None
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            budget = cls(_connect(path, info), path, info)
            with budget._transaction():
                for statement in _TRACKING_TABLES if tracking_only else _TABLES:
                    budget._connection.execute(statement)
                budget._connection.execute(
                    f"PRAGMA user_version={2 if tracking_only else 1}"
                )
                budget._connection.execute(
                    "INSERT INTO campaign VALUES (1,?,?,?)",
                    (uuid.uuid4().hex, "USD", None if tracking_only else _CEILING),
                )
                for statement in _TRACKING_GUARDS if tracking_only else _GUARDS:
                    budget._connection.execute(statement)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            return budget
        except BaseException as exc:
            if budget is not None:
                budget.close()
            # Never unlink on ambiguous commit/fsync failure. Existing files are
            # opened and verified, not silently recreated or reset.
            if isinstance(exc, FileExistsError):
                raise ValueError("campaign_exists") from None
            if isinstance(exc, sqlite3.Error):
                raise _database_error(exc) from None
            if isinstance(exc, OSError):
                raise LedgerError("ledger_io") from None
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def _verify(self):
        db = self._connection
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        tracking = schema_version == 2
        tables = _TRACKING_TABLES if tracking else _TABLES
        guards = _TRACKING_GUARDS if tracking else _GUARDS
        expected = {s.split()[2]: s.strip() for s in tables + guards}
        actual = {
            r["name"]: r["sql"]
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        rows = db.execute("SELECT * FROM campaign").fetchall()
        if (
            schema_version not in (1, 2)
            or actual != expected
            or len(rows) != 1
            or rows[0]["singleton"] != 1
            or rows[0]["currency"] != "USD"
            or rows[0]["ceiling"] != (None if tracking else _CEILING)
            or [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]
        ):
            raise ValueError("campaign_corrupt")
        _identifier(rows[0]["campaign_id"], attempt=True)
        if not tracking and self._total() > _CEILING:
            raise ValueError("campaign_corrupt")

    def _identity(self):
        row = self._connection.execute("SELECT * FROM campaign").fetchone()
        return {
            "campaign_id": row["campaign_id"],
            "currency": row["currency"],
            "ceiling_usd": "50.00" if row["ceiling"] is not None else None,
        }

    @property
    def identity(self):
        with self._transaction(write=False):
            return self._identity()

    def _total(self):
        return self._connection.execute(
            "SELECT coalesce(sum(units),0) FROM reservations"
        ).fetchone()[0]

    def summary(self):
        with self._transaction(write=False):
            return {
                **self._identity(),
                "reserved_usd": _money(self._total())
                if self._identity()["ceiling_usd"] is not None
                else None,
                "reservation_count": self._connection.execute(
                    "SELECT count(*) FROM reservations"
                ).fetchone()[0],
            }

    def reserve(
        self,
        *,
        attempt_id,
        run_digest,
        request_digest,
        config_digest,
        provider,
        currency,
        upper_bound,
    ):
        _identifier(attempt_id, attempt=True)
        _identifier(provider)
        for value in (run_digest, request_digest, config_digest):
            _digest(value)
        if currency != "USD":
            raise ValueError("campaign_currency")
        tracking = self.identity["ceiling_usd"] is None
        if tracking:
            if upper_bound is not None:
                raise ValueError("tracking_campaign_requires_no_reservation")
            units, exact_amount = None, None
        else:
            units, exact_amount = _units(upper_bound)
        binding = hashlib.sha256(
            canonical_bytes(
                {
                    "attempt_id": attempt_id,
                    "run_digest": run_digest,
                    "request_digest": request_digest,
                    "config_digest": config_digest,
                    "provider": provider,
                    "currency": currency,
                    "amount_ratio": list(exact_amount)
                    if exact_amount is not None
                    else None,
                }
            )
        ).hexdigest()
        with self._transaction():
            existing = self._connection.execute(
                "SELECT binding FROM reservations WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                if existing["binding"] != binding:
                    raise ValueError("campaign_binding_mismatch")
                return attempt_id
            if not tracking and self._total() + units > _CEILING:
                raise ValueError("campaign_exhausted")
            self._connection.execute(
                "INSERT INTO reservations VALUES (?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    run_digest,
                    request_digest,
                    config_digest,
                    provider,
                    binding,
                    units,
                ),
            )
        return attempt_id
