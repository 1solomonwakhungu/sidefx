"""Unit and CLI tests for ground-truth receipts (`sidefx report`) and Claude Code Stop hook."""

from __future__ import annotations

import json
import os
import stat
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
            "agent",
            "user@example.com",
            "a-failed",
            attempt=1,
            error="SMTP 550 [click](https://evil.example)",
        )

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
            error="timeout \x1b[31mred\x1b[0m\n## Forged Heading\u202e\u2028\U000e0041",
        )

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

        rc_md = main(["--db", str(db_path), "report", "--session-id", "sess-1", "--markdown"])
        assert rc_md == 0
        md = capsys.readouterr().out
        assert "## sidefx Session Receipt" in md
        assert "5 total (1 confirmed, 1 reconciled, 1 failed, 2 unknown (1 pending))" in md
        assert "### Confirmed (1)" in md
        assert "`https://github.com/org/repo/issues/10`" in md
        assert "### Reconciled (1)" in md
        assert "`origin refs/heads/feat@1234567890ab`" in md
        assert "### Failed (1)" in md
        # Untrusted error text containing Markdown link syntax is enclosed in code span (N5!)
        assert "`SMTP 550 [click](https://evil.example)`" in md
        assert "### Unknown / Unresolved (2)" in md
        assert "[unknown]" in md and "[pending]" in md
        assert "a-other-session" not in md
        assert "\x1b[" not in md
        assert "\u202e" not in md
        assert "\u2028" not in md
        assert "\U000e0041" not in md
        assert "\n## Forged Heading" not in md
        assert "sidefx_meta=" not in md
        assert "hide-me" not in md
        assert "`` org/`repo` ``" in md

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

    def test_sanitize_text_strips_ansi_controls_bidi_and_tag_chars(self) -> None:
        raw = "\x1b[32mok\x1b[0m\r\nline2\x00\u202e\u200b\u2028\U000e0020bidi"
        assert sanitize_text(raw) == "ok  line2  bidi"


class TestClaudeCodeStopHookInit:
    def test_init_claude_code_prints_valid_snippet_and_absolutizes_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc = main(["init", "--claude-code"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == claude_code_stop_hook_snippet(DEFAULT_STOP_HOOK_COMMAND)

        # Relative --db is absolutized in generated Stop hook command (N7!)
        rc_db = main(["init", "--claude-code", "--db", "rel/journal.db"])
        assert rc_db == 0
        parsed_db = json.loads(capsys.readouterr().out)
        cmd_str = parsed_db["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert str((tmp_path / "rel" / "journal.db").resolve()) in cmd_str

    def test_init_claude_code_rejects_path_flag_to_protect_sidefx_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """B7: `--path` must NOT merge hooks into `.sidefx.json`."""
        cfg = tmp_path / ".sidefx.json"
        rc_init = main(["init", "--path", str(cfg)])
        assert rc_init == 0
        orig = cfg.read_text(encoding="utf-8")
        _ = capsys.readouterr()

        rc_bad = main(["init", "--claude-code", "--path", str(cfg)])
        assert rc_bad == 2
        assert "--settings" in capsys.readouterr().err
        assert cfg.read_text(encoding="utf-8") == orig

        # `--write` / `--settings` without `--claude-code` also exits 2 cleanly
        rc_no_cc = main(["init", "--write"])
        assert rc_no_cc == 2
        assert "--claude-code" in capsys.readouterr().err

    def test_merge_via_settings_flag_preserves_existing_settings_symlink_and_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """B7 & N7: `--settings` merges idempotently while preserving symlink target and mode."""
        real_target = tmp_path / "dotfiles" / "claude-settings.json"
        real_target.parent.mkdir(parents=True)
        existing = {
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "echo existing-stop"}]}],
            },
        }
        real_target.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.chmod(real_target, 0o640)

        settings_link = tmp_path / ".claude" / "settings.json"
        settings_link.parent.mkdir(parents=True)
        settings_link.symlink_to(real_target)

        rc1 = main(["init", "--claude-code", "--settings", str(settings_link)])
        assert rc1 == 0
        assert "Merged" in capsys.readouterr().out
        assert settings_link.is_symlink()
        assert stat.S_IMODE(real_target.stat().st_mode) == 0o640

        merged = json.loads(real_target.read_text(encoding="utf-8"))
        assert merged["permissions"] == existing["permissions"]
        assert merged["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
        assert len(merged["hooks"]["Stop"]) == 2
        assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo existing-stop"
        assert merged["hooks"]["Stop"][1]["hooks"][0]["command"] == DEFAULT_STOP_HOOK_COMMAND

        # Idempotent re-run
        rc2 = main(["init", "--claude-code", "--settings", str(settings_link)])
        assert rc2 == 0
        assert "already present" in capsys.readouterr().out
        assert len(json.loads(real_target.read_text(encoding="utf-8"))["hooks"]["Stop"]) == 2

    def test_write_flag_merges_into_default_claude_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc = main(["init", "--claude-code", "--write"])
        assert rc == 0
        assert "Merged" in capsys.readouterr().out
        target = tmp_path / ".claude" / "settings.json"
        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == DEFAULT_STOP_HOOK_COMMAND

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
