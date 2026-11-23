"""CLI tests for `sidefx --version`, `init`, `status`, error handling, and subcommand stubs."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from sidefx import __version__
from sidefx.cli import main
from sidefx.config import CONFIG_FILENAME, load_config
from sidefx.journal import ENV_DB_PATH, Journal, Surface


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

    def test_unrecognized_flag_on_status_still_errors(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["status", "--definitely-invalid-flag"])
        assert exc_info.value.code == 2


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

    def test_init_refuses_dangling_symlink_and_handles_path_errors_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Dangling symlink must NOT be followed to create target without --force
        target = tmp_path / "outside.json"
        link = tmp_path / CONFIG_FILENAME
        link.symlink_to(target)
        rc = main(["init", "--path", str(link)])
        assert rc == 1
        assert "already exists" in capsys.readouterr().err
        assert not target.exists()

        # Directory path returns clean exit 1 without --force
        rc_dir = main(["init", "--path", str(tmp_path)])
        assert rc_dir == 1
        assert "directory" in capsys.readouterr().err

        # Non-directory parent returns clean exit 1 instead of traceback
        blocker = tmp_path / "a_file"
        blocker.write_text("hello", encoding="utf-8")
        rc_notdir = main(["init", "--path", str(blocker / "sub" / CONFIG_FILENAME)])
        assert rc_notdir == 1
        assert "error:" in capsys.readouterr().err


class TestCliStatus:
    def test_status_missing_db_does_not_create_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "missing" / "journal.db"
        rc = main(["--db", str(db_path), "status"])
        assert rc == 0
        assert not db_path.exists()
        assert "0 actions" in capsys.readouterr().out

        # Also works when --db follows `status`
        rc_json = main(["status", "--db", str(db_path), "--json"])
        assert rc_json == 0
        data = json.loads(capsys.readouterr().out)
        assert data["journal_exists"] is False
        assert data["total"] == 0
        assert data["actions"] == []
        assert not db_path.exists()

    def test_status_populated_db_text_json_filters_and_terminal_sanitization(
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
            journal.mark_unknown(
                "agent",
                "origin/feat",
                "a-2",
                error="network\x1b[31m timeout\nforged line",
            )
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

        # Text output strips ANSI escapes and embedded newlines
        rc_text = main(["--db", str(db_path), "status"])
        assert rc_text == 0
        out_text = capsys.readouterr().out
        assert "Totals: 3" in out_text
        assert "confirmed=1" in out_text
        assert "unknown=1" in out_text
        assert "failed=1" in out_text
        assert "\x1b[" not in out_text
        assert "network timeout forged line" in out_text

        # JSON output with filters preserves raw fields
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
        assert payload["actions"][0]["attempt"] == 1

    def test_status_readonly_and_error_paths_b3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 1. Non-sidefx SQLite DB: must fail cleanly AND NOT mutate DB or enable WAL
        other_db = tmp_path / "other_app.db"
        conn = sqlite3.connect(other_db, isolation_level=None)
        conn.execute("CREATE TABLE items (id INT)")
        orig_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        conn.close()

        rc_other = main(["--db", str(other_db), "status"])
        assert rc_other == 1
        assert "missing 'actions' table" in capsys.readouterr().err

        conn = sqlite3.connect(other_db, isolation_level=None)
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == orig_mode
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='actions'"
            ).fetchone()
            is None
        )
        conn.close()

        # 2. Non-SQLite text file: clean exit 1
        notes = tmp_path / "notes.txt"
        notes.write_text("hello world\n", encoding="utf-8")
        rc_notes = main(["--db", str(notes), "status"])
        assert rc_notes == 1
        assert "error:" in capsys.readouterr().err

        # 3. DB with incompatible `actions` table: clean exit 1
        wrong_schema = tmp_path / "wrong.db"
        conn = sqlite3.connect(wrong_schema, isolation_level=None)
        conn.execute("CREATE TABLE actions (id INT)")
        conn.close()
        rc_wrong = main(["--db", str(wrong_schema), "status"])
        assert rc_wrong == 1
        assert "incompatible 'actions' table" in capsys.readouterr().err

        # 4. Read-only DB file inside read-only directory: succeeds cleanly with exit 0!
        ro_dir = tmp_path / "ro_dir"
        ro_dir.mkdir()
        ro_db = ro_dir / "journal.db"
        with Journal(ro_db) as j:
            j.begin_action(
                action_id="ro-a1",
                session_id="s1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_x",
                target="t",
                payload={"x": 1},
            )
        os.chmod(ro_db, 0o444)
        os.chmod(ro_dir, 0o555)
        try:
            rc_ro = main(["--db", str(ro_db), "status", "--json"])
            assert rc_ro == 0
            data = json.loads(capsys.readouterr().out)
            assert data["total"] == 1
            assert data["actions"][0]["action_id"] == "ro-a1"
        finally:
            os.chmod(ro_dir, 0o755)
            os.chmod(ro_db, 0o644)

    def test_status_db_precedence_cli_env_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_db = tmp_path / "from_cfg.db"
        env_db = tmp_path / "from_env.db"
        cli_db = tmp_path / "from_cli.db"
        for path, aid in ((cfg_db, "cfg-act"), (env_db, "env-act"), (cli_db, "cli-act")):
            with Journal(path) as j:
                j.begin_action(
                    action_id=aid,
                    session_id="s1",
                    actor="agent",
                    surface=Surface.MCP,
                    tool_name="create_x",
                    target="t",
                    payload={"id": aid},
                )

        cfg_file = tmp_path / CONFIG_FILENAME
        cfg_file.write_text(
            json.dumps({"version": 1, "journal_path": "from_cfg.db"}), encoding="utf-8"
        )
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        monkeypatch.delenv(ENV_DB_PATH, raising=False)

        # Config discovered from parent directory
        assert main(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["actions"][0]["action_id"] == "cfg-act"

        # SIDEFX_DB overrides config
        monkeypatch.setenv(ENV_DB_PATH, str(env_db))
        assert main(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["actions"][0]["action_id"] == "env-act"

        # --db overrides SIDEFX_DB
        assert main(["--db", str(cli_db), "status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["actions"][0]["action_id"] == "cli-act"

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
            ["report", "--format", "json"],
            ["report", "--session-id", "s1", "--markdown"],
            ["reconcile"],
            ["reconcile", "--all"],
            ["reconcile", "--action-id", "a1", "--dry-run"],
            ["wrap", "--", "echo", "hi"],
            ["wrap", "--action-id", "a", "--target", "t", "--", "gh", "issue", "create"],
            ["init", "--claude-code"],
        ],
    )
    def test_stub_subcommands_accept_flags_and_return_exit_code_2(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(argv)
        assert rc == 2
        err = capsys.readouterr().err
        assert "not yet implemented" in err
        assert "unrecognized arguments" not in err
