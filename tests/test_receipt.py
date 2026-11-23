"""Unit and CLI tests for ground-truth receipts (`sidefx report`) and Claude Code Stop hook."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sidefx.cli import main
from sidefx.journal import Journal, Surface
from sidefx.receipt import (
    DEFAULT_STOP_HOOK_COMMAND,
    claude_code_stop_hook_snippet,
    merge_claude_code_settings,
    sanitize_text,
)
from sidefx.reconcilers.base import format_summary_with_meta


def _populate_sample_journal(db_path: Path) -> None:
    with Journal(db_path) as journal:
        # Confirmed (sess-1)
        journal.begin_action(
            action_id="a-confirmed",
            session_id="sess-1",
            actor="agent",
            surface=Surface.CLI,
            tool_name="gh_issue_create",
            target="org/repo",
            payload={"k": 1},
            summary=format_summary_with_meta(
                "gh issue create (org/repo)", {"adapter": "github", "repo": "org/repo"}
            ),
        )
        journal.mark_confirmed(
            "agent",
            "org/repo",
            "a-confirmed",
            attempt=1,
            result_ref="https://github.com/org/repo/issues/10",
        )

        # Reconciled (sess-1)
        journal.begin_action(
            action_id="a-reconciled",
            session_id="sess-1",
            actor="agent",
            surface=Surface.CLI,
            tool_name="git_push",
            target="origin:refs/heads/feat",
            payload={"k": 2},
        )
        journal.mark_unknown("agent", "origin:refs/heads/feat", "a-reconciled", attempt=1)
        journal.mark_reconciled(
            "agent",
            "origin:refs/heads/feat",
            "a-reconciled",
            attempt=1,
            result_ref="origin refs/heads/feat@1234567890ab",
        )

        # Failed (sess-1)
        journal.begin_action(
            action_id="a-failed",
            session_id="sess-1",
            actor="agent",
            surface=Surface.MCP,
            tool_name="send_email",
            target="user@example.com",
            payload={"k": 3},
        )
        journal.mark_failed(
            "agent", "user@example.com", "a-failed", attempt=1, error="SMTP 550 rejected"
        )

        # Unknown with ANSI/newline/bidi injection attempt (sess-1)
        journal.begin_action(
            action_id="a-unknown",
            session_id="sess-1",
            actor="agent",
            surface=Surface.CLI,
            tool_name="gh_pr_create",
            target="org/`repo`",
            payload={"k": 4},
        )
        journal.mark_unknown(
            "agent",
            "org/`repo`",
            "a-unknown",
            attempt=1,
            error="timeout \x1b[31mred\x1b[0m\n## Forged Heading\u202e",
        )

        # Pending (sess-1)
        journal.begin_action(
            action_id="a-pending",
            session_id="sess-1",
            actor="agent",
            surface=Surface.CLI,
            tool_name="git_push",
            target="origin:refs/heads/wip",
            payload={"k": 5},
            summary=format_summary_with_meta(
                "git push origin wip", {"adapter": "git_push", "secret_internal": "hide-me"}
            ),
        )

        # Different session (sess-2)
        journal.begin_action(
            action_id="a-other-session",
            session_id="sess-2",
            actor="agent",
            surface=Surface.HTTP,
            tool_name="post_webhook",
            target="https://example.com/hook",
            payload={"k": 6},
        )
        journal.mark_confirmed(
            "agent",
            "https://example.com/hook",
            "a-other-session",
            attempt=1,
            result_ref="200 OK",
        )


class TestReceiptRenderingAndCli:
    def test_missing_db_is_readonly_and_does_not_create_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing_db = tmp_path / "nope" / "journal.db"
        rc_md = main(["--db", str(missing_db), "report"])
        assert rc_md == 0
        assert not missing_db.exists()
        assert not missing_db.parent.exists()
        assert "0 actions" in capsys.readouterr().out

        rc_json = main(["report", "--db", str(missing_db), "--json"])
        assert rc_json == 0
        assert not missing_db.exists()
        data = json.loads(capsys.readouterr().out)
        assert data["journal_exists"] is False
        assert data["total"] == 0
        assert data["actions"] == []

    def test_markdown_text_and_json_formats_and_sanitization(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        _populate_sample_journal(db_path)

        # Markdown filtered to sess-1
        rc_md = main(["--db", str(db_path), "report", "--session-id", "sess-1", "--markdown"])
        assert rc_md == 0
        md = capsys.readouterr().out
        assert "## sidefx Session Receipt" in md
        assert "5 total (1 confirmed, 1 reconciled, 1 failed, 2 unknown (1 pending))" in md
        assert "### Confirmed (1)" in md
        assert "https://github.com/org/repo/issues/10" in md
        assert "### Reconciled (1)" in md
        assert "origin refs/heads/feat@1234567890ab" in md
        assert "### Failed (1)" in md
        assert "SMTP 550 rejected" in md
        assert "### Unknown / Unresolved (2)" in md
        assert "[unknown]" in md and "[pending]" in md
        assert "a-other-session" not in md
        # Control chars, ANSI escapes, bidi overrides, and internal sidefx_meta stripped!
        assert "\x1b[" not in md
        assert "\u202e" not in md
        assert "\n## Forged Heading" not in md
        assert "sidefx_meta=" not in md
        assert "hide-me" not in md
        # Backtick inside target is safely fenced
        assert "`` org/`repo` ``" in md

        # Text format
        rc_txt = main(
            ["report", "--db", str(db_path), "--session-id", "sess-1", "--format", "text"]
        )
        assert rc_txt == 0
        txt = capsys.readouterr().out
        assert "sidefx Session Receipt" in txt
        assert "Confirmed (1):" in txt
        assert "Unknown / Unresolved (2):" in txt
        assert "\x1b[" not in txt
        assert "sidefx_meta=" not in txt

        # JSON format (unfiltered -> all 6 actions)
        rc_json = main(["--db", str(db_path), "report", "--format", "json"])
        assert rc_json == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["journal_exists"] is True
        assert payload["total"] == 6
        assert payload["counts"] == {
            "pending": 1,
            "confirmed": 2,
            "failed": 1,
            "reconciled": 1,
            "unknown": 1,
        }
        assert payload["unresolved_count"] == 2
        assert len(payload["groups"]["confirmed"]) == 2
        assert len(payload["groups"]["reconciled"]) == 1
        assert len(payload["groups"]["failed"]) == 1
        assert len(payload["groups"]["unknown"]) == 2

    def test_report_conflicting_format_flags_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        rc = main(["--db", str(db_path), "report", "--json", "--markdown"])
        assert rc == 1
        assert "Conflicting output format" in capsys.readouterr().err

    def test_report_reads_readonly_db_in_readonly_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        ro_db = ro_dir / "journal.db"
        _populate_sample_journal(ro_db)

        os.chmod(ro_db, 0o444)
        os.chmod(ro_dir, 0o555)
        try:
            rc = main(["--db", str(ro_db), "report", "--format", "json"])
            assert rc == 0
            data = json.loads(capsys.readouterr().out)
            assert data["total"] == 6
        finally:
            os.chmod(ro_dir, 0o755)
            os.chmod(ro_db, 0o644)

    def test_sanitize_text_strips_ansi_controls_and_bidi(self) -> None:
        raw = "\x1b[32mok\x1b[0m\r\nline2\x00\u202ebidi"
        assert sanitize_text(raw) == "ok  line2 bidi"


class TestClaudeCodeStopHookInit:
    def test_init_claude_code_prints_valid_snippet(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["init", "--claude-code"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == claude_code_stop_hook_snippet(DEFAULT_STOP_HOOK_COMMAND)
        stop_hooks = parsed["hooks"]["Stop"]
        assert stop_hooks[0]["hooks"][0]["command"] == "sidefx report --format markdown"

    def test_merge_preserves_existing_settings_and_is_idempotent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        existing = {
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "echo existing-stop"}]}],
            },
        }
        settings_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        # First merge via CLI `--path` adds sidefx Stop hook without clobbering existing keys
        rc1 = main(["init", "--claude-code", "--path", str(settings_file)])
        assert rc1 == 0
        assert "Merged" in capsys.readouterr().out

        merged = json.loads(settings_file.read_text(encoding="utf-8"))
        assert merged["permissions"] == existing["permissions"]
        assert merged["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
        assert len(merged["hooks"]["Stop"]) == 2
        assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo existing-stop"
        assert (
            merged["hooks"]["Stop"][1]["hooks"][0]["command"] == "sidefx report --format markdown"
        )

        # Second merge is idempotent (no duplicate!)
        rc2 = main(["init", "--claude-code", "--path", str(settings_file)])
        assert rc2 == 0
        assert "already present" in capsys.readouterr().out
        merged_again = json.loads(settings_file.read_text(encoding="utf-8"))
        assert len(merged_again["hooks"]["Stop"]) == 2

    def test_merge_refuses_corrupt_or_invalid_settings_without_clobbering(
        self, tmp_path: Path
    ) -> None:
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{broken json", encoding="utf-8")
        with pytest.raises(Exception, match="Malformed JSON"):
            merge_claude_code_settings(corrupt)
        assert corrupt.read_text(encoding="utf-8") == "{broken json"

        bad_hooks = tmp_path / "bad_hooks.json"
        bad_hooks.write_text('{"hooks": "not-an-object"}', encoding="utf-8")
        with pytest.raises(Exception, match="Expected 'hooks'"):
            merge_claude_code_settings(bad_hooks)
        assert bad_hooks.read_text(encoding="utf-8") == '{"hooks": "not-an-object"}'
