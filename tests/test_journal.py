"""Unit tests for the durable SQLite WAL journal."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sidefx.config import SidefxConfig, load_config
from sidefx.journal import (
    DEFAULT_BUSY_TIMEOUT_MS,
    ENV_DB_PATH,
    SCHEMA_VERSION,
    ActionNotFoundError,
    ActionStatus,
    BeginDecision,
    InvalidStateTransitionError,
    Journal,
    JournalError,
    PayloadMismatchError,
    StaleAttemptError,
    Surface,
    compute_payload_hash,
    derive_marker,
    resolve_db_path,
)


class TestCanonicalHelpers:
    def test_compute_payload_hash_is_deterministic_across_key_order(self) -> None:
        h1 = compute_payload_hash({"b": 2, "a": [1, {"y": True, "x": "hello"}]})
        h2 = compute_payload_hash({"a": [1, {"x": "hello", "y": True}], "b": 2})
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_payload_hash_distinguishes_types_and_unicode_forms(self) -> None:
        assert compute_payload_hash({"flag": True}) != compute_payload_hash({"flag": 1})
        assert compute_payload_hash({"flag": False}) != compute_payload_hash({"flag": 0})
        assert compute_payload_hash({"n": 1}) != compute_payload_hash({"n": 1.0})
        assert compute_payload_hash({"s": "\u00e9"}) != compute_payload_hash({"s": "e\u0301"})

    def test_compute_payload_hash_handles_lone_surrogates(self) -> None:
        lone = json.loads(r'{"t":"\ud83d"}')
        digest = compute_payload_hash(lone)
        assert len(digest) == 64
        assert digest == compute_payload_hash(lone)

    def test_compute_payload_hash_rejects_non_string_keys_nan_inf_and_objects(self) -> None:
        with pytest.raises(TypeError, match="keys must be strings"):
            compute_payload_hash({1: "a"})
        with pytest.raises(TypeError, match="keys must be strings"):
            compute_payload_hash({"nested": [{True: "x"}]})
        with pytest.raises(ValueError):
            compute_payload_hash({"x": math.nan})
        with pytest.raises(ValueError):
            compute_payload_hash({"x": math.inf})
        with pytest.raises(TypeError):
            compute_payload_hash({"x": object()})

    def test_derive_marker_is_deterministic_delimiter_and_surrogate_safe(self) -> None:
        m1 = derive_marker("agent", "org/repo", "act-1")
        m2 = derive_marker("agent", "org/repo", "act-1")
        assert m1 == m2
        assert len(m1) == 24
        assert all(c in "0123456789abcdef" for c in m1)

        # Delimiter ambiguity check
        assert derive_marker("a:b", "c", "d") != derive_marker("a", "b:c", "d")
        assert derive_marker("a", "b", "c") != derive_marker("a", "b", "d")

        # Lone surrogate does not crash
        m_surr = derive_marker("agent", "org/\ud83d", "act-1")
        assert len(m_surr) == 24

    def test_derive_marker_rejects_empty_and_nul_fields(self) -> None:
        with pytest.raises(ValueError):
            derive_marker("", "target", "id")
        with pytest.raises(ValueError):
            derive_marker("actor", "   ", "id")
        with pytest.raises(ValueError):
            derive_marker("actor", "target", "\x00id")

    def test_resolve_db_path_precedence_and_config_anchoring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_DB_PATH, raising=False)
        assert resolve_db_path(cwd=tmp_path) == tmp_path / ".sidefx" / "journal.db"

        # Discovered config with no explicit journal_path anchors to config directory
        cfg_file = tmp_path / ".sidefx.json"
        cfg_file.write_text('{"version": 1}\n', encoding="utf-8")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        cfg = load_config(cwd=sub)
        assert resolve_db_path(config=cfg, cwd=sub) == tmp_path / ".sidefx" / "journal.db"

        # Explicit config.journal_path resolves relative to config directory
        cfg_custom = SidefxConfig(journal_path="state/custom.db", source_path=cfg_file)
        assert resolve_db_path(config=cfg_custom, cwd=sub) == tmp_path / "state" / "custom.db"

        # SIDEFX_DB overrides config
        env_db = tmp_path / "env.db"
        monkeypatch.setenv(ENV_DB_PATH, str(env_db))
        assert resolve_db_path(config=cfg_custom, cwd=sub) == env_db

        # Explicit db_path overrides SIDEFX_DB
        explicit_db = tmp_path / "explicit.db"
        assert resolve_db_path(explicit_db, config=cfg_custom, cwd=sub) == explicit_db

        with pytest.raises(ValueError):
            resolve_db_path("   ")


class TestSchemaAndDurability:
    def test_wal_durability_pragmas_and_user_version(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "journal.db"
        with Journal(db_path) as journal:
            pragmas = journal.pragmas()
            assert pragmas["journal_mode"] == "wal"
            assert pragmas["synchronous"] == 2  # FULL
            assert pragmas["foreign_keys"] == 1
            assert pragmas["busy_timeout"] == DEFAULT_BUSY_TIMEOUT_MS
            assert pragmas["user_version"] == SCHEMA_VERSION
            if sys.platform == "darwin":
                assert pragmas["fullfsync"] == 1
            assert journal.db_path == db_path

    def test_in_memory_journal_works_and_rejects_readonly(self) -> None:
        with Journal(":memory:") as journal:
            assert journal.db_path is None
            res = journal.begin_action(
                action_id="mem-1",
                session_id="s1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"ok": True},
            )
            assert res.decision == BeginDecision.PROCEED
            assert res.record.attempt == 1
            assert journal.get_action("agent", "org/repo", "mem-1") == res.record

        with pytest.raises(ValueError, match="read_only"):
            Journal(":memory:", read_only=True)

    def test_future_schema_version_is_rejected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()

        with pytest.raises(JournalError, match="Unsupported journal schema version"):
            Journal(db_path)
        with pytest.raises(JournalError, match="Unsupported journal schema version"):
            Journal.open_readonly(db_path)

    def test_schema_columns_and_constraints(self, tmp_path: Path) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path):
            pass

        conn = sqlite3.connect(db_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(actions)").fetchall()]
            assert cols == [
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
            ]

            # Invalid surface rejected by CHECK constraint
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s1','actor','grpc','tool','t','h','m','pending',1,"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )

            # Invalid status rejected by CHECK constraint
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s1','actor','mcp','tool','t','h','m','bogus',1,"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )

            # Invalid attempt rejected by CHECK constraint
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s1','actor','mcp','tool','t','h','m','pending',0,"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )

            # Duplicate (actor, target, action_id) rejected by UNIQUE constraint
            conn.execute(
                "INSERT INTO actions VALUES ("
                "'a1','s1','actor','mcp','tool','t','h','m','pending',1,"
                "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s2','actor','cli','tool','t','h','m','pending',1,"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )
        finally:
            conn.close()

    def test_readonly_open_does_not_mutate_non_sidefx_db_and_blocks_writes(
        self, tmp_path: Path
    ) -> None:
        other_db = tmp_path / "other_app.db"
        conn = sqlite3.connect(other_db, isolation_level=None)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users VALUES (1, 'alice')")
        orig_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        conn.close()

        with pytest.raises(JournalError, match="missing 'actions' table"):
            Journal.open_readonly(other_db)

        # Verify database was NOT altered (still same journal mode, no actions table)
        conn = sqlite3.connect(other_db, isolation_level=None)
        try:
            assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == orig_mode
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert tables == {"users"}
        finally:
            conn.close()

        # Valid journal opened read-only rejects mutations
        valid_db = tmp_path / "valid.db"
        with Journal(valid_db) as jw:
            jw.begin_action(
                action_id="ro-1",
                session_id="s1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"x": 1},
            )
        with Journal.open_readonly(valid_db) as jr:
            assert jr.read_only is True
            assert len(jr.list_actions()) == 1
            with pytest.raises(JournalError, match="read-only"):
                jr.mark_confirmed("agent", "t", "ro-1")

    def test_directory_db_path_and_closed_journal_errors(self, tmp_path: Path) -> None:
        with pytest.raises(JournalError, match="directory"):
            Journal(tmp_path)

        journal = Journal(tmp_path / "journal.db")
        journal.close()
        with pytest.raises(JournalError, match="closed"):
            journal.list_actions()

    def test_rollback_on_exception_keeps_connection_usable(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "rollback.db") as journal:
            digest = compute_payload_hash({"x": 1})
            with pytest.raises(RuntimeError, match="injected"), journal._immediate_tx() as conn:
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'rb-1','s1','agent','mcp','create_x','t',?,'m','pending',1,"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                    (digest,),
                )
                raise RuntimeError("injected")

            # Row must not have committed and connection must remain usable
            assert journal.get_action("agent", "t", "rb-1") is None
            res = journal.begin_action(
                action_id="rb-1",
                session_id="s1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"x": 1},
            )
            assert res.decision == BeginDecision.PROCEED

    def test_busy_timeout_expiry_raises_journal_error_promptly(self, tmp_path: Path) -> None:
        db_path = tmp_path / "busy.db"
        with Journal(db_path):
            pass

        holder = sqlite3.connect(db_path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with (
                Journal(db_path, busy_timeout_ms=100) as contender,
                pytest.raises(JournalError, match="locked"),
            ):
                contender.begin_action(
                    action_id="locked-1",
                    session_id="s1",
                    actor="agent",
                    surface=Surface.MCP,
                    tool_name="create_x",
                    target="t",
                    payload={"x": 1},
                )
        finally:
            holder.execute("ROLLBACK")
            holder.close()


class TestStateTransitionsAndFencing:
    def test_begin_confirm_replay_flow(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            payload = {"title": "Fix bug", "body": "Details"}
            res = journal.begin_action(
                action_id="act-1",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload=payload,
                summary="Create issue Fix bug",
            )
            assert res.decision == BeginDecision.PROCEED
            assert res.retried is False
            assert res.record.status == ActionStatus.PENDING
            assert res.record.attempt == 1
            assert res.record.payload_hash == compute_payload_hash(payload)
            assert res.record.marker == derive_marker("agent-1", "org/repo", "act-1")

            # While pending, a duplicate begin_action returns OUTCOME_UNKNOWN
            dup = journal.begin_action(
                action_id="act-1",
                session_id="sess-1",
                actor="agent-1",
                surface="mcp",
                tool_name="create_issue",
                target="org/repo",
                payload={"body": "Details", "title": "Fix bug"},
            )
            assert dup.decision == BeginDecision.OUTCOME_UNKNOWN
            assert dup.record.status == ActionStatus.PENDING

            # Confirm action
            confirmed = journal.mark_confirmed(
                "agent-1",
                "org/repo",
                "act-1",
                attempt=res.record.attempt,
                result_ref="https://github.com/org/repo/issues/42",
                summary="Created issue #42",
            )
            assert confirmed.status == ActionStatus.CONFIRMED
            assert confirmed.result_ref == "https://github.com/org/repo/issues/42"
            assert confirmed.summary == "Created issue #42"

            # Confirm again with same ref is idempotent
            confirmed_again = journal.mark_confirmed(
                "agent-1",
                "org/repo",
                "act-1",
                attempt=res.record.attempt,
                result_ref="https://github.com/org/repo/issues/42",
            )
            assert confirmed_again == confirmed

            # Confirm again with different ref raises InvalidStateTransitionError
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_confirmed(
                    "agent-1",
                    "org/repo",
                    "act-1",
                    result_ref="https://github.com/org/repo/issues/99",
                )

            # Subsequent begin_action returns REPLAY with cached result_ref
            replay = journal.begin_action(
                action_id="act-1",
                session_id="sess-2",
                actor="agent-1",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload=payload,
            )
            assert replay.decision == BeginDecision.REPLAY
            assert replay.record.result_ref == "https://github.com/org/repo/issues/42"
            assert replay.record.summary == "Created issue #42"

    def test_stale_attempt_finalizers_are_fenced(self, tmp_path: Path) -> None:
        """Reproduces the B1 race: late attempt-1 finalizers cannot corrupt attempt 2."""
        with Journal(tmp_path / "fenced.db") as journal:
            # Worker A claims attempt 1, stalls, gets reconciled as not-found -> failed
            attempt1 = journal.begin_action(
                action_id="fenced-1",
                session_id="sess-A",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Fenced"},
            )
            assert attempt1.decision == BeginDecision.PROCEED
            assert attempt1.record.attempt == 1

            journal.mark_unknown(
                "agent", "org/repo", "fenced-1", attempt=1, error="upstream timeout"
            )
            journal.mark_failed(
                "agent", "org/repo", "fenced-1", attempt=1, error="reconciler: not found"
            )

            # Worker B retries cleanly -> attempt 2 is now in flight
            attempt2 = journal.begin_action(
                action_id="fenced-1",
                session_id="sess-B",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Fenced"},
            )
            assert attempt2.decision == BeginDecision.PROCEED
            assert attempt2.retried is True
            assert attempt2.record.attempt == 2

            # Late finalizers from Worker A (attempt=1) MUST be rejected with StaleAttemptError
            with pytest.raises(StaleAttemptError) as exc_fail:
                journal.mark_failed(
                    "agent", "org/repo", "fenced-1", attempt=1, error="late error from A"
                )
            assert exc_fail.value.expected_attempt == 1
            assert exc_fail.value.actual_attempt == 2

            with pytest.raises(StaleAttemptError):
                journal.mark_confirmed(
                    "agent", "org/repo", "fenced-1", attempt=1, result_ref="A-late"
                )
            with pytest.raises(StaleAttemptError):
                journal.mark_reconciled(
                    "agent", "org/repo", "fenced-1", attempt=1, result_ref="A-late"
                )
            with pytest.raises(StaleAttemptError):
                journal.mark_unknown(
                    "agent", "org/repo", "fenced-1", attempt=1, error="A-late-timeout"
                )

            # Worker C cannot double-dispatch because attempt 2 is still safely pending
            worker_c = journal.begin_action(
                action_id="fenced-1",
                session_id="sess-C",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Fenced"},
            )
            assert worker_c.decision == BeginDecision.OUTCOME_UNKNOWN
            assert worker_c.record.attempt == 2

            # Worker B's real confirmation succeeds cleanly
            confirmed = journal.mark_confirmed(
                "agent", "org/repo", "fenced-1", attempt=2, result_ref="B-real-ref"
            )
            assert confirmed.status == ActionStatus.CONFIRMED
            assert confirmed.attempt == 2
            assert confirmed.result_ref == "B-real-ref"

    def test_unknown_and_reconciled_flow(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            journal.begin_action(
                action_id="act-2",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"title": "Timeout test"},
            )

            unk = journal.mark_unknown(
                "agent-1", "org/repo", "act-2", error="upstream timed out after 30s"
            )
            assert unk.status == ActionStatus.UNKNOWN
            assert unk.error == "upstream timed out after 30s"

            # mark_unknown is idempotent on already unknown action
            assert journal.mark_unknown("agent-1", "org/repo", "act-2") == unk

            # begin_action blocks blind retry while unknown
            blocked = journal.begin_action(
                action_id="act-2",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"title": "Timeout test"},
            )
            assert blocked.decision == BeginDecision.OUTCOME_UNKNOWN
            assert blocked.record.status == ActionStatus.UNKNOWN

            # Reconcile recovers external result_ref
            reconciled = journal.mark_reconciled(
                "agent-1",
                "org/repo",
                "act-2",
                result_ref="https://github.com/org/repo/issues/77",
                summary="Recovered issue #77",
            )
            assert reconciled.status == ActionStatus.RECONCILED
            assert reconciled.result_ref == "https://github.com/org/repo/issues/77"
            assert reconciled.error is None

            # Subsequent begin_action replays reconciled result
            replay = journal.begin_action(
                action_id="act-2",
                session_id="sess-2",
                actor="agent-1",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"title": "Timeout test"},
            )
            assert replay.decision == BeginDecision.REPLAY
            assert replay.record.status == ActionStatus.RECONCILED
            assert replay.record.result_ref == "https://github.com/org/repo/issues/77"

    def test_failed_action_allows_safe_retry(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            first = journal.begin_action(
                action_id="act-retry",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.HTTP,
                tool_name="post_webhook",
                target="https://hooks.example.com/a",
                payload={"event": "ping"},
                summary="Initial summary",
            )
            assert first.decision == BeginDecision.PROCEED
            assert first.retried is False
            assert first.record.attempt == 1

            failed = journal.mark_failed(
                "agent-1",
                "https://hooks.example.com/a",
                "act-retry",
                error="HTTP 422 validation error",
            )
            assert failed.status == ActionStatus.FAILED
            assert failed.error == "HTTP 422 validation error"

            retried = journal.begin_action(
                action_id="act-retry",
                session_id="sess-2",
                actor="agent-1",
                surface=Surface.HTTP,
                tool_name="post_webhook",
                target="https://hooks.example.com/a",
                payload={"event": "ping"},
            )
            assert retried.decision == BeginDecision.PROCEED
            assert retried.retried is True
            assert retried.record.status == ActionStatus.PENDING
            assert retried.record.attempt == 2
            assert retried.record.error is None
            assert retried.record.session_id == "sess-2"
            assert retried.record.summary == "Initial summary"

            confirmed = journal.mark_confirmed(
                "agent-1",
                "https://hooks.example.com/a",
                "act-retry",
                attempt=2,
                result_ref="delivery-123",
            )
            assert confirmed.status == ActionStatus.CONFIRMED

    def test_payload_tool_surface_and_marker_mismatch_raise_error(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            journal.begin_action(
                action_id="act-m",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Original"},
                marker="custom-marker-1",
            )

            with pytest.raises(PayloadMismatchError) as exc_info:
                journal.begin_action(
                    action_id="act-m",
                    session_id="sess-1",
                    actor="agent-1",
                    surface=Surface.MCP,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Mutated"},
                )
            assert exc_info.value.action_id == "act-m"

            with pytest.raises(PayloadMismatchError, match="tool_name"):
                journal.begin_action(
                    action_id="act-m",
                    session_id="sess-1",
                    actor="agent-1",
                    surface=Surface.MCP,
                    tool_name="delete_issue",
                    target="org/repo",
                    payload={"title": "Original"},
                )

            with pytest.raises(PayloadMismatchError, match="surface"):
                journal.begin_action(
                    action_id="act-m",
                    session_id="sess-1",
                    actor="agent-1",
                    surface=Surface.CLI,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Original"},
                )

            with pytest.raises(PayloadMismatchError, match="marker"):
                journal.begin_action(
                    action_id="act-m",
                    session_id="sess-1",
                    actor="agent-1",
                    surface=Surface.MCP,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Original"},
                    marker="different-marker",
                )

    def test_input_validation_and_explicit_payload_hash(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            # Must pass either payload or payload_hash
            with pytest.raises(ValueError, match="Either payload or payload_hash"):
                journal.begin_action(
                    action_id="no-payload",
                    session_id="s1",
                    actor="a1",
                    surface=Surface.MCP,
                    tool_name="create_item",
                    target="t1",
                )

            # Invalid payload_hash format rejected
            with pytest.raises(ValueError, match="64-character lowercase hex"):
                journal.begin_action(
                    action_id="bad-hash",
                    session_id="s1",
                    actor="a1",
                    surface=Surface.MCP,
                    tool_name="create_item",
                    target="t1",
                    payload_hash="not-a-hash",
                )

            # NUL byte rejected before SQLite
            with pytest.raises(ValueError, match="NUL"):
                journal.begin_action(
                    action_id="\x00abc",
                    session_id="s1",
                    actor="a1",
                    surface=Surface.MCP,
                    tool_name="create_item",
                    target="t1",
                    payload={"x": 1},
                )

            # Explicit matching payload_hash accepted
            payload = {"x": 1}
            digest = compute_payload_hash(payload)
            res = journal.begin_action(
                action_id="act-hash",
                session_id="s1",
                actor="a1",
                surface=Surface.MCP,
                tool_name="create_item",
                target="t1",
                payload=payload,
                payload_hash=digest,
            )
            assert res.record.payload_hash == digest

            # Non-string result_ref rejected cleanly
            with pytest.raises(ValueError, match="result_ref"):
                journal.mark_confirmed("a1", "t1", "act-hash", result_ref=123)  # type: ignore[arg-type]

            # Invalid attempt parameter rejected
            with pytest.raises(ValueError, match="attempt"):
                journal.mark_confirmed("a1", "t1", "act-hash", attempt=True)  # type: ignore[arg-type]

    def test_invalid_state_transitions_matrix(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            for fn in (
                lambda: journal.mark_confirmed("a", "t", "missing"),
                lambda: journal.mark_failed("a", "t", "missing"),
                lambda: journal.mark_reconciled("a", "t", "missing"),
                lambda: journal.mark_unknown("a", "t", "missing"),
            ):
                with pytest.raises(ActionNotFoundError):
                    fn()

            journal.begin_action(
                action_id="c1",
                session_id="s1",
                actor="a",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"k": 1},
            )
            journal.mark_confirmed("a", "t", "c1", result_ref="ref-1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_failed("a", "t", "c1", error="err")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_reconciled("a", "t", "c1", result_ref="ref-1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_unknown("a", "t", "c1", error="err")

            journal.begin_action(
                action_id="r1",
                session_id="s1",
                actor="a",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"k": 2},
            )
            journal.mark_reconciled("a", "t", "r1", result_ref="ref-r1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_confirmed("a", "t", "r1", result_ref="ref-r1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_failed("a", "t", "r1", error="err")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_reconciled("a", "t", "r1", result_ref="ref-r1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_unknown("a", "t", "r1", error="err")

            journal.begin_action(
                action_id="f1",
                session_id="s1",
                actor="a",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"k": 3},
            )
            journal.mark_failed("a", "t", "f1", error="err")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_confirmed("a", "t", "f1", result_ref="ref-f1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_reconciled("a", "t", "f1", result_ref="ref-f1")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_unknown("a", "t", "f1", error="err")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_failed("a", "t", "f1", error="err2")

            journal.begin_action(
                action_id="u1",
                session_id="s1",
                actor="a",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"k": 4},
            )
            journal.mark_unknown("a", "t", "u1", error="timeout")
            with pytest.raises(InvalidStateTransitionError):
                journal.mark_confirmed("a", "t", "u1", result_ref="ref-u1")
            failed_u1 = journal.mark_failed("a", "t", "u1", error="reconciler confirmed absent")
            assert failed_u1.status == ActionStatus.FAILED


class TestRealCrashWindowsAndQueries:
    def test_abrupt_process_crash_before_commit_leaves_zero_rows_and_releases_lock(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "crash_before_commit.db"
        with Journal(db_path):
            pass

        script = (
            "import os, sqlite3, sys\n"
            "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
            "conn.execute('BEGIN IMMEDIATE')\n"
            "conn.execute(\"INSERT INTO actions VALUES ('c0','s1','agent','cli','git_push',"
            "'origin/feat','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
            "'m0','pending',1,NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')\")\n"
            "os._exit(99)\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, str(db_path)], check=False)
        assert proc.returncode == 99

        with Journal(db_path) as recovered:
            assert recovered.list_actions() == []
            res = recovered.begin_action(
                action_id="c0",
                session_id="s2",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "abc123"},
            )
            assert res.decision == BeginDecision.PROCEED

    def test_abrupt_process_crash_after_begin_action_recovers_from_hot_wal(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "crash_after_commit.db"
        script = (
            "import os, sys\n"
            "from sidefx.journal import Journal, Surface\n"
            "j = Journal(sys.argv[1])\n"
            "j.begin_action(action_id='crash-wal', session_id='s1', actor='agent', "
            "surface=Surface.CLI, tool_name='git_push', target='origin/feat', "
            "payload={'sha': 'deadbeef'})\n"
            "os._exit(42)\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, str(db_path)], check=False)
        assert proc.returncode == 42
        # Confirm WAL sidecar existed or database persisted without clean close()
        assert os.path.exists(db_path)

        with Journal(db_path) as recovered:
            fetched = recovered.get_action("agent", "origin/feat", "crash-wal")
            assert fetched is not None
            assert fetched.status == ActionStatus.PENDING
            assert recovered.get_action_by_marker(fetched.marker) == fetched

            retry = recovered.begin_action(
                action_id="crash-wal",
                session_id="s2",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "deadbeef"},
            )
            assert retry.decision == BeginDecision.OUTCOME_UNKNOWN

            rec = recovered.mark_reconciled(
                "agent",
                "origin/feat",
                "crash-wal",
                attempt=fetched.attempt,
                result_ref="refs/heads/feat@deadbeef",
            )
            assert rec.status == ActionStatus.RECONCILED

            replay = recovered.begin_action(
                action_id="crash-wal",
                session_id="s2",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "deadbeef"},
            )
            assert replay.decision == BeginDecision.REPLAY
            assert replay.record.result_ref == "refs/heads/feat@deadbeef"

    def test_list_actions_filters_and_counts(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "queries.db") as journal:
            for idx, (sess, surface, tool) in enumerate(
                [
                    ("s1", Surface.MCP, "create_issue"),
                    ("s1", Surface.CLI, "git_push"),
                    ("s2", Surface.HTTP, "post_hook"),
                    ("s2", Surface.MCP, "send_email"),
                ],
                start=1,
            ):
                journal.begin_action(
                    action_id=f"a-{idx}",
                    session_id=sess,
                    actor="agent",
                    surface=surface,
                    tool_name=tool,
                    target=f"target-{idx}",
                    payload={"i": idx},
                )

            journal.mark_confirmed("agent", "target-1", "a-1", result_ref="#1")
            journal.mark_failed("agent", "target-2", "a-2", error="rejected")
            journal.mark_unknown("agent", "target-3", "a-3", error="timeout")

            counts = journal.count_by_status()
            assert counts[ActionStatus.CONFIRMED] == 1
            assert counts[ActionStatus.FAILED] == 1
            assert counts[ActionStatus.UNKNOWN] == 1
            assert counts[ActionStatus.PENDING] == 1
            assert counts[ActionStatus.RECONCILED] == 0

            assert [
                a.action_id for a in journal.list_actions(session_id="s1", descending=False)
            ] == [
                "a-1",
                "a-2",
            ]
            assert [
                a.action_id for a in journal.list_actions(surface=Surface.MCP, descending=False)
            ] == ["a-1", "a-4"]
            assert [
                a.action_id
                for a in journal.list_actions(
                    status=[ActionStatus.PENDING, ActionStatus.UNKNOWN], descending=True
                )
            ] == ["a-4", "a-3"]
            assert len(journal.list_actions(limit=2)) == 2
            assert journal.list_actions(limit=0) == []

            with pytest.raises(ValueError):
                journal.list_actions(limit=-1)
