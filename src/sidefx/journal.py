"""Durable SQLite WAL outbound-effects journal with atomic state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from sidefx.config import SidefxConfig

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 30_000
DEFAULT_DB_RELATIVE_PATH = Path(".sidefx") / "journal.db"
ENV_DB_PATH = "SIDEFX_DB"
_MARKER_HEX_LENGTH = 24
_WAL_INIT_MAX_RETRIES = 8
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET: Any = object()

_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "action_id",
        "session_id",
        "actor",
        "surface",
        "tool_name",
        "target",
        "payload_hash",
        "marker",
        "status",
        "attempt",
        "result_ref",
        "summary",
        "error",
        "created_at",
        "updated_at",
    }
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS actions (
        action_id TEXT NOT NULL CHECK (length(action_id) > 0),
        session_id TEXT NOT NULL CHECK (length(session_id) > 0),
        actor TEXT NOT NULL CHECK (length(actor) > 0),
        surface TEXT NOT NULL CHECK (surface IN ('mcp', 'cli', 'http')),
        tool_name TEXT NOT NULL CHECK (length(tool_name) > 0),
        target TEXT NOT NULL CHECK (length(target) > 0),
        payload_hash TEXT NOT NULL CHECK (length(payload_hash) > 0),
        marker TEXT NOT NULL CHECK (length(marker) > 0),
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'confirmed', 'failed', 'reconciled', 'unknown')
        ),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
        result_ref TEXT,
        summary TEXT,
        error TEXT,
        created_at TEXT NOT NULL CHECK (length(created_at) > 0),
        updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
        UNIQUE (actor, target, action_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_session_created
        ON actions (session_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_status_created
        ON actions (status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_marker
        ON actions (marker)
    """,
)

_SELECT_COLUMNS = (
    "action_id, session_id, actor, surface, tool_name, target, "
    "payload_hash, marker, status, attempt, result_ref, summary, error, "
    "created_at, updated_at"
)


class Surface(StrEnum):
    """Execution surface that initiated a side effect."""

    MCP = "mcp"
    CLI = "cli"
    HTTP = "http"


class ActionStatus(StrEnum):
    """Durable lifecycle state of a journaled side effect."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    RECONCILED = "reconciled"
    UNKNOWN = "unknown"


class BeginDecision(StrEnum):
    """Outcome of attempting to atomically claim an action before dispatch."""

    PROCEED = "proceed"
    REPLAY = "replay"
    OUTCOME_UNKNOWN = "outcome_unknown"


class JournalError(Exception):
    """Base exception for journal errors."""


class PayloadMismatchError(JournalError):
    """Raised when an existing action ID is reused with a different payload, tool, or surface."""

    def __init__(
        self,
        *,
        actor: str,
        target: str,
        action_id: str,
        expected_hash: str,
        actual_hash: str,
        detail: str | None = None,
    ) -> None:
        self.actor = actor
        self.target = target
        self.action_id = action_id
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        self.detail = detail
        msg = (
            f"Payload mismatch for action ({actor!r}, {target!r}, {action_id!r}): "
            f"expected {expected_hash}, got {actual_hash}"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


class InvalidStateTransitionError(JournalError):
    """Raised when an invalid action lifecycle state transition is attempted."""

    def __init__(
        self,
        *,
        actor: str,
        target: str,
        action_id: str,
        current_status: ActionStatus,
        target_status: ActionStatus,
        detail: str | None = None,
    ) -> None:
        self.actor = actor
        self.target = target
        self.action_id = action_id
        self.current_status = current_status
        self.target_status = target_status
        msg = (
            f"Invalid state transition for action ({actor!r}, {target!r}, {action_id!r}): "
            f"{current_status.value!r} -> {target_status.value!r}"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


class StaleAttemptError(JournalError):
    """Raised when a finalizer targets an outdated attempt number."""

    def __init__(
        self,
        *,
        actor: str,
        target: str,
        action_id: str,
        expected_attempt: int,
        actual_attempt: int,
    ) -> None:
        self.actor = actor
        self.target = target
        self.action_id = action_id
        self.expected_attempt = expected_attempt
        self.actual_attempt = actual_attempt
        super().__init__(
            f"Stale attempt for action ({actor!r}, {target!r}, {action_id!r}): "
            f"expected attempt {expected_attempt}, current attempt is {actual_attempt}"
        )


class ActionNotFoundError(JournalError):
    """Raised when attempting to update an action that does not exist in the journal."""

    def __init__(self, *, actor: str, target: str, action_id: str) -> None:
        self.actor = actor
        self.target = target
        self.action_id = action_id
        super().__init__(f"Action not found: ({actor!r}, {target!r}, {action_id!r})")


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Immutable view of one durable row in the actions journal."""

    action_id: str
    session_id: str
    actor: str
    surface: Surface
    tool_name: str
    target: str
    payload_hash: str
    marker: str
    status: ActionStatus
    attempt: int = 1
    result_ref: str | None = None
    summary: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary representation."""
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "actor": self.actor,
            "surface": self.surface.value,
            "tool_name": self.tool_name,
            "target": self.target,
            "payload_hash": self.payload_hash,
            "marker": self.marker,
            "status": self.status.value,
            "attempt": self.attempt,
            "result_ref": self.result_ref,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BeginResult:
    """Result returned by `Journal.begin_action`."""

    decision: BeginDecision
    record: ActionRecord
    retried: bool = False


def _validate_json_keys(value: Any) -> None:
    """Ensure all mapping keys are plain strings so canonical JSON cannot collide."""
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"Payload dictionary keys must be strings, got {type(k).__name__}")
            _validate_json_keys(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_keys(item)


def compute_payload_hash(payload: Any) -> str:
    """Compute a deterministic SHA-256 hex digest over canonical ASCII JSON.

    Canonicalization guarantees:
      - Dictionary keys must be strings (rejects int/bool/mixed keys to prevent collisions).
      - ``ensure_ascii=True`` safely handles lone Unicode surrogates without encoding errors.
      - ``NaN``, ``Infinity``, and non-JSON types are rejected.
      - Distinct JSON representations (such as ``1`` vs ``1.0``, ``True`` vs ``1``, or
        NFC vs NFD Unicode) produce distinct hashes (fail-safe).
    """
    _validate_json_keys(payload)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def derive_marker(actor: str, target: str, action_id: str) -> str:
    """Derive a compact deterministic URL/comment-safe hex token for reconciliation."""
    _require_non_empty("actor", actor)
    _require_non_empty("target", target)
    _require_non_empty("action_id", action_id)
    encoded = json.dumps(
        ["sidefx-marker-v1", actor, target, action_id],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:_MARKER_HEX_LENGTH]


def resolve_db_path(
    db_path: str | Path | None = None,
    *,
    config: SidefxConfig | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the journal database path consistently across CLI and library callers.

    Precedence:
      1. Explicit ``db_path`` argument.
      2. ``SIDEFX_DB`` environment variable.
      3. ``config.journal_path`` (relative paths resolved against ``config.source_path.parent``
         when discovered from a file, else ``cwd``).
      4. ``config.source_path.parent / ".sidefx/journal.db"`` when a config file was discovered.
      5. ``<cwd>/.sidefx/journal.db``.
    """
    if db_path is not None:
        raw = str(db_path).strip()
        if not raw:
            raise ValueError("db_path must not be empty")
        return Path(raw).expanduser()

    env_val = os.environ.get(ENV_DB_PATH, "").strip()
    if env_val:
        return Path(env_val).expanduser()

    base = cwd if cwd is not None else Path.cwd()
    if config is not None:
        if config.journal_path is not None:
            configured = Path(config.journal_path).expanduser()
            if not configured.is_absolute():
                anchor = config.source_path.parent if config.source_path is not None else base
                return anchor / configured
            return configured
        if config.source_path is not None:
            return config.source_path.parent / DEFAULT_DB_RELATIVE_PATH

    return base / DEFAULT_DB_RELATIVE_PATH


def _write_journal_gitignore(journal_dir: Path) -> None:
    """Keep a journal directory sidefx just created out of ``git status``.

    The journal is local execution state (SQLite + WAL sidecars), so an agent running
    ``git add -A`` must not sweep it into a commit. Best effort: a read-only or exotic
    filesystem must never stop the journal from opening.
    """
    with suppress(OSError):
        (journal_dir / ".gitignore").write_text(
            "# Created by sidefx: the effects journal is local state, not source.\n*\n",
            encoding="utf-8",
        )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_non_empty(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _validate_optional_text(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _validate_attempt(attempt: int | None) -> int | None:
    if attempt is None:
        return None
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    return attempt


def _coerce_surface(surface: Surface | str) -> Surface:
    if isinstance(surface, Surface):
        return surface
    if isinstance(surface, str):
        try:
            return Surface(surface)
        except ValueError as exc:
            valid = ", ".join(s.value for s in Surface)
            raise ValueError(f"Invalid surface {surface!r}; expected one of: {valid}") from exc
    raise ValueError(f"Invalid surface type: {type(surface).__name__}")


def _coerce_status(status: ActionStatus | str) -> ActionStatus:
    if isinstance(status, ActionStatus):
        return status
    if isinstance(status, str):
        try:
            return ActionStatus(status)
        except ValueError as exc:
            valid = ", ".join(s.value for s in ActionStatus)
            raise ValueError(f"Invalid status {status!r}; expected one of: {valid}") from exc
    raise ValueError(f"Invalid status type: {type(status).__name__}")


def _row_to_record(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        action_id=row["action_id"],
        session_id=row["session_id"],
        actor=row["actor"],
        surface=Surface(row["surface"]),
        tool_name=row["tool_name"],
        target=row["target"],
        payload_hash=row["payload_hash"],
        marker=row["marker"],
        status=ActionStatus(row["status"]),
        attempt=int(row["attempt"]),
        result_ref=row["result_ref"],
        summary=row["summary"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _is_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        busy_codes = {
            getattr(sqlite3, "SQLITE_BUSY", 5),
            getattr(sqlite3, "SQLITE_LOCKED", 6),
        }
        if (code & 0xFF) in busy_codes:
            return True
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _sqlite_ro_uri(path: Path, *, immutable: bool = False) -> str:
    abs_path = os.path.normpath(os.path.abspath(path))
    encoded = quote(abs_path, safe="/:")
    if not encoded.startswith("/"):
        encoded = f"/{encoded}"
    suffix = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"file:{encoded}?{suffix}"


class Journal:
    """Write-ahead durable SQLite WAL journal for outbound side effects.

    Note: On macOS, ``PRAGMA fullfsync=ON`` and ``PRAGMA checkpoint_fullfsync=ON`` are
    enabled alongside ``PRAGMA synchronous=FULL`` to flush disk write caches on commit.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        config: SidefxConfig | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        read_only: bool = False,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be >= 0")

        self._is_memory = isinstance(db_path, str) and db_path == ":memory:"
        if self._is_memory and read_only:
            raise ValueError("Cannot open ':memory:' journal in read_only mode")

        self._read_only = read_only
        self._db_path: Path | None = (
            None if self._is_memory else resolve_db_path(db_path, config=config)
        )
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = self._open_and_initialize()

    @classmethod
    def open_readonly(
        cls,
        db_path: str | Path | None = None,
        *,
        config: SidefxConfig | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> Journal:
        """Open an existing journal strictly read-only without mutating the file or schema."""
        return cls(
            db_path,
            config=config,
            busy_timeout_ms=busy_timeout_ms,
            read_only=True,
        )

    @property
    def db_path(self) -> Path | None:
        """Resolved filesystem path to the SQLite database, or None for ``:memory:``."""
        return self._db_path

    @property
    def read_only(self) -> bool:
        """Whether this journal handle was opened strictly read-only."""
        return self._read_only

    def _open_and_initialize(self) -> sqlite3.Connection:
        if self._read_only:
            return self._open_readonly_conn()

        if self._db_path is not None:
            if self._db_path.is_dir():
                raise JournalError(
                    f"Journal database path is a directory, expected a file: {self._db_path}"
                )
            journal_dir = self._db_path.parent
            created_dir = not journal_dir.exists()
            try:
                journal_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise JournalError(
                    f"Failed to create journal directory {journal_dir}: {exc}"
                ) from exc
            if created_dir:
                _write_journal_gitignore(journal_dir)
            target_str = str(self._db_path)
        else:
            target_str = ":memory:"

        timeout_s = max(self._busy_timeout_ms / 1000.0, 0.1)
        try:
            conn = sqlite3.connect(
                target_str,
                timeout=timeout_s,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise JournalError(f"Failed to open journal database {target_str}: {exc}") from exc

        conn.row_factory = sqlite3.Row
        try:
            self._configure_and_migrate(conn)
        except JournalError:
            conn.close()
            raise
        except sqlite3.Error as exc:
            conn.close()
            raise JournalError(
                f"Failed to initialize journal database {target_str}: {exc}"
            ) from exc
        except Exception:
            conn.close()
            raise
        return conn

    def _open_readonly_conn(self) -> sqlite3.Connection:
        assert self._db_path is not None
        if not self._db_path.exists():
            raise JournalError(f"Journal database not found: {self._db_path}")
        if self._db_path.is_dir():
            raise JournalError(
                f"Journal database path is a directory, expected a file: {self._db_path}"
            )

        timeout_s = max(self._busy_timeout_ms / 1000.0, 0.1)
        uri = _sqlite_ro_uri(self._db_path, immutable=False)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout_s,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA query_only = ON")
            self._verify_existing_schema(conn)
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
                conn = None
            # A WAL-mode DB inside a read-only directory cannot create a `-shm` file.
            # If no uncheckpointed `-wal` data exists, immutable=1 is safe and read-only.
            wal_path = Path(f"{self._db_path}-wal")
            has_uncheckpointed_wal = wal_path.exists() and wal_path.stat().st_size > 0
            if "readonly" in str(exc).lower() and not has_uncheckpointed_wal:
                return self._open_immutable_readonly_conn(timeout_s)
            raise JournalError(f"Failed to read journal database {self._db_path}: {exc}") from exc
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise JournalError(f"Failed to read journal database {self._db_path}: {exc}") from exc
        except JournalError:
            if conn is not None:
                conn.close()
            raise

    def _open_immutable_readonly_conn(self, timeout_s: float) -> sqlite3.Connection:
        assert self._db_path is not None
        uri = _sqlite_ro_uri(self._db_path, immutable=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout_s,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            self._verify_existing_schema(conn)
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise JournalError(f"Failed to read journal database {self._db_path}: {exc}") from exc
        except JournalError:
            if conn is not None:
                conn.close()
            raise

    def _verify_existing_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise JournalError(
                f"Unsupported journal schema version {version} in {self._db_path}; "
                f"maximum supported version is {SCHEMA_VERSION}"
            )
        table_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
        ).fetchone()
        if table_row is None:
            raise JournalError(
                f"Database {self._db_path} is not a valid sidefx journal (missing 'actions' table)"
            )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
        missing = sorted(_REQUIRED_COLUMNS - cols)
        if missing:
            raise JournalError(
                f"Database {self._db_path} has an incompatible 'actions' table "
                f"(missing columns: {', '.join(missing)})"
            )

    def _configure_and_migrate(self, conn: sqlite3.Connection) -> None:
        # Set busy_timeout first so concurrent process startups wait instead of failing.
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")

        # Reject future schema versions BEFORE mutating journal_mode.
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise JournalError(
                f"Unsupported journal schema version {version}; "
                f"maximum supported version is {SCHEMA_VERSION}"
            )

        if not self._is_memory:
            current_mode_row = conn.execute("PRAGMA journal_mode").fetchone()
            current_mode = str(current_mode_row[0]).lower() if current_mode_row else ""
            if current_mode != "wal":
                # Retry only the WAL-mode switch on transient cold-start contention.
                for attempt in range(_WAL_INIT_MAX_RETRIES):
                    try:
                        mode_row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
                        mode = str(mode_row[0]).lower() if mode_row else ""
                        if mode != "wal":
                            raise JournalError(
                                f"Failed to enable SQLite WAL mode on {self._db_path}; got {mode!r}"
                            )
                        break
                    except sqlite3.OperationalError as exc:
                        if _is_busy_or_locked(exc) and attempt < _WAL_INIT_MAX_RETRIES - 1:
                            time.sleep(0.01 * (attempt + 1) + random.uniform(0.0, 0.02))
                            continue
                        raise

        conn.execute("PRAGMA synchronous = FULL")
        if not self._is_memory and sys.platform == "darwin":
            conn.execute("PRAGMA fullfsync = ON")
            conn.execute("PRAGMA checkpoint_fullfsync = ON")

        # Fast path for already-migrated journals: verify schema without taking a write lock.
        if version == SCHEMA_VERSION:
            self._verify_existing_schema(conn)
            return

        # Run initial schema migration inside a single BEGIN IMMEDIATE transaction
        # (relying directly on busy_timeout rather than multiplying retries).
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise JournalError(
                    f"Unsupported journal schema version {version}; "
                    f"maximum supported version is {SCHEMA_VERSION}"
                )
            if version == 0:
                existing_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
                ).fetchone()
                if existing_table is not None:
                    cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
                    missing = sorted(_REQUIRED_COLUMNS - cols)
                    if missing:
                        raise JournalError(
                            f"Database {self._db_path} has an incompatible 'actions' table "
                            f"(missing columns: {', '.join(missing)})"
                        )
                for stmt in _SCHEMA_STATEMENTS:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                self._verify_existing_schema(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise JournalError("Journal connection is closed")
        return self._conn

    @contextmanager
    def _immediate_tx(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            raise JournalError("Cannot mutate a read-only journal")
        with self._lock:
            conn = self._require_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise JournalError(f"SQLite transaction failed: {exc}") from exc
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def pragmas(self) -> dict[str, Any]:
        """Return active SQLite durability and locking PRAGMA values."""
        with self._lock:
            conn = self._require_conn()
            try:
                result: dict[str, Any] = {
                    "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                    "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
                    "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                    "busy_timeout": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
                    "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
                }
                if sys.platform == "darwin":
                    result["fullfsync"] = int(conn.execute("PRAGMA fullfsync").fetchone()[0])
                return result
            except sqlite3.Error as exc:
                raise JournalError(f"Failed to query PRAGMAs: {exc}") from exc

    def begin_action(
        self,
        *,
        action_id: str,
        session_id: str,
        actor: str,
        surface: Surface | str,
        tool_name: str,
        target: str,
        payload: Any = _UNSET,
        payload_hash: str | None = None,
        marker: str | None = None,
        summary: str | None = None,
    ) -> BeginResult:
        """Atomically claim an action before dispatching any external side effect.

        Decisions:
          - ``PROCEED``: No prior row existed (inserted as ``pending``, ``attempt=1``),
            or a prior ``failed`` row with identical payload was atomically transitioned
            back to ``pending`` with ``attempt = attempt + 1`` (``retried=True``).
          - ``REPLAY``: Action already ``confirmed`` or ``reconciled`` with matching
            payload; caller should return cached ``result_ref``/``summary`` without
            re-executing.
          - ``OUTCOME_UNKNOWN``: Action is currently ``pending`` or ``unknown``;
            caller must NOT blindly retry until reconciled.

        Raises:
          - ``PayloadMismatchError`` if ``(actor, target, action_id)`` already exists
            with a different ``payload_hash``, ``tool_name``, ``surface``, or explicit
            ``marker``.
        """
        _require_non_empty("action_id", action_id)
        _require_non_empty("session_id", session_id)
        _require_non_empty("actor", actor)
        _require_non_empty("tool_name", tool_name)
        _require_non_empty("target", target)
        surface_enum = _coerce_surface(surface)
        _validate_optional_text("summary", summary)

        if payload is _UNSET and payload_hash is None:
            raise ValueError("Either payload or payload_hash must be provided")

        if payload_hash is not None:
            _require_non_empty("payload_hash", payload_hash)
            if not _SHA256_HEX_RE.match(payload_hash):
                raise ValueError("payload_hash must be a 64-character lowercase hex SHA-256 digest")
            if payload is not _UNSET:
                computed = compute_payload_hash(payload)
                if computed != payload_hash:
                    raise ValueError(
                        "Provided payload_hash does not match computed hash of payload"
                    )
            resolved_hash = payload_hash
        else:
            resolved_hash = compute_payload_hash(payload)

        explicit_marker = _require_non_empty("marker", marker) if marker is not None else None
        resolved_marker = (
            explicit_marker
            if explicit_marker is not None
            else derive_marker(actor, target, action_id)
        )

        with self._immediate_tx() as conn:
            existing_row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM actions "
                "WHERE actor = ? AND target = ? AND action_id = ?",
                (actor, target, action_id),
            ).fetchone()

            if existing_row is None:
                now = _utc_now()
                conn.execute(
                    "INSERT INTO actions ("
                    "action_id, session_id, actor, surface, tool_name, target, "
                    "payload_hash, marker, status, attempt, result_ref, summary, error, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, NULL, ?, ?)",
                    (
                        action_id,
                        session_id,
                        actor,
                        surface_enum.value,
                        tool_name,
                        target,
                        resolved_hash,
                        resolved_marker,
                        ActionStatus.PENDING.value,
                        summary,
                        now,
                        now,
                    ),
                )
                created = ActionRecord(
                    action_id=action_id,
                    session_id=session_id,
                    actor=actor,
                    surface=surface_enum,
                    tool_name=tool_name,
                    target=target,
                    payload_hash=resolved_hash,
                    marker=resolved_marker,
                    status=ActionStatus.PENDING,
                    attempt=1,
                    result_ref=None,
                    summary=summary,
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
                return BeginResult(decision=BeginDecision.PROCEED, record=created, retried=False)

            existing = _row_to_record(existing_row)

            if existing.payload_hash != resolved_hash:
                raise PayloadMismatchError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_hash=existing.payload_hash,
                    actual_hash=resolved_hash,
                )
            if existing.tool_name != tool_name:
                raise PayloadMismatchError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_hash=existing.payload_hash,
                    actual_hash=resolved_hash,
                    detail=(
                        f"tool_name mismatch: expected {existing.tool_name!r}, got {tool_name!r}"
                    ),
                )
            if existing.surface != surface_enum:
                raise PayloadMismatchError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_hash=existing.payload_hash,
                    actual_hash=resolved_hash,
                    detail=(
                        f"surface mismatch: expected {existing.surface.value!r}, "
                        f"got {surface_enum.value!r}"
                    ),
                )
            if explicit_marker is not None and existing.marker != explicit_marker:
                raise PayloadMismatchError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_hash=existing.payload_hash,
                    actual_hash=resolved_hash,
                    detail=(
                        f"marker mismatch: expected {existing.marker!r}, got {explicit_marker!r}"
                    ),
                )

            if existing.status in (ActionStatus.CONFIRMED, ActionStatus.RECONCILED):
                return BeginResult(decision=BeginDecision.REPLAY, record=existing, retried=False)

            if existing.status in (ActionStatus.PENDING, ActionStatus.UNKNOWN):
                return BeginResult(
                    decision=BeginDecision.OUTCOME_UNKNOWN,
                    record=existing,
                    retried=False,
                )

            # existing.status == ActionStatus.FAILED -> safe atomic retry with attempt fence
            now = _utc_now()
            next_attempt = existing.attempt + 1
            next_summary = summary if summary is not None else existing.summary
            cursor = conn.execute(
                "UPDATE actions "
                "SET status = ?, attempt = ?, session_id = ?, error = NULL, result_ref = NULL, "
                "summary = ?, updated_at = ? "
                "WHERE actor = ? AND target = ? AND action_id = ? AND status = ? AND attempt = ?",
                (
                    ActionStatus.PENDING.value,
                    next_attempt,
                    session_id,
                    next_summary,
                    now,
                    actor,
                    target,
                    action_id,
                    ActionStatus.FAILED.value,
                    existing.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"Failed atomic retry transition for ({actor!r}, {target!r}, {action_id!r})"
                )

            updated = ActionRecord(
                action_id=existing.action_id,
                session_id=session_id,
                actor=existing.actor,
                surface=existing.surface,
                tool_name=existing.tool_name,
                target=existing.target,
                payload_hash=existing.payload_hash,
                marker=existing.marker,
                status=ActionStatus.PENDING,
                attempt=next_attempt,
                result_ref=None,
                summary=next_summary,
                error=None,
                created_at=existing.created_at,
                updated_at=now,
            )
            return BeginResult(decision=BeginDecision.PROCEED, record=updated, retried=True)

    def mark_confirmed(
        self,
        actor: str,
        target: str,
        action_id: str,
        *,
        attempt: int | None = None,
        result_ref: str | None = None,
        summary: str | None = None,
    ) -> ActionRecord:
        """Transition a ``pending`` action to ``confirmed``.

        Idempotent if already ``confirmed`` with the exact same ``result_ref`` (and matching
        ``attempt`` when provided). Note that ``unknown`` actions cannot transition directly to
        ``confirmed``; callers recovering late or reconciled results must call
        ``mark_reconciled``.
        """
        checked_attempt = _validate_attempt(attempt)
        _validate_optional_text("result_ref", result_ref)
        _validate_optional_text("summary", summary)

        with self._immediate_tx() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM actions "
                "WHERE actor = ? AND target = ? AND action_id = ?",
                (actor, target, action_id),
            ).fetchone()
            if row is None:
                raise ActionNotFoundError(actor=actor, target=target, action_id=action_id)

            existing = _row_to_record(row)
            if checked_attempt is not None and existing.attempt != checked_attempt:
                raise StaleAttemptError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_attempt=checked_attempt,
                    actual_attempt=existing.attempt,
                )

            if existing.status == ActionStatus.CONFIRMED:
                if existing.result_ref == result_ref:
                    return existing
                raise InvalidStateTransitionError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    current_status=existing.status,
                    target_status=ActionStatus.CONFIRMED,
                    detail=(
                        f"already confirmed with different result_ref: "
                        f"{existing.result_ref!r} != {result_ref!r}"
                    ),
                )

            if existing.status != ActionStatus.PENDING:
                raise InvalidStateTransitionError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    current_status=existing.status,
                    target_status=ActionStatus.CONFIRMED,
                )

            now = _utc_now()
            next_summary = summary if summary is not None else existing.summary
            cursor = conn.execute(
                "UPDATE actions "
                "SET status = ?, result_ref = ?, summary = ?, error = NULL, updated_at = ? "
                "WHERE actor = ? AND target = ? AND action_id = ? AND attempt = ?",
                (
                    ActionStatus.CONFIRMED.value,
                    result_ref,
                    next_summary,
                    now,
                    actor,
                    target,
                    action_id,
                    existing.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"Failed atomic confirm transition for ({actor!r}, {target!r}, {action_id!r})"
                )
            return ActionRecord(
                action_id=existing.action_id,
                session_id=existing.session_id,
                actor=existing.actor,
                surface=existing.surface,
                tool_name=existing.tool_name,
                target=existing.target,
                payload_hash=existing.payload_hash,
                marker=existing.marker,
                status=ActionStatus.CONFIRMED,
                attempt=existing.attempt,
                result_ref=result_ref,
                summary=next_summary,
                error=None,
                created_at=existing.created_at,
                updated_at=now,
            )

    def mark_failed(
        self,
        actor: str,
        target: str,
        action_id: str,
        *,
        attempt: int | None = None,
        error: str | None = None,
        summary: str | None = None,
    ) -> ActionRecord:
        """Transition a ``pending`` or ``unknown`` action to ``failed``."""
        checked_attempt = _validate_attempt(attempt)
        _validate_optional_text("error", error)
        _validate_optional_text("summary", summary)

        with self._immediate_tx() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM actions "
                "WHERE actor = ? AND target = ? AND action_id = ?",
                (actor, target, action_id),
            ).fetchone()
            if row is None:
                raise ActionNotFoundError(actor=actor, target=target, action_id=action_id)

            existing = _row_to_record(row)
            if checked_attempt is not None and existing.attempt != checked_attempt:
                raise StaleAttemptError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_attempt=checked_attempt,
                    actual_attempt=existing.attempt,
                )

            if existing.status not in (ActionStatus.PENDING, ActionStatus.UNKNOWN):
                raise InvalidStateTransitionError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    current_status=existing.status,
                    target_status=ActionStatus.FAILED,
                )

            now = _utc_now()
            next_summary = summary if summary is not None else existing.summary
            cursor = conn.execute(
                "UPDATE actions "
                "SET status = ?, error = ?, summary = ?, updated_at = ? "
                "WHERE actor = ? AND target = ? AND action_id = ? AND attempt = ?",
                (
                    ActionStatus.FAILED.value,
                    error,
                    next_summary,
                    now,
                    actor,
                    target,
                    action_id,
                    existing.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"Failed atomic fail transition for ({actor!r}, {target!r}, {action_id!r})"
                )
            return ActionRecord(
                action_id=existing.action_id,
                session_id=existing.session_id,
                actor=existing.actor,
                surface=existing.surface,
                tool_name=existing.tool_name,
                target=existing.target,
                payload_hash=existing.payload_hash,
                marker=existing.marker,
                status=ActionStatus.FAILED,
                attempt=existing.attempt,
                result_ref=existing.result_ref,
                summary=next_summary,
                error=error,
                created_at=existing.created_at,
                updated_at=now,
            )

    def mark_reconciled(
        self,
        actor: str,
        target: str,
        action_id: str,
        *,
        attempt: int | None = None,
        result_ref: str | None = None,
        summary: str | None = None,
    ) -> ActionRecord:
        """Transition a ``pending`` or ``unknown`` action to ``reconciled``."""
        checked_attempt = _validate_attempt(attempt)
        _validate_optional_text("result_ref", result_ref)
        _validate_optional_text("summary", summary)

        with self._immediate_tx() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM actions "
                "WHERE actor = ? AND target = ? AND action_id = ?",
                (actor, target, action_id),
            ).fetchone()
            if row is None:
                raise ActionNotFoundError(actor=actor, target=target, action_id=action_id)

            existing = _row_to_record(row)
            if checked_attempt is not None and existing.attempt != checked_attempt:
                raise StaleAttemptError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_attempt=checked_attempt,
                    actual_attempt=existing.attempt,
                )

            if existing.status not in (ActionStatus.PENDING, ActionStatus.UNKNOWN):
                raise InvalidStateTransitionError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    current_status=existing.status,
                    target_status=ActionStatus.RECONCILED,
                )

            now = _utc_now()
            next_summary = summary if summary is not None else existing.summary
            cursor = conn.execute(
                "UPDATE actions "
                "SET status = ?, result_ref = ?, summary = ?, error = NULL, updated_at = ? "
                "WHERE actor = ? AND target = ? AND action_id = ? AND attempt = ?",
                (
                    ActionStatus.RECONCILED.value,
                    result_ref,
                    next_summary,
                    now,
                    actor,
                    target,
                    action_id,
                    existing.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"Failed atomic reconcile transition for ({actor!r}, {target!r}, {action_id!r})"
                )
            return ActionRecord(
                action_id=existing.action_id,
                session_id=existing.session_id,
                actor=existing.actor,
                surface=existing.surface,
                tool_name=existing.tool_name,
                target=existing.target,
                payload_hash=existing.payload_hash,
                marker=existing.marker,
                status=ActionStatus.RECONCILED,
                attempt=existing.attempt,
                result_ref=result_ref,
                summary=next_summary,
                error=None,
                created_at=existing.created_at,
                updated_at=now,
            )

    def mark_unknown(
        self,
        actor: str,
        target: str,
        action_id: str,
        *,
        attempt: int | None = None,
        error: str | None = None,
        summary: str | None = None,
    ) -> ActionRecord:
        """Transition a ``pending`` action to ``unknown`` (idempotent if already ``unknown``)."""
        checked_attempt = _validate_attempt(attempt)
        _validate_optional_text("error", error)
        _validate_optional_text("summary", summary)

        with self._immediate_tx() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM actions "
                "WHERE actor = ? AND target = ? AND action_id = ?",
                (actor, target, action_id),
            ).fetchone()
            if row is None:
                raise ActionNotFoundError(actor=actor, target=target, action_id=action_id)

            existing = _row_to_record(row)
            if checked_attempt is not None and existing.attempt != checked_attempt:
                raise StaleAttemptError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    expected_attempt=checked_attempt,
                    actual_attempt=existing.attempt,
                )

            if existing.status == ActionStatus.UNKNOWN:
                return existing

            if existing.status != ActionStatus.PENDING:
                raise InvalidStateTransitionError(
                    actor=actor,
                    target=target,
                    action_id=action_id,
                    current_status=existing.status,
                    target_status=ActionStatus.UNKNOWN,
                )

            now = _utc_now()
            next_summary = summary if summary is not None else existing.summary
            cursor = conn.execute(
                "UPDATE actions "
                "SET status = ?, error = ?, summary = ?, updated_at = ? "
                "WHERE actor = ? AND target = ? AND action_id = ? AND attempt = ?",
                (
                    ActionStatus.UNKNOWN.value,
                    error,
                    next_summary,
                    now,
                    actor,
                    target,
                    action_id,
                    existing.attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(
                    f"Failed atomic unknown transition for ({actor!r}, {target!r}, {action_id!r})"
                )
            return ActionRecord(
                action_id=existing.action_id,
                session_id=existing.session_id,
                actor=existing.actor,
                surface=existing.surface,
                tool_name=existing.tool_name,
                target=existing.target,
                payload_hash=existing.payload_hash,
                marker=existing.marker,
                status=ActionStatus.UNKNOWN,
                attempt=existing.attempt,
                result_ref=existing.result_ref,
                summary=next_summary,
                error=error,
                created_at=existing.created_at,
                updated_at=now,
            )

    def get_action(self, actor: str, target: str, action_id: str) -> ActionRecord | None:
        """Fetch a single action by its unique composite key."""
        with self._lock:
            conn = self._require_conn()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM actions "
                    "WHERE actor = ? AND target = ? AND action_id = ?",
                    (actor, target, action_id),
                ).fetchone()
            except sqlite3.Error as exc:
                raise JournalError(f"Failed to query action: {exc}") from exc
            return _row_to_record(row) if row is not None else None

    def get_action_by_marker(self, marker: str) -> ActionRecord | None:
        """Fetch the most recent action matching a reconciliation marker."""
        with self._lock:
            conn = self._require_conn()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM actions WHERE marker = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (marker,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise JournalError(f"Failed to query action by marker: {exc}") from exc
            return _row_to_record(row) if row is not None else None

    def list_actions(
        self,
        *,
        session_id: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        status: ActionStatus | str | Iterable[ActionStatus | str] | None = None,
        surface: Surface | str | None = None,
        limit: int | None = None,
        descending: bool = True,
    ) -> list[ActionRecord]:
        """List journaled actions in deterministic order with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        if surface is not None:
            surface_enum = _coerce_surface(surface)
            clauses.append("surface = ?")
            params.append(surface_enum.value)
        if status is not None:
            if isinstance(status, (ActionStatus, str)):
                statuses = [_coerce_status(status).value]
            else:
                statuses = [_coerce_status(s).value for s in status]
            if not statuses:
                return []
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("limit must be a non-negative integer")
            if limit == 0:
                return []

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_dir = "DESC" if descending else "ASC"
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)

        query = (
            f"SELECT {_SELECT_COLUMNS} FROM actions {where_sql} "
            f"ORDER BY created_at {order_dir}, rowid {order_dir}{limit_sql}"
        )

        with self._lock:
            conn = self._require_conn()
            try:
                rows = conn.execute(query, params).fetchall()
            except sqlite3.Error as exc:
                raise JournalError(f"Failed to list actions: {exc}") from exc
            return [_row_to_record(row) for row in rows]

    def count_by_status(
        self,
        *,
        session_id: str | None = None,
        surface: Surface | str | None = None,
    ) -> dict[ActionStatus, int]:
        """Return counts of actions grouped by status."""
        clauses: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if surface is not None:
            clauses.append("surface = ?")
            params.append(_coerce_surface(surface).value)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        counts: dict[ActionStatus, int] = {status: 0 for status in ActionStatus}

        with self._lock:
            conn = self._require_conn()
            try:
                rows = conn.execute(
                    f"SELECT status, COUNT(*) AS cnt FROM actions {where_sql} GROUP BY status",
                    params,
                ).fetchall()
            except sqlite3.Error as exc:
                raise JournalError(f"Failed to count actions: {exc}") from exc
            for row in rows:
                counts[ActionStatus(row["status"])] = int(row["cnt"])
        return counts
