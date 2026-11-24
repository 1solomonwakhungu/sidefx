"""Regression tests for the public-launch audit fixes."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sidefx import __version__
from sidefx.cli import main as cli_main
from sidefx.journal import ActionStatus, Journal
from sidefx.mcp_proxy import (
    ERR_OUTCOME_UNKNOWN,
    JSONRPC_INVALID_REQUEST,
    MAX_MESSAGE_BYTES,
    MCPProxy,
    _is_ambiguous_error_text,
)
from sidefx.reconcilers.git_push import GitPushReconciler, _ls_remote_ref, inspect_git_push
from sidefx.reconcilers.github import GitHubReconciler
from sidefx.session import _create_session_file_excl, ensure_persisted_session
from sidefx.wrapper import _inspect_curl_command, _redact_secrets_in_text, wrap_command


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


def test_bin_sidefx_launcher_matches_version_and_runs() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    launcher = repo_root / "bin" / "sidefx"
    text = launcher.read_text(encoding="utf-8")
    assert f"@v{__version__}" in text
    assert "@v0.1.0" not in text

    proc = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )
    assert proc.returncode == 0
    assert f"sidefx {__version__}" in proc.stdout


def test_git_push_redacts_secrets_in_custom_refspec_mismatch_and_ls_remote_stderr(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path)
    secret = "ghp_SUPERSECRETTOKEN1234567890"
    remote_url = f"https://x-access-token:{secret}@127.0.0.1:1/acme/missing.git"

    # 1. Custom remote.<url>.push path must redact remote & reason
    subprocess.run(
        ["git", "config", f"remote.{remote_url}.push", "refs/heads/*:refs/heads/*"],
        cwd=repo,
        check=True,
    )
    spec_custom = inspect_git_push(["git", "push", remote_url], cwd=repo, probe_remote=False)
    assert spec_custom is not None
    assert spec_custom.reconcilable is False
    assert secret not in (spec_custom.remote or "")
    assert secret not in (spec_custom.reason or "")

    # 2. push.default=simple upstream mismatch must redact remote
    subprocess.run(
        ["git", "config", "--unset-all", f"remote.{remote_url}.push"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "branch.main.remote", remote_url], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "branch.main.merge", "refs/heads/other"],
        cwd=repo,
        check=True,
    )
    spec_mismatch = inspect_git_push(["git", "push", remote_url], cwd=repo, probe_remote=False)
    assert spec_mismatch is not None
    assert spec_mismatch.reconcilable is False
    assert secret not in (spec_mismatch.remote or "")

    # 3. _ls_remote_ref failure stderr must redact inline credentials
    ok, sha, err = _ls_remote_ref(remote_url, "refs/heads/main", cwd=repo, timeout_seconds=5.0)
    assert ok is False
    assert sha is None
    assert err is not None
    assert secret not in err


def test_wrapper_redacts_secrets_in_stderr_and_supports_curl_url_flag(tmp_path: Path) -> None:
    secret_tok = "ghp_AbCdEfGhIjKlMnOpQrStUvWx123456"
    webhook_tok = "AbCdEfGhIjKlMnOpQrStUvWx1234"
    bearer_tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"

    line = (
        f"POST https://user:{secret_tok}@hooks.example.test/services/T1/B1/{webhook_tok}?key=1 "
        f"failed with Authorization: Bearer {bearer_tok}"
    )
    redacted = _redact_secrets_in_text(line)
    assert secret_tok not in redacted
    assert webhook_tok not in redacted
    assert bearer_tok not in redacted
    assert "Bearer <redacted>" in redacted
    assert "https://hooks.example.test/services/T1/B1/<redacted:" in redacted

    # Persisted journal error must also be redacted
    db_path = tmp_path / ".sidefx" / "journal.db"
    script = f"import sys; sys.stderr.write({line!r} + '\\n'); sys.exit(22)"
    with Journal(db_path) as journal:
        res = wrap_command(
            [sys.executable, "-c", script],
            journal=journal,
            action_id="redact-stderr-1",
        )
    assert res.exit_code == 22
    with Journal.open_readonly(db_path) as journal:
        rec = journal.get_action("agent", res.target, "redact-stderr-1")
        assert rec is not None and rec.error is not None
        assert secret_tok not in rec.error
        assert webhook_tok not in rec.error
        assert bearer_tok not in rec.error

    # curl --url=<url> detects GitHub REST POST and injects marker
    url, meta, exec_argv, is_gh = _inspect_curl_command(
        [
            "curl",
            "-X",
            "POST",
            "--url=https://api.github.com/repos/acme/app/issues",
            "-d",
            '{"title":"Bug","body":"Details"}',
        ],
        marker="abc123marker",
        configured_github_api_url="https://api.github.com",
    )
    assert url == "https://api.github.com/repos/acme/app/issues"
    assert is_gh is True
    assert meta["repo"] == "acme/app"
    assert meta["marker_injected"] is True
    assert "<!-- sidefx:id=abc123marker -->" in exec_argv[-1]


def test_wrap_cli_inserts_newline_before_diag_when_child_stderr_has_no_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "journal.db"
    script = "import sys; sys.stderr.write('partial err'); sys.exit(124)"
    rc = cli_main(
        [
            "wrap",
            "--db",
            str(db_path),
            "--action-id",
            "nl-test-1",
            "--",
            sys.executable,
            "-c",
            script,
        ]
    )
    assert rc == 124
    err = capsys.readouterr().err
    assert "partial err\nOUTCOME UNKNOWN" in err


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions check")
def test_journal_wal_shm_and_session_excl_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / ".sidefx" / "journal.db"
    with Journal(db_path) as journal:
        journal.begin_action(
            action_id="perm-1",
            session_id="s1",
            actor="agent",
            surface="cli",
            tool_name="deploy",
            target="prod",
            payload={"v": 1},
        )
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{db_path}{suffix}")
            if p.exists():
                assert stat.S_IMODE(p.stat().st_mode) == 0o600

    sess_dir = tmp_path / ".sidefx-excl"
    sess_dir.mkdir(mode=0o700)
    sess_file = sess_dir / "current-session"
    assert _create_session_file_excl(sess_file, "sess-test123") is True
    assert stat.S_IMODE(sess_file.stat().st_mode) == 0o600

    # ensure_persisted_session also keeps directory 0700 and file 0600
    db_path2 = tmp_path / ".sidefx-sess2" / "journal.db"
    ensure_persisted_session(db_path2)
    assert stat.S_IMODE(db_path2.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((db_path2.parent / "current-session").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions check")
def test_init_force_preserves_non_restrictive_config_permissions(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".sidefx.json"
    assert cli_main(["init", "--path", str(cfg_path)]) == 0
    initial_mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert cli_main(["init", "--path", str(cfg_path), "--force"]) == 0
    assert stat.S_IMODE(cfg_path.stat().st_mode) == initial_mode


def test_finite_timeout_validation_across_cli_and_apis(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    with Journal(db_path) as journal:
        for bad in (float("inf"), float("-inf"), float("nan"), 100_000.0):
            with pytest.raises(ValueError):
                wrap_command(["echo", "hi"], journal=journal, timeout_seconds=bad)
        for bad_age in (float("inf"), float("nan"), -1.0):
            with pytest.raises(ValueError):
                wrap_command(["echo", "hi"], journal=journal, min_retry_age_seconds=bad_age)

    for bad in (float("inf"), float("nan"), 100_000.0):
        with pytest.raises(ValueError):
            GitPushReconciler(timeout_seconds=bad)
        with pytest.raises(ValueError):
            GitHubReconciler(timeout_seconds=bad)

    for bad_argv in (
        ["wrap", "--timeout", "inf", "--", "echo", "hi"],
        ["wrap", "--min-retry-age", "inf", "--", "echo", "hi"],
        ["reconcile", "--timeout", "inf"],
        ["reconcile", "--stale-pending-after", "inf"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli_main(bad_argv)
        assert exc_info.value.code == 2


def test_mcp_proxy_rejects_unhashable_and_bool_jsonrpc_ids(tmp_path: Path) -> None:
    server_script = tmp_path / "echo_mcp.py"
    server_script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    if not line.strip(): continue\n"
        "    m = json.loads(line)\n"
        "    if 'id' in m:\n"
        "        resp = {'jsonrpc': '2.0', 'id': m['id'], 'result': {'ok': True}}\n"
        "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    client_in = io.BytesIO(
        b'{"jsonrpc":"2.0","id":[1,2],"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":{"a":1},"method":"initialize","params":{}}\n'
        b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":"ok-1","method":"tools/list"}\n'
    )
    client_out = io.BytesIO()
    err_buf = io.BytesIO()
    with Journal(tmp_path / "journal.db") as journal:
        proxy = MCPProxy(
            [sys.executable, str(server_script)],
            journal=journal,
            client_in=client_in,
            client_out=client_out,
            err_stream=err_buf,
        )
        assert proxy.run() == 0

    responses = [json.loads(line) for line in client_out.getvalue().decode("utf-8").splitlines()]
    assert len(responses) == 4
    for bad_resp in responses[:3]:
        assert bad_resp["id"] is None
        assert bad_resp["error"]["code"] == JSONRPC_INVALID_REQUEST
    assert responses[3]["id"] == "ok-1"
    assert responses[3]["result"] == {"ok": True}


def test_mcp_proxy_frame_limit_exact_boundary_and_outcome_unknown_data_sanitization(
    tmp_path: Path,
) -> None:
    with Journal(tmp_path / "journal.db") as journal:
        client_out = io.BytesIO()
        proxy = MCPProxy(
            [sys.executable, "-c", "pass"],
            journal=journal,
            client_in=io.BytesIO(),
            client_out=client_out,
        )

        # Frame of length MAX_MESSAGE_BYTES + 1 ending with '\n' MUST overflow
        oversized = (b"a" * MAX_MESSAGE_BYTES) + b"\n"
        assert len(oversized) == MAX_MESSAGE_BYTES + 1
        line, overflowed = proxy._read_framed_line(io.BytesIO(oversized + b'{"ok":true}\n'))
        assert overflowed is True
        assert line == b""

        # OUTCOME UNKNOWN error.data["detail"] must be sanitized just like error.message
        noisy = "timeout \x1b[31mred\x1b[0m \u202ereversed\nfake" + ("z" * 400)
        proxy._send_outcome_unknown(
            has_client_id=True,
            client_id="req-1",
            action_id="act-1",
            marker="m-1",
            tool_name="create_ticket",
            status=ActionStatus.UNKNOWN,
            detail=noisy,
        )
        resp = json.loads(client_out.getvalue().decode("utf-8").strip())
        assert resp["error"]["code"] == ERR_OUTCOME_UNKNOWN
        data_detail = resp["error"]["data"]["detail"]
        assert "\x1b" not in data_detail
        assert "\u202e" not in data_detail
        assert "\n" not in data_detail
        assert len(data_detail) <= 300


def test_mcp_proxy_ambiguous_error_text_recognizes_http_5xx_variants() -> None:
    for msg in (
        "HTTP Error 502: Bad Gateway",
        "HTTP 503",
        "Request failed with status code 504",
        "503 Service Temporarily Unavailable",
        "Connection closed by remote host",
        "Connection aborted while reading response",
    ):
        assert _is_ambiguous_error_text(msg) is True, f"expected ambiguous: {msg!r}"

    for msg in (
        "validation error: issue #502 not found",
        "400 Bad Request: missing title",
        "403 Forbidden: insufficient scope",
    ):
        assert _is_ambiguous_error_text(msg) is False, f"expected non-ambiguous: {msg!r}"
