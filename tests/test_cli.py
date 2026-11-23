"""CLI tests for `sidefx --version`, `init`, `status`, and subcommand stubs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidefx import __version__
from sidefx.cli import main
from sidefx.config import CONFIG_FILENAME, load_config
from sidefx.journal import Journal, Surface


class TestCliBasics:
    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert f"sidefx {__version__}" in out

    def test_no_args_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "sidefx" in out
        assert "status" in out
        assert "init" in out


class TestCliInit:
    def test_init_creates_valid_config_and_refuses_overwrite_without_force(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dest = tmp_path / "nested" / CONFIG_FILENAME
        rc = main(["init", "--path", str(dest)])
        assert rc == 0
        assert dest.is_file()
        assert str(dest) in capsys.readouterr().out

        loaded = load_config(dest)
        assert loaded.version == 1

        # Second run without --force fails without modifying file
        dest.write_text('{"version": 1, "default_actor": "custom"}\n', encoding="utf-8")
        rc2 = main(["init", "--path", str(dest)])
        assert rc2 == 1
        assert "already exists" in capsys.readouterr().err
        assert load_config(dest).default_actor == "custom"

        # With --force it overwrites cleanly
        rc3 = main(["init", "--path", str(dest), "--force"])
        assert rc3 == 0
        assert load_config(dest).default_actor == "agent"

    def test_init_force_recovers_corrupted_cwd_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / CONFIG_FILENAME).write_text("{corrupt", encoding="utf-8")
        rc = main(["init", "--force"])
        assert rc == 0
        assert load_config(cwd=tmp_path).version == 1
        _ = capsys.readouterr()


class TestCliStatus:
    def test_status_missing_db_does_not_create_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "missing" / "journal.db"
        rc = main(["--db", str(db_path), "status"])
        assert rc == 0
        assert not db_path.exists()
        assert "0 actions" in capsys.readouterr().out

        rc_json = main(["--db", str(db_path), "status", "--json"])
        assert rc_json == 0
        data = json.loads(capsys.readouterr().out)
        assert data["journal_exists"] is False
        assert data["total"] == 0
        assert data["actions"] == []
        assert not db_path.exists()

    def test_status_populated_db_text_and_json_with_filters(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="a-1",
                session_id="sess-1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"title": "One"},
            )
            journal.mark_confirmed(
                "agent",
                "org/repo",
                "a-1",
                result_ref="https://github.com/org/repo/issues/1",
            )
            journal.begin_action(
                action_id="a-2",
                session_id="sess-1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin/feat",
                payload={"sha": "111"},
            )
            journal.mark_unknown("agent", "origin/feat", "a-2", error="network timeout")
            journal.begin_action(
                action_id="a-3",
                session_id="sess-2",
                actor="agent",
                surface=Surface.HTTP,
                tool_name="post_webhook",
                target="https://example.com/hook",
                payload={"ok": True},
            )
            journal.mark_failed("agent", "https://example.com/hook", "a-3", error="HTTP 500")

        # Text output
        rc_text = main(["--db", str(db_path), "status"])
        assert rc_text == 0
        out_text = capsys.readouterr().out
        assert "Totals: 3" in out_text
        assert "confirmed=1" in out_text
        assert "unknown=1" in out_text
        assert "failed=1" in out_text
        assert "create_issue" in out_text
        assert "git_push" in out_text

        # JSON output with filters
        rc_json = main(
            [
                "--db",
                str(db_path),
                "status",
                "--session-id",
                "sess-1",
                "--status",
                "unknown",
                "--surface",
                "cli",
                "--json",
            ]
        )
        assert rc_json == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["journal_exists"] is True
        assert payload["total"] == 1
        assert payload["counts"]["unknown"] == 1
        assert len(payload["actions"]) == 1
        assert payload["actions"][0]["action_id"] == "a-2"
        assert payload["actions"][0]["error"] == "network timeout"

    def test_invalid_config_exits_with_code_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        rc = main(["--config", str(bad), "status"])
        assert rc == 1
        assert "Malformed JSON" in capsys.readouterr().err


class TestCliSubcommandStubs:
    @pytest.mark.parametrize(
        "argv",
        [
            ["report"],
            ["reconcile"],
            ["wrap", "--", "echo", "hi"],
            ["mcp-proxy", "--", "python3", "-V"],
            ["init", "--claude-code"],
        ],
    )
    def test_stub_subcommands_return_exit_code_2(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(argv)
        assert rc == 2
        assert "not yet implemented" in capsys.readouterr().err
