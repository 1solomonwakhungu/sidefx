"""Regression tests for v0.1.1 security hardening (redaction, permissions, sanitization)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sidefx.cli import _format_status_text
from sidefx.guidance import outcome_unknown_cli_hint, outcome_unknown_message
from sidefx.journal import ActionRecord, ActionStatus, Journal, Surface
from sidefx.reconcilers.base import format_summary_with_meta
from sidefx.reconcilers.git_push import GitPushSpec, inspect_git_push
from sidefx.session import ensure_persisted_session
from sidefx.wrapper import _redact_url_for_display, wrap_command


def _init_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_git_push_redacts_inline_credentials_on_non_reconcilable_paths(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    secret = "tok_" + "SUPERSECRETVALUE1234567890"
    remote_url = f"https://x-access-token:{secret}@github.com/acme/widgets.git"

    # --tags hits the non-reconcilable early return
    spec_tags = inspect_git_push(
        ["git", "push", remote_url, "--tags"], cwd=repo, probe_remote=False
    )
    assert spec_tags is not None
    assert spec_tags.reconcilable is False
    assert secret not in (spec_tags.remote or "")
    assert secret not in str(spec_tags.to_meta())

    # multi-refspec hits the second early return
    spec_multi = inspect_git_push(
        ["git", "push", remote_url, "main", "HEAD"], cwd=repo, probe_remote=False
    )
    assert spec_multi is not None
    assert secret not in (spec_multi.remote or "")
    assert secret not in str(spec_multi.to_meta())

    # Direct GitPushSpec.to_meta() always redacts even if constructed raw
    raw_spec = GitPushSpec(
        cwd=str(repo),
        remote=remote_url,
        remote_ref=None,
        expected_sha=None,
        before_sha=None,
        before_known=False,
        is_delete=False,
        is_force=False,
        reconcilable=False,
    )
    meta = raw_spec.to_meta()
    assert meta["remote"] == "https://github.com/acme/widgets.git"
    assert secret not in str(meta)


def test_redact_url_masks_webhook_tokens_and_preserves_normal_paths() -> None:
    # Slack-style incoming webhook path
    slack_token = "AbCdEfGhIjKl" + "MnOpQrStUvWx"
    slack = f"https://hooks.example.test/services/T00000000/B00000000/{slack_token}"
    redacted_slack = _redact_url_for_display(slack)
    assert slack_token not in redacted_slack
    assert redacted_slack.startswith(
        "https://hooks.example.test/services/T00000000/B00000000/<redacted:"
    )

    # Discord-style webhook path
    discord_token = "aBcDeFgHiJkLmNoPqRsTuVwXyZ" + "0123456789_abcdEFGHijklMNOP"
    discord = f"https://hooks.example.test/api/webhooks/123456789012345678/{discord_token}"
    redacted_discord = _redact_url_for_display(discord)
    assert discord_token[:16] not in redacted_discord
    assert redacted_discord.startswith(
        "https://hooks.example.test/api/webhooks/123456789012345678/<redacted:"
    )

    # Telegram-style bot token path
    tg_token = "123456789:" + "AAH_testTokenValue123456"
    tg = f"https://hooks.example.test/bot{tg_token}/sendMessage"
    redacted_tg = _redact_url_for_display(tg)
    assert "AAH_testTokenValue123456" not in redacted_tg
    assert redacted_tg.startswith("https://hooks.example.test/bot<redacted:")
    assert redacted_tg.endswith("/sendMessage")

    # Deterministic: same URL produces same redacted target
    assert _redact_url_for_display(slack) == redacted_slack

    # Normal REST & GitHub URLs stay intact (only userinfo/query stripped)
    gh = "https://user:pass@api.github.com/repos/acme/my-cool-app/issues/42/comments?token=abc"
    assert (
        _redact_url_for_display(gh)
        == "https://api.github.com/repos/acme/my-cool-app/issues/42/comments"
    )
    normal = "https://api.example.com/v1/orders/42"
    assert _redact_url_for_display(normal) == normal


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions check")
def test_journal_and_session_set_owner_only_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / ".sidefx" / "journal.db"
    with Journal(db_path):
        pass

    dir_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    db_mode = stat.S_IMODE(db_path.stat().st_mode)
    assert dir_mode == 0o700
    assert db_mode == 0o600

    db_path2 = tmp_path / ".sidefx-sess" / "journal.db"
    ensure_persisted_session(db_path2)
    assert stat.S_IMODE(db_path2.parent.stat().st_mode) == 0o700


def test_outcome_unknown_sanitizes_and_caps_untrusted_detail(tmp_path: Path) -> None:
    # Guidance message sanitizes control/BiDi chars and caps length
    noisy = "boom \x1b[31mred\x1b[0m \u202ereversed\nfake line " + ("x" * 500)
    msg = outcome_unknown_message(
        action_id="a1",
        marker="m1",
        detail=noisy,
        tool_name="deploy",
    )
    assert "\n" not in msg
    assert "\x1b" not in msg
    assert "\u202e" not in msg
    assert "x" * 350 not in msg

    # Non-reconcilable CLI hint no longer tells caller to re-run after --landed
    hint = outcome_unknown_cli_hint("a1", has_reconciler=False)
    assert "If it landed, stop" in hint
    assert "replays the recorded result" not in hint

    # Wrapped failing command stores sanitized, capped error
    db_path = tmp_path / ".sidefx" / "journal.db"
    script = "import sys; sys.stderr.write('\\x1b[31m\\u202e' + 'A'*450 + '\\n'); sys.exit(28)"
    with Journal(db_path) as journal:
        res = wrap_command(
            [sys.executable, "-c", script],
            journal=journal,
            action_id="err-cap-1",
        )
    assert res.exit_code == 28
    with Journal.open_readonly(db_path) as journal:
        rec = journal.list_actions(limit=5)[0]
        assert rec.error is not None
        assert "\x1b" not in rec.error
        assert "\u202e" not in rec.error
        assert len(rec.error) <= 320


def test_status_text_strips_sidefx_meta_and_bidi() -> None:
    rec = ActionRecord(
        action_id="a1",
        session_id="s1",
        actor="cli",
        surface=Surface.CLI,
        tool_name="git_push",
        target="origin:refs/heads/main",
        payload_hash="0" * 64,
        marker="m1",
        status=ActionStatus.CONFIRMED,
        attempt=1,
        summary=format_summary_with_meta(
            "git push \u202eorigin", {"adapter": "git_push", "cwd": "/secret/path"}
        ),
    )
    rendered = _format_status_text(
        Path("/tmp/journal.db"),
        {s: (1 if s == ActionStatus.CONFIRMED else 0) for s in ActionStatus},
        [rec],
        journal_exists=True,
    )
    assert "sidefx_meta" not in rendered
    assert "/secret/path" not in rendered
    assert "\u202e" not in rendered
