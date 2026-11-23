"""Tests for `sidefx resolve` and the agent-facing recovery guidance it backs."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from sidefx.cli import main
from sidefx.guidance import (
    OUTCOME_UNKNOWN_BANNER,
    outcome_unknown_cli_hint,
    outcome_unknown_data,
    outcome_unknown_message,
    payload_mismatch_message,
    preflight_denied_message,
    reconcile_command,
    replay_notice,
    resolve_command,
)
from sidefx.journal import (
    ActionStatus,
    BeginDecision,
    Journal,
    PayloadMismatchError,
    Surface,
)
from sidefx.wrapper import EXIT_OUTCOME_UNKNOWN, EXIT_PAYLOAD_MISMATCH, EXIT_TIMEOUT

ACTOR = "agent"
TARGET = "mcp:tickets"


def _begin(journal: Journal, action_id: str, *, target: str = TARGET) -> None:
    journal.begin_action(
        action_id=action_id,
        session_id="sess-1",
        actor=ACTOR,
        surface=Surface.MCP,
        tool_name="create_ticket",
        target=target,
        payload={"title": "hello"},
    )


def _begin_unknown(journal: Journal, action_id: str, *, target: str = TARGET) -> None:
    _begin(journal, action_id, target=target)
    journal.mark_unknown(ACTOR, target, action_id, error="upstream timed out")


def _retry_decision(journal: Journal, action_id: str) -> BeginDecision:
    return journal.begin_action(
        action_id=action_id,
        session_id="sess-2",
        actor=ACTOR,
        surface=Surface.MCP,
        tool_name="create_ticket",
        target=TARGET,
        payload={"title": "hello"},
    ).decision


class TestResolveCli:
    def test_landed_marks_reconciled_and_later_retries_replay(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            _begin_unknown(journal, "tix-1")
            assert _retry_decision(journal, "tix-1") == BeginDecision.OUTCOME_UNKNOWN

            rc = main(
                [
                    "resolve",
                    "--db",
                    str(db_path),
                    "--action-id",
                    "tix-1",
                    "--landed",
                    "--ref",
                    "https://tix.example/T-9",
                    "--note",
                    "found via get_ticket",
                ]
            )
            assert rc == 0
            out = capsys.readouterr().out
            assert "as landed (unknown -> reconciled)" in out

            rec = journal.get_action(ACTOR, TARGET, "tix-1")
            assert rec is not None
            assert rec.status == ActionStatus.RECONCILED
            assert rec.result_ref == "https://tix.example/T-9"
            assert rec.summary == "manually resolved as landed: found via get_ticket"
            assert _retry_decision(journal, "tix-1") == BeginDecision.REPLAY

    def test_not_landed_marks_failed_and_reopens_the_same_call(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            _begin_unknown(journal, "tix-2")

            rc = main(["resolve", "--db", str(db_path), "--action-id", "tix-2", "--not-landed"])
            assert rc == 0
            assert "as not landed (unknown -> failed)" in capsys.readouterr().out

            rec = journal.get_action(ACTOR, TARGET, "tix-2")
            assert rec is not None
            assert rec.status == ActionStatus.FAILED
            assert rec.error == "manually resolved as not landed"

            assert _retry_decision(journal, "tix-2") == BeginDecision.PROCEED
            retried = journal.get_action(ACTOR, TARGET, "tix-2")
            assert retried is not None
            assert retried.attempt == 2

    def test_pending_requires_force_pending(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            _begin(journal, "tix-3")

            rc = main(["resolve", "--db", str(db_path), "--action-id", "tix-3", "--not-landed"])
            assert rc == 1
            assert "still pending" in capsys.readouterr().err
            rec = journal.get_action(ACTOR, TARGET, "tix-3")
            assert rec is not None
            assert rec.status == ActionStatus.PENDING

            rc_forced = main(
                [
                    "resolve",
                    "--db",
                    str(db_path),
                    "--action-id",
                    "tix-3",
                    "--not-landed",
                    "--force-pending",
                ]
            )
            assert rc_forced == 0
            rec = journal.get_action(ACTOR, TARGET, "tix-3")
            assert rec is not None
            assert rec.status == ActionStatus.FAILED

    def test_terminal_actions_are_idempotent_or_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            _begin(journal, "tix-4")
            journal.mark_confirmed(ACTOR, TARGET, "tix-4", result_ref="https://tix.example/T-4")

            same = main(
                ["resolve", "--db", str(db_path), "--action-id", "tix-4", "--landed", "--json"]
            )
            assert same == 0
            payload = json.loads(capsys.readouterr().out)
            assert payload["applied"] is False
            assert payload["current_status"] == "confirmed"

            contradict = main(
                ["resolve", "--db", str(db_path), "--action-id", "tix-4", "--not-landed"]
            )
            assert contradict == 1
            assert "already confirmed" in capsys.readouterr().err
            rec = journal.get_action(ACTOR, TARGET, "tix-4")
            assert rec is not None
            assert rec.status == ActionStatus.CONFIRMED
            assert rec.result_ref == "https://tix.example/T-4"

    def test_ambiguous_action_id_needs_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            _begin_unknown(journal, "dup", target="mcp:a")
            _begin_unknown(journal, "dup", target="mcp:b")

            rc = main(["resolve", "--db", str(db_path), "--action-id", "dup", "--landed"])
            assert rc == 2
            err = capsys.readouterr().err
            assert "matches 2 actions" in err
            assert "target=mcp:a" in err and "target=mcp:b" in err

            rc_one = main(
                [
                    "resolve",
                    "--db",
                    str(db_path),
                    "--action-id",
                    "dup",
                    "--target",
                    "mcp:b",
                    "--landed",
                    "--json",
                ]
            )
            assert rc_one == 0
            payload = json.loads(capsys.readouterr().out)
            assert payload["target"] == "mcp:b"
            assert payload["applied"] is True
            assert payload["previous_status"] == "unknown"
            assert payload["current_status"] == "reconciled"

            untouched = journal.get_action(ACTOR, "mcp:a", "dup")
            assert untouched is not None
            assert untouched.status == ActionStatus.UNKNOWN

    def test_usage_and_lookup_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"

        missing_db = main(["resolve", "--db", str(db_path), "--action-id", "x", "--landed"])
        assert missing_db == 1
        assert "journal database not found" in capsys.readouterr().err
        assert not db_path.exists()

        with Journal(db_path) as journal:
            _begin_unknown(journal, "tix-5")

            unknown_id = main(["resolve", "--db", str(db_path), "--action-id", "nope", "--landed"])
            assert unknown_id == 1
            assert "action_id not found: nope" in capsys.readouterr().err

            bad_ref = main(
                [
                    "resolve",
                    "--db",
                    str(db_path),
                    "--action-id",
                    "tix-5",
                    "--not-landed",
                    "--ref",
                    "https://tix.example/T-5",
                ]
            )
            assert bad_ref == 2
            assert "--ref is only valid with --landed" in capsys.readouterr().err

            with pytest.raises(SystemExit) as exc_info:
                main(["resolve", "--db", str(db_path), "--action-id", "tix-5"])
            assert exc_info.value.code == 2
            capsys.readouterr()

            rec = journal.get_action(ACTOR, TARGET, "tix-5")
            assert rec is not None
            assert rec.status == ActionStatus.UNKNOWN


class TestWrapRecoveryFlow:
    """A generic command has no reconciler: `resolve` is the only way to unblock it."""

    # Writes a marker file then hangs on first run; exits cleanly once the marker exists.
    _SCRIPT = (
        "import pathlib, sys, time\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "if marker.exists():\n"
        "    print('done')\n"
        "    raise SystemExit(0)\n"
        "marker.write_text('x')\n"
        "time.sleep(30)\n"
    )

    def _wrap_args(self, db_path: Path, marker: Path) -> list[str]:
        return [
            "wrap",
            "--db",
            str(db_path),
            "--action-id",
            "deploy-1",
            "--timeout",
            "1",
            "--min-retry-age",
            "0",
            "--",
            sys.executable,
            "-c",
            self._SCRIPT,
            str(marker),
        ]

    def test_timeout_prints_next_steps_and_resolve_not_landed_reopens_it(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "state" / "journal.db"
        marker = tmp_path / "marker"
        args = self._wrap_args(db_path, marker)

        assert main(args) == EXIT_TIMEOUT
        err = capfd.readouterr().err
        assert OUTCOME_UNKNOWN_BANNER in err
        assert "No reconciler covers this command" in err
        assert resolve_command("deploy-1", landed=False, db_path=db_path) in err

        # A blind retry stays blocked and repeats the same guidance.
        assert main(args) == EXIT_OUTCOME_UNKNOWN
        err = capfd.readouterr().err
        assert resolve_command("deploy-1", landed=True, db_path=db_path) in err

        assert (
            main(["resolve", "--db", str(db_path), "--action-id", "deploy-1", "--not-landed"]) == 0
        )
        capfd.readouterr()

        assert main(args) == 0
        assert "done" in capfd.readouterr().out
        with Journal.open_readonly(db_path) as journal:
            rec = journal.get_action(ACTOR, Path(sys.executable).name, "deploy-1")
            assert rec is not None
            assert rec.status == ActionStatus.CONFIRMED
            assert rec.attempt == 2

    def test_resolve_landed_makes_the_retry_replay_without_executing(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        marker = tmp_path / "marker"
        args = self._wrap_args(db_path, marker)

        assert main(args) == EXIT_TIMEOUT
        marker.unlink()
        assert (
            main(
                [
                    "resolve",
                    "--db",
                    str(db_path),
                    "--action-id",
                    "deploy-1",
                    "--landed",
                    "--ref",
                    "deploy/42",
                ]
            )
            == 0
        )
        capfd.readouterr()

        assert main(args) == 0
        captured = capfd.readouterr()
        assert "deploy/42" in captured.out
        assert "replayed reconciled action deploy-1" in captured.err
        assert not marker.exists()

    def test_payload_mismatch_says_what_to_send_instead(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        base = ["wrap", "--db", str(db_path), "--action-id", "echo-1", "--", sys.executable, "-c"]

        assert main([*base, "print('one')"]) == 0
        capfd.readouterr()
        assert main([*base, "print('two')"]) == EXIT_PAYLOAD_MISMATCH
        err = capfd.readouterr().err
        assert "Payload mismatch for action 'echo-1'" in err
        assert "use a new --action-id" in err
        assert "Nothing was sent" in err

    def test_missing_binary_error_ends_with_newline(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        rc = main(["wrap", "--db", str(db_path), "--", "sidefx-no-such-binary-xyz"])
        assert rc == 127
        err = capfd.readouterr().err
        assert "sidefx-no-such-binary-xyz" in err
        assert err.endswith("\n")


class TestJournalDirectory:
    def test_new_journal_directory_is_git_ignored(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".sidefx" / "journal.db"
        with Journal(db_path):
            pass
        ignore = db_path.parent / ".gitignore"
        assert ignore.read_text(encoding="utf-8").splitlines()[-1] == "*"

    def test_existing_directory_is_left_alone(self, tmp_path: Path) -> None:
        with Journal(tmp_path / "journal.db"):
            pass
        assert not (tmp_path / ".gitignore").exists()


class TestGuidanceText:
    def test_commands_are_shell_safe_and_carry_the_journal_path(self) -> None:
        action_id = "fix it; rm -rf ~"
        db_path = Path("/tmp/my journal/journal.db")
        for command in (
            reconcile_command(action_id, db_path=db_path),
            resolve_command(action_id, landed=True, db_path=db_path),
            resolve_command(action_id, landed=False, db_path=db_path),
        ):
            tokens = shlex.split(command)
            assert tokens[0] == "sidefx"
            assert tokens[tokens.index("--db") + 1] == str(db_path)
            assert tokens[tokens.index("--action-id") + 1] == action_id

        assert shlex.split(resolve_command("a1", landed=False)) == [
            "sidefx",
            "resolve",
            "--action-id",
            "a1",
            "--not-landed",
        ]

    def test_outcome_unknown_message_and_data_carry_recovery_steps(self) -> None:
        message = outcome_unknown_message(
            action_id="a1",
            marker="m1",
            tool_name="create_ticket",
            detail="upstream MCP server timed out after 2s.",
            db_path="/tmp/j.db",
        )
        assert message.startswith(
            f"{OUTCOME_UNKNOWN_BANNER} (action_id=a1, marker=m1, tool=create_ticket): "
            "upstream MCP server timed out after 2s. Next steps: 1) Do not re-send"
        )
        assert "\n" not in message
        assert reconcile_command("a1", db_path="/tmp/j.db") in message
        assert resolve_command("a1", landed=True, db_path="/tmp/j.db") in message
        assert "`--not-landed`" in message

        data = outcome_unknown_data("a1", db_path="/tmp/j.db")
        assert data["journal"] == "/tmp/j.db"
        assert data["reconcile_command"] == reconcile_command("a1", db_path="/tmp/j.db")
        assert data["resolve_commands"] == {
            "landed": resolve_command("a1", landed=True, db_path="/tmp/j.db"),
            "not_landed": resolve_command("a1", landed=False, db_path="/tmp/j.db"),
        }
        assert len(data["next_steps"]) == 4

    def test_cli_hint_depends_on_reconciler_coverage(self) -> None:
        covered = outcome_unknown_cli_hint("p1", has_reconciler=True)
        assert "Re-run this exact command" in covered
        assert "never writes twice" in covered

        uncovered = outcome_unknown_cli_hint("p1", has_reconciler=False)
        assert "No reconciler covers this command" in uncovered
        assert resolve_command("p1", landed=True) in uncovered

    def test_mismatch_preflight_and_replay_wording(self) -> None:
        exc = PayloadMismatchError(
            actor="agent",
            target="mcp:tickets",
            action_id="tix-1",
            expected_hash="a" * 64,
            actual_hash="b" * 64,
            detail="tool_name mismatch",
        )
        mismatch = payload_mismatch_message(exc, id_source="`_sidefx_action_id`")
        assert "use a new `_sidefx_action_id`" in mismatch
        assert "(tool_name mismatch)" in mismatch
        assert "a" * 13 not in mismatch

        denied = preflight_denied_message("refund_payment", "amount 500 exceeds maximum 50.")
        assert denied.startswith(
            "Preflight check denied tool 'refund_payment': amount 500 exceeds maximum 50. This is"
        )
        assert "unchanged retry will be denied again" in denied

        notice = replay_notice(tool_name="create_ticket", action_id="det-1", status="confirmed")
        assert notice.startswith("[sidefx] Duplicate call not re-sent")
        assert "_sidefx_action_id" in notice
