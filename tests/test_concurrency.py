"""Multi-thread and multi-process concurrency tests for atomic journal claims."""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sidefx.journal import ActionStatus, BeginDecision, Journal, Surface


def _mp_claim_worker(
    db_path_str: str,
    barrier: Any,
    action_id: str,
    worker_idx: int,
    result_queue: Any,
) -> None:
    """Subprocess entry point synchronized via a real cross-process Barrier."""
    try:
        barrier.wait(timeout=20.0)
        with Journal(db_path_str) as journal:
            res = journal.begin_action(
                action_id=action_id,
                session_id=f"proc-session-{worker_idx}",
                actor="shared-agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "Concurrent issue", "priority": 1},
            )
            result_queue.put(("ok", res.decision.value, res.retried, res.record.attempt))
    except BaseException as exc:  # pragma: no cover - surfaces unexpected worker errors
        result_queue.put(("err", type(exc).__name__, str(exc), -1))


class TestThreadConcurrency:
    def test_separate_connections_per_thread_single_winner(self, tmp_path: Path) -> None:
        db_path = tmp_path / "thread_separate.db"
        worker_count = 16
        barrier = threading.Barrier(worker_count)

        def attempt(idx: int) -> tuple[BeginDecision, bool]:
            with Journal(db_path) as journal:
                barrier.wait(timeout=10.0)
                res = journal.begin_action(
                    action_id="race-thread-1",
                    session_id=f"s-{idx}",
                    actor="agent",
                    surface=Surface.MCP,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Race"},
                )
                return (res.decision, res.retried)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(attempt, range(worker_count)))

        proceeds = [r for r in results if r[0] == BeginDecision.PROCEED]
        unknowns = [r for r in results if r[0] == BeginDecision.OUTCOME_UNKNOWN]
        assert len(proceeds) == 1
        assert proceeds[0][1] is False
        assert len(unknowns) == worker_count - 1

        # Confirm once, then verify all concurrent callers get REPLAY
        with Journal(db_path) as journal:
            journal.mark_confirmed(
                "agent",
                "org/repo",
                "race-thread-1",
                attempt=1,
                result_ref="https://github.com/org/repo/issues/101",
            )

        replay_barrier = threading.Barrier(worker_count)

        def attempt_replay(idx: int) -> tuple[BeginDecision, str | None]:
            with Journal(db_path) as journal:
                replay_barrier.wait(timeout=10.0)
                res = journal.begin_action(
                    action_id="race-thread-1",
                    session_id=f"s-replay-{idx}",
                    actor="agent",
                    surface=Surface.MCP,
                    tool_name="create_issue",
                    target="org/repo",
                    payload={"title": "Race"},
                )
                return (res.decision, res.record.result_ref)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            replay_results = list(pool.map(attempt_replay, range(worker_count)))

        assert all(
            dec == BeginDecision.REPLAY and ref == "https://github.com/org/repo/issues/101"
            for dec, ref in replay_results
        )

    def test_shared_journal_instance_thread_safety(self, tmp_path: Path) -> None:
        db_path = tmp_path / "thread_shared.db"
        worker_count = 16
        barrier = threading.Barrier(worker_count)

        with Journal(db_path) as shared_journal:

            def attempt(idx: int) -> BeginDecision:
                barrier.wait(timeout=10.0)
                res = shared_journal.begin_action(
                    action_id="shared-race-1",
                    session_id=f"s-{idx}",
                    actor="agent",
                    surface=Surface.CLI,
                    tool_name="git_push",
                    target="origin/main",
                    payload={"sha": "deadbeef"},
                )
                return res.decision

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                decisions = list(pool.map(attempt, range(worker_count)))

        assert decisions.count(BeginDecision.PROCEED) == 1
        assert decisions.count(BeginDecision.OUTCOME_UNKNOWN) == worker_count - 1

    def test_concurrent_retry_of_failed_action_single_winner(self, tmp_path: Path) -> None:
        db_path = tmp_path / "thread_retry.db"
        with Journal(db_path) as journal:
            first = journal.begin_action(
                action_id="retry-race",
                session_id="s-0",
                actor="agent",
                surface=Surface.HTTP,
                tool_name="post_webhook",
                target="https://example.com/hook",
                payload={"n": 1},
            )
            assert first.decision == BeginDecision.PROCEED
            journal.mark_failed(
                "agent",
                "https://example.com/hook",
                "retry-race",
                attempt=1,
                error="503 unavailable",
            )

        worker_count = 16
        barrier = threading.Barrier(worker_count)

        def retry_worker(idx: int) -> tuple[BeginDecision, bool, int]:
            with Journal(db_path) as journal:
                barrier.wait(timeout=10.0)
                res = journal.begin_action(
                    action_id="retry-race",
                    session_id=f"s-retry-{idx}",
                    actor="agent",
                    surface=Surface.HTTP,
                    tool_name="post_webhook",
                    target="https://example.com/hook",
                    payload={"n": 1},
                )
                return (res.decision, res.retried, res.record.attempt)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(retry_worker, range(worker_count)))

        proceeds = [r for r in results if r[0] == BeginDecision.PROCEED]
        unknowns = [r for r in results if r[0] == BeginDecision.OUTCOME_UNKNOWN]
        assert len(proceeds) == 1
        assert proceeds[0][1] is True
        assert proceeds[0][2] == 2
        assert len(unknowns) == worker_count - 1


class TestProcessConcurrency:
    def test_multiprocess_cold_start_and_retry_single_winner(self, tmp_path: Path) -> None:
        db_path = tmp_path / "multiprocess.db"
        worker_count = 10
        ctx = multiprocessing.get_context("spawn")

        def run_wave(action_id: str) -> list[tuple[str, str, bool, int]]:
            barrier = ctx.Barrier(worker_count)
            queue: Any = ctx.Queue()
            procs = [
                ctx.Process(
                    target=_mp_claim_worker,
                    args=(str(db_path), barrier, action_id, i, queue),
                )
                for i in range(worker_count)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=30.0)
                assert proc.exitcode == 0
            return [queue.get(timeout=5.0) for _ in range(worker_count)]

        # Wave 1: cold-start processes race behind a real Barrier on a brand-new DB
        wave1 = run_wave("mp-action-1")
        assert all(row[0] == "ok" for row in wave1), wave1
        wave1_proceeds = [r for r in wave1 if r[1] == BeginDecision.PROCEED.value]
        wave1_unknowns = [r for r in wave1 if r[1] == BeginDecision.OUTCOME_UNKNOWN.value]
        assert len(wave1_proceeds) == 1
        assert wave1_proceeds[0][2] is False
        assert wave1_proceeds[0][3] == 1
        assert len(wave1_unknowns) == worker_count - 1

        # Verify journal holds exactly one pending row, then mark failed cleanly
        with Journal(db_path) as journal:
            rows = journal.list_actions()
            assert len(rows) == 1
            assert rows[0].status == ActionStatus.PENDING
            journal.mark_failed(
                "shared-agent",
                "org/repo",
                "mp-action-1",
                attempt=1,
                error="transient error",
            )

        # Wave 2: processes race behind a real Barrier to retry the cleanly-failed action
        wave2 = run_wave("mp-action-1")
        assert all(row[0] == "ok" for row in wave2), wave2
        wave2_proceeds = [r for r in wave2 if r[1] == BeginDecision.PROCEED.value]
        wave2_unknowns = [r for r in wave2 if r[1] == BeginDecision.OUTCOME_UNKNOWN.value]
        assert len(wave2_proceeds) == 1
        assert wave2_proceeds[0][2] is True
        assert wave2_proceeds[0][3] == 2
        assert len(wave2_unknowns) == worker_count - 1
