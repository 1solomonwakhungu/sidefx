"""Unit tests for the durable SQLite WAL journal."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from sidefx.journal import (
    DEFAULT_BUSY_TIMEOUT_MS,
    ENV_DB_PATH,
    ActionNotFoundError,
    ActionStatus,
    BeginDecision,
    InvalidStateTransitionError,
    Journal,
    JournalError,
    PayloadMismatchError,
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

    def test_compute_payload_hash_distinguishes_bool_and_int(self) -> None:
        assert compute_payload_hash({"flag": True}) != compute_payload_hash({"flag": 1})
        assert compute_payload_hash({"flag": False}) != compute_payload_hash({"flag": 0})

    def test_compute_payload_hash_rejects_nan_and_inf(self) -> None:
        with pytest.raises(ValueError):
            compute_payload_hash({"x": math.nan})
        with pytest.raises(ValueError):
            compute_payload_hash({"x": math.inf})
        with pytest.raises(TypeError):
            compute_payload_hash({"x": object()})

    def test_derive_marker_is_deterministic_and_delimiter_safe(self) -> None:
        m1 = derive_marker("agent", "org/repo", "act-1")
        m2 = derive_marker("agent", "org/repo", "act-1")
        assert m1 == m2
        assert len(m1) == 24
        assert all(c in "0123456789abcdef" for c in m1)

        # Delimiter ambiguity check
        assert derive_marker("a:b", "c", "d") != derive_marker("a", "b:c", "d")
        assert derive_marker("a", "b", "c") != derive_marker("a", "b", "d")

    def test_derive_marker_rejects_empty_fields(self) -> None:
        with pytest.raises(ValueError):
            derive_marker("", "target", "id")
        with pytest.raises(ValueError):
            derive_marker("actor", "   ", "id")
        with pytest.raises(ValueError):
            derive_marker("actor", "target", "")

    def test_resolve_db_path_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_DB_PATH, raising=False)
        assert resolve_db_path(cwd=tmp_path) == tmp_path / ".sidefx" / "journal.db"

        env_db = tmp_path / "env.db"
        monkeypatch.setenv(ENV_DB_PATH, str(env_db))
        assert resolve_db_path(cwd=tmp_path) == env_db

        explicit_db = tmp_path / "explicit.db"
        assert resolve_db_path(explicit_db, cwd=tmp_path) == explicit_db

        with pytest.raises(ValueError):
            resolve_db_path("   ")


class TestSchemaAndDurability:
    def test_wal_and_durability_pragmas(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "journal.db"
        with Journal(db_path) as journal:
            pragmas = journal.pragmas()
            assert pragmas["journal_mode"] == "wal"
            assert pragmas["synchronous"] == 2  # FULL
            assert pragmas["foreign_keys"] == 1
            assert pragmas["busy_timeout"] == DEFAULT_BUSY_TIMEOUT_MS
            assert journal.db_path == db_path

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
                    "'a1','s1','actor','grpc','tool','t','h','m','pending',"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )

            # Invalid status rejected by CHECK constraint
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s1','actor','mcp','tool','t','h','m','bogus',"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )

            # Duplicate (actor, target, action_id) rejected by UNIQUE constraint
            conn.execute(
                "INSERT INTO actions VALUES ("
                "'a1','s1','actor','mcp','tool','t','h','m','pending',"
                "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO actions VALUES ("
                    "'a1','s2','actor','cli','tool','t','h','m','pending',"
                    "NULL,NULL,NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
                )
        finally:
            conn.close()

    def test_directory_db_path_raises_journal_error(self, tmp_path: Path) -> None:
        with pytest.raises(JournalError, match="directory"):
            Journal(tmp_path)

    def test_closed_journal_rejects_operations(self, tmp_path: Path) -> None:
        journal = Journal(tmp_path / "journal.db")
        journal.close()
        with pytest.raises(JournalError, match="closed"):
            journal.list_actions()


class TestStateTransitions:
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
            assert retried.record.error is None
            assert retried.record.session_id == "sess-2"
            assert retried.record.summary == "Initial summary"

            # Now confirming succeeds
            confirmed = journal.mark_confirmed(
                "agent-1",
                "https://hooks.example.com/a",
                "act-retry",
                result_ref="delivery-123",
            )
            assert confirmed.status == ActionStatus.CONFIRMED

    def test_payload_tool_and_surface_mismatch_raise_error(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            journal.begin_action(
                action_id="act-m",
                session_id="sess-1",
                actor="agent-1",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Original"},
            )

            # Different payload
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

            # Different tool_name with identical payload
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

            # Different surface with identical payload
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

            # Even after failed or confirmed, payload mismatch is still rejected
            journal.mark_failed("agent-1", "org/repo", "act-m", error="boom")
            with pytest.raises(PayloadMismatchError):
                journal.begin_action(
                    action_id="act-m",
                    session_id="sess-1",
                    actor="agent-1",
                    surface=Surface.MCP,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Changed after fail"},
                )

    def test_explicit_payload_hash_validation(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
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

            with pytest.raises(ValueError, match="does not match"):
                journal.begin_action(
                    action_id="act-hash-bad",
                    session_id="s1",
                    actor="a1",
                    surface=Surface.MCP,
                    tool_name="create_item",
                    target="t1",
                    payload={"x": 2},
                    payload_hash=digest,
                )

    def test_invalid_state_transitions_matrix(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db") as journal:
            # Missing action errors
            for fn in (
                lambda: journal.mark_confirmed("a", "t", "missing"),
                lambda: journal.mark_failed("a", "t", "missing"),
                lambda: journal.mark_reconciled("a", "t", "missing"),
                lambda: journal.mark_unknown("a", "t", "missing"),
            ):
                with pytest.raises(ActionNotFoundError):
                    fn()

            # From confirmed: cannot fail, reconcile, or unknown
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

            # From reconciled: cannot confirm, fail, reconcile again, or unknown
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

            # From failed: cannot confirm, reconcile, unknown, or fail again without begin_action
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

            # From unknown: cannot confirm directly (must reconcile or mark_failed)
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
            # unknown -> failed is valid (e.g., reconciler proved not_found)
            failed_u1 = journal.mark_failed("a", "t", "u1", error="reconciler confirmed absent")
            assert failed_u1.status == ActionStatus.FAILED


class TestCrashPersistenceAndQueries:
    def test_crash_left_pending_persists_across_connections(self, tmp_path: Path) -> None:
        db_path = tmp_path / "crash.db"

        # Worker 1 logs intent then crashes before marking confirmed
        j1 = Journal(db_path)
        begin1 = j1.begin_action(
            action_id="crash-1",
            session_id="sess-crash",
            actor="agent",
            surface=Surface.CLI,
            tool_name="git_push",
            target="origin/feat",
            payload={"sha": "abc123"},
        )
        assert begin1.decision == BeginDecision.PROCEED
        # Simulate abrupt process exit without finishing the action
        j1.close()

        # Fresh connection after restart sees durable pending row and blocks blind retry
        with Journal(db_path) as j2:
            fetched = j2.get_action("agent", "origin/feat", "crash-1")
            assert fetched is not None
            assert fetched.status == ActionStatus.PENDING
            assert j2.get_action_by_marker(begin1.record.marker) == fetched

            retry = j2.begin_action(
                action_id="crash-1",
                session_id="sess-recovery",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "abc123"},
            )
            assert retry.decision == BeginDecision.OUTCOME_UNKNOWN

            # Reconciler finds the push actually landed
            rec = j2.mark_reconciled(
                "agent", "origin/feat", "crash-1", result_ref="refs/heads/feat@abc123"
            )
            assert rec.status == ActionStatus.RECONCILED

            # Subsequent attempt cleanly replays without re-pushing
            replay = j2.begin_action(
                action_id="crash-1",
                session_id="sess-recovery",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "abc123"},
            )
            assert replay.decision == BeginDecision.REPLAY
            assert replay.record.result_ref == "refs/heads/feat@abc123"

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

            # Filter by session
            s1_actions = journal.list_actions(session_id="s1", descending=False)
            assert [a.action_id for a in s1_actions] == ["a-1", "a-2"]

            # Filter by surface
            mcp_actions = journal.list_actions(surface=Surface.MCP, descending=False)
            assert [a.action_id for a in mcp_actions] == ["a-1", "a-4"]

            # Filter by multiple statuses
            unresolved = journal.list_actions(
                status=[ActionStatus.PENDING, ActionStatus.UNKNOWN],
                descending=True,
            )
            assert [a.action_id for a in unresolved] == ["a-4", "a-3"]

            # Limit
            assert len(journal.list_actions(limit=2)) == 2
            assert journal.list_actions(limit=0) == []

            with pytest.raises(ValueError):
                journal.list_actions(limit=-1)
