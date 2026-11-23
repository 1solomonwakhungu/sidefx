"""End-to-end integration tests for `sidefx wrap` with mock GitHub server and real git repos."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from sidefx.cli import main
from sidefx.journal import ActionStatus, Journal, Surface
from sidefx.wrapper import (
    EXIT_NOT_FOUND,
    EXIT_OUTCOME_UNKNOWN,
    EXIT_PAYLOAD_MISMATCH,
    EXIT_TIMEOUT,
    _inspect_curl_command,
    wrap_command,
)


class _WriteTrackingGitHubServer:
    """Local mock GitHub server supporting both POST (create) and GET (reconcile)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.issues: list[dict[str, Any]] = []
        self.post_count = 0
        self.fail_gets_status: int | None = None
        self.fail_posts_status: int | None = None

    def start(self) -> tuple[ThreadingHTTPServer, str]:
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                parsed = urlsplit(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                owner, repo = parts[1], parts[2]
                with state.lock:
                    if state.fail_posts_status is not None:
                        status_code = state.fail_posts_status
                        self.send_response(status_code)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        err_obj = {
                            "message": "Validation Failed",
                            "documentation_url": (
                                "https://docs.github.com/rest/issues/issues#create-an-issue"
                            ),
                        }
                        self.wfile.write(json.dumps(err_obj).encode("utf-8"))
                        return
                    state.post_count += 1
                    num = len(state.issues) + 1
                    item = {
                        "id": num * 100,
                        "number": num,
                        "url": f"https://api.github.com/repos/{owner}/{repo}/issues/{num}",
                        "title": payload.get("title", ""),
                        "body": payload.get("body", ""),
                        "state": "open",
                        "user": {"login": "agent-bot"},
                        "html_url": f"https://github.com/{owner}/{repo}/issues/{num}",
                    }
                    state.issues.append(item)
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(item).encode("utf-8"))

            def do_GET(self) -> None:  # noqa: N802
                with state.lock:
                    fail_status = state.fail_gets_status
                    items = list(reversed(state.issues))
                if fail_status is not None:
                    self.send_response(fail_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"message":"server error"}')
                    return

                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                req_state = (query.get("state") or ["open"])[0]
                if req_state != "all":
                    items = [i for i in items if i["state"] == req_state]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(items).encode("utf-8"))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def gh_harness(tmp_path: Path) -> tuple[str, _WriteTrackingGitHubServer, Path]:
    """Start local GitHub mock server and create a fake `gh` CLI executable script."""
    tracker = _WriteTrackingGitHubServer()
    server, base_url = tracker.start()

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    gh_script = bin_dir / "gh"
    gh_script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import sys
            import time
            import urllib.request

            args = sys.argv[1:]
            api_url = os.environ["FAKE_GH_API_URL"]
            mode = os.environ.get("FAKE_GH_MODE", "normal")

            if args[:2] != ["issue", "create"]:
                print("unsupported fake gh args", args, file=sys.stderr)
                sys.exit(2)

            title = ""
            body = ""
            repo = "org/repo"
            i = 2
            while i < len(args):
                if args[i] in ("--title", "-t"):
                    title = args[i + 1]
                    i += 2
                elif args[i] in ("--body", "-b"):
                    body = args[i + 1]
                    i += 2
                elif args[i] in ("--repo", "-R"):
                    repo = args[i + 1]
                    i += 2
                elif args[i].startswith("-R"):
                    repo = args[i][2:]
                    i += 1
                else:
                    i += 1

            if mode == "timeout_before_write":
                time.sleep(5)
                sys.exit(1)

            req = urllib.request.Request(
                f"{{api_url}}/repos/{{repo}}/issues",
                data=json.dumps({{"title": title, "body": body}}).encode("utf-8"),
                headers={{"Content-Type": "application/json"}},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                created = json.loads(resp.read().decode("utf-8"))

            if mode == "timeout_after_write":
                time.sleep(5)
                sys.exit(1)

            if mode == "crash_after_write":
                print("connection reset by peer", file=sys.stderr)
                sys.exit(1)

            print(created["html_url"])
            """
        ),
        encoding="utf-8",
    )
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IXUSR)

    try:
        yield base_url, tracker, gh_script
    finally:
        server.shutdown()
        server.server_close()


class TestWrapGitHubEndToEnd:
    def test_normal_wrap_confirms_and_injects_marker_and_replays(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        env = os.environ.copy()
        env["FAKE_GH_API_URL"] = base_url
        env["FAKE_GH_MODE"] = "normal"

        cmd = [str(gh_script), "issue", "create", "-Rorg/repo", "--title", "Bug", "--body", "Hi"]

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-1",
                session_id="sess-1",
                env=env,
                github_api_url=base_url,
            )
            assert res1.exit_code == 0
            assert res1.executed is True
            assert res1.status == ActionStatus.CONFIRMED
            assert res1.result_ref == "https://github.com/org/repo/issues/1"
            assert tracker.post_count == 1
            assert f"<!-- sidefx:id={res1.marker} -->" in tracker.issues[0]["body"]

            res2 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-1",
                session_id="sess-1",
                env=env,
                github_api_url=base_url,
            )
            assert res2.exit_code == 0
            assert res2.executed is False
            assert res2.replayed is True
            assert res2.result_ref == res1.result_ref
            assert tracker.post_count == 1

            mutated = [
                str(gh_script),
                "issue",
                "create",
                "-Rorg/repo",
                "--title",
                "Different",
                "--body",
                "Hi",
            ]
            res3 = wrap_command(
                mutated,
                journal=journal,
                action_id="gh-1",
                session_id="sess-1",
                env=env,
                github_api_url=base_url,
            )
            assert res3.exit_code == EXIT_PAYLOAD_MISMATCH
            assert res3.executed is False
            assert tracker.post_count == 1

    def test_timeout_after_write_reconciles_with_zero_duplicate_writes(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        cmd = [
            str(gh_script),
            "issue",
            "create",
            "-R",
            "org/repo",
            "--title",
            "Lost",
            "--body",
            "X",
        ]

        env_timeout = os.environ.copy()
        env_timeout["FAKE_GH_API_URL"] = base_url
        env_timeout["FAKE_GH_MODE"] = "timeout_after_write"

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-timeout-post",
                timeout_seconds=0.5,
                env=env_timeout,
                github_api_url=base_url,
            )
            assert res1.exit_code == EXIT_TIMEOUT
            assert res1.status == ActionStatus.UNKNOWN
            assert "OUTCOME UNKNOWN" in (res1.error or "")
            assert tracker.post_count == 1

            res_blocked = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-timeout-post",
                env=env_timeout,
                github_api_url=base_url,
                auto_reconcile=False,
            )
            assert res_blocked.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res_blocked.executed is False
            assert tracker.post_count == 1

            env_normal = dict(env_timeout, FAKE_GH_MODE="normal")
            res2 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-timeout-post",
                env=env_normal,
                github_api_url=base_url,
            )
            assert res2.exit_code == 0
            assert res2.executed is False
            assert res2.reconciled is True
            assert res2.status == ActionStatus.RECONCILED
            assert res2.result_ref == "https://github.com/org/repo/issues/1"
            assert tracker.post_count == 1

    def test_crash_after_write_marks_unknown_and_reconciles_without_duplicate(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        cmd = [
            str(gh_script),
            "issue",
            "create",
            "-R",
            "org/repo",
            "--title",
            "Crash",
            "--body",
            "Y",
        ]

        env = os.environ.copy()
        env["FAKE_GH_API_URL"] = base_url
        env["FAKE_GH_MODE"] = "crash_after_write"

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd, journal=journal, action_id="gh-crash", env=env, github_api_url=base_url
            )
            assert res1.exit_code == 1
            assert res1.status == ActionStatus.UNKNOWN
            assert tracker.post_count == 1

            env_ok = dict(env, FAKE_GH_MODE="normal")
            res2 = wrap_command(
                cmd, journal=journal, action_id="gh-crash", env=env_ok, github_api_url=base_url
            )
            assert res2.exit_code == 0
            assert res2.executed is False
            assert res2.reconciled is True
            assert res2.result_ref == "https://github.com/org/repo/issues/1"
            assert tracker.post_count == 1

    def test_timeout_before_write_proves_not_found_and_safely_retries_once(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        cmd = [
            str(gh_script),
            "issue",
            "create",
            "-R",
            "org/repo",
            "--title",
            "Pre",
            "--body",
            "Z",
        ]

        env_pre = os.environ.copy()
        env_pre["FAKE_GH_API_URL"] = base_url
        env_pre["FAKE_GH_MODE"] = "timeout_before_write"

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-pre-timeout",
                timeout_seconds=0.3,
                env=env_pre,
                github_api_url=base_url,
            )
            assert res1.exit_code == EXIT_TIMEOUT
            assert res1.status == ActionStatus.UNKNOWN
            assert tracker.post_count == 0

            # Default min_retry_age_seconds (5s) blocks immediate retry right after timeout!
            env_ok = dict(env_pre, FAKE_GH_MODE="normal")
            res_cooldown = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-pre-timeout",
                env=env_ok,
                github_api_url=base_url,
            )
            assert res_cooldown.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res_cooldown.executed is False
            assert "cooldown" in (res_cooldown.error or "")
            assert tracker.post_count == 0

            # With min_retry_age_seconds=0 (or after cooldown), NOT_FOUND retries once!
            res2 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-pre-timeout",
                min_retry_age_seconds=0.0,
                env=env_ok,
                github_api_url=base_url,
            )
            assert res2.exit_code == 0
            assert res2.executed is True
            assert res2.status == ActionStatus.CONFIRMED
            assert res2.result_ref == "https://github.com/org/repo/issues/1"
            assert tracker.post_count == 1
            rec = journal.get_action("agent", "org/repo", "gh-pre-timeout")
            assert rec is not None and rec.attempt == 2

    def test_reconciler_http_500_blocks_blind_retry_with_zero_writes(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        cmd = [str(gh_script), "issue", "create", "-R", "org/repo", "--title", "500", "--body", "Q"]

        env_pre = os.environ.copy()
        env_pre["FAKE_GH_API_URL"] = base_url
        env_pre["FAKE_GH_MODE"] = "timeout_after_write"

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-500",
                timeout_seconds=0.5,
                env=env_pre,
                github_api_url=base_url,
            )
            assert res1.status == ActionStatus.UNKNOWN
            assert tracker.post_count == 1

            tracker.fail_gets_status = 500
            env_ok = dict(env_pre, FAKE_GH_MODE="normal")
            res2 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-500",
                env=env_ok,
                github_api_url=base_url,
            )
            assert res2.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res2.executed is False
            assert tracker.post_count == 1

    def test_live_pending_row_never_auto_retries_on_not_found(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        """Concurrent worker safety: PENDING row must NOT be marked failed/retried on NOT_FOUND."""
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        cmd = [
            str(gh_script),
            "issue",
            "create",
            "-R",
            "org/repo",
            "--title",
            "Live",
            "--body",
            "W",
        ]

        env = os.environ.copy()
        env["FAKE_GH_API_URL"] = base_url
        env["FAKE_GH_MODE"] = "normal"

        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="in-flight",
                session_id="s1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"argv": cmd, "target": "org/repo", "tool_name": "gh_issue_create"},
            )
            res_b = wrap_command(
                cmd,
                journal=journal,
                action_id="in-flight",
                session_id="s1",
                env=env,
                github_api_url=base_url,
            )
            assert res_b.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res_b.executed is False
            assert "still pending" in (res_b.error or "")
            assert tracker.post_count == 0
            rec = journal.get_action("agent", "org/repo", "in-flight")
            assert rec is not None and rec.status == ActionStatus.PENDING

    def test_other_gh_subcommand_nonzero_exit_marks_unknown(
        self, tmp_path: Path, gh_harness: tuple[str, _WriteTrackingGitHubServer, Path]
    ) -> None:
        """N12: `gh api` or `gh pr merge` exiting non-zero must mark unknown, not failed."""
        base_url, _, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        env = os.environ.copy()
        env["FAKE_GH_API_URL"] = base_url

        with Journal(db_path) as journal:
            res = wrap_command(
                [str(gh_script), "pr", "merge", "42"],
                journal=journal,
                action_id="gh-merge-1",
                env=env,
            )
            assert res.exit_code == 2
            assert res.status == ActionStatus.UNKNOWN

    def test_target_flag_never_authorizes_false_not_found_for_gh(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        """Round-2 B1: `--target other/repo` must be treated as inferred, never triggering retry."""
        base_url, tracker, gh_script = gh_harness
        db_path = tmp_path / "journal.db"
        # Fake `gh` posts to `org/repo` by default, while user passed `--target acme/other`!
        cmd = [str(gh_script), "issue", "create", "--title", "T", "--body", "B"]

        env_timeout = os.environ.copy()
        env_timeout["FAKE_GH_API_URL"] = base_url
        env_timeout["FAKE_GH_MODE"] = "timeout_after_write"

        with Journal(db_path) as journal:
            res1 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-target-mismatch",
                target="acme/other",
                timeout_seconds=0.5,
                env=env_timeout,
                github_api_url=base_url,
            )
            assert res1.exit_code == EXIT_TIMEOUT
            assert tracker.post_count == 1

            # Even with min_retry_age_seconds=0, repo came only from --target -> capped at UNKNOWN!
            env_ok = dict(env_timeout, FAKE_GH_MODE="normal")
            res2 = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-target-mismatch",
                target="acme/other",
                min_retry_age_seconds=0.0,
                env=env_ok,
                github_api_url=base_url,
            )
            assert res2.executed is False
            assert tracker.post_count == 1

        # Second check: empty server + --target without -R MUST NOT retry (stays UNKNOWN)
        tracker.issues.clear()
        tracker.post_count = 0
        env_pre = dict(env_timeout, FAKE_GH_MODE="timeout_before_write")
        with Journal(db_path) as journal:
            res_a = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-target-inferred-empty",
                target="acme/other",
                timeout_seconds=0.3,
                env=env_pre,
                github_api_url=base_url,
            )
            assert res_a.exit_code == EXIT_TIMEOUT
            env_ok = dict(env_timeout, FAKE_GH_MODE="normal")
            res_b = wrap_command(
                cmd,
                journal=journal,
                action_id="gh-target-inferred-empty",
                target="acme/other",
                min_retry_age_seconds=0.0,
                env=env_ok,
                github_api_url=base_url,
            )
            assert res_b.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res_b.executed is False
            assert tracker.post_count == 0


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


class TestWrapGitPushEndToEnd:
    def test_git_push_wrap_confirm_timeout_reconcile_and_mutation_reject(
        self, tmp_path: Path
    ) -> None:
        bare = tmp_path / "remote.git"
        bare.mkdir()
        _git(bare, "init", "--bare", "-b", "main")

        work = tmp_path / "work"
        work.mkdir()
        _git(work, "init", "-b", "main")
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "T")
        _git(work, "remote", "add", "origin", str(bare))

        (work / "a.txt").write_text("1\n", encoding="utf-8")
        _git(work, "add", "a.txt")
        _git(work, "commit", "-m", "c1")
        sha1 = _git(work, "rev-parse", "HEAD")

        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            res1 = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c1",
                cwd=work,
            )
            assert res1.exit_code == 0
            assert res1.status == ActionStatus.CONFIRMED
            assert res1.result_ref == f"origin refs/heads/main@{sha1[:12]}"

            (work / "a.txt").write_text("2\n", encoding="utf-8")
            _git(work, "add", "a.txt")
            _git(work, "commit", "-m", "c2")
            sha2 = _git(work, "rev-parse", "HEAD")

            hook_dir = bare / "hooks"
            pre_recv = hook_dir / "pre-receive"
            pre_recv.write_text("#!/bin/sh\nsleep 5\nexit 1\n", encoding="utf-8")
            pre_recv.chmod(0o755)

            res_pre_timeout = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c2",
                cwd=work,
                timeout_seconds=0.4,
            )
            assert res_pre_timeout.exit_code == EXIT_TIMEOUT
            assert res_pre_timeout.status == ActionStatus.UNKNOWN

            pre_recv.unlink()
            res_retry_ok = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c2",
                cwd=work,
                min_retry_age_seconds=0.0,
            )
            assert res_retry_ok.exit_code == 0
            assert res_retry_ok.executed is True
            assert res_retry_ok.status == ActionStatus.CONFIRMED
            assert res_retry_ok.result_ref == f"origin refs/heads/main@{sha2[:12]}"

            (work / "a.txt").write_text("3\n", encoding="utf-8")
            _git(work, "add", "a.txt")
            _git(work, "commit", "-m", "c3")
            sha3 = _git(work, "rev-parse", "HEAD")

            post_recv = hook_dir / "post-receive"
            post_recv.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            post_recv.chmod(0o755)

            res_post_timeout = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c3",
                cwd=work,
                timeout_seconds=0.6,
            )
            assert res_post_timeout.exit_code == EXIT_TIMEOUT
            assert res_post_timeout.status == ActionStatus.UNKNOWN

            post_recv.unlink()
            res_reconciled = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c3",
                cwd=work,
            )
            assert res_reconciled.exit_code == 0
            assert res_reconciled.executed is False
            assert res_reconciled.reconciled is True
            assert res_reconciled.status == ActionStatus.RECONCILED
            assert res_reconciled.result_ref == f"origin refs/heads/main@{sha3[:12]}"

            (work / "a.txt").write_text("4\n", encoding="utf-8")
            _git(work, "add", "a.txt")
            _git(work, "commit", "-m", "c4")
            res_mismatch = wrap_command(
                ["git", "push", "origin", "main"],
                journal=journal,
                action_id="push-c3",
                cwd=work,
            )
            assert res_mismatch.exit_code == EXIT_PAYLOAD_MISMATCH
            assert res_mismatch.executed is False


class TestWrapCliAndEdgeCases:
    def test_wrap_cli_passthrough_flags_and_missing_binary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "journal.db"

        rc = main(
            [
                "--db",
                str(db_path),
                "wrap",
                "--action-id",
                "echo-1",
                "--target",
                "stdout",
                "--",
                sys.executable,
                "-c",
                "import sys; print(' '.join(sys.argv[1:]))",
                "--timeout",
                "999",
                "--target",
                "inner",
            ]
        )
        assert rc == 0
        assert "--timeout 999 --target inner" in capsys.readouterr().out

        rc_nf = main(
            [
                "--db",
                str(db_path),
                "wrap",
                "--action-id",
                "no-bin",
                "--",
                "definitely_nonexistent_sidefx_binary_xyz",
            ]
        )
        assert rc_nf == EXIT_NOT_FOUND
        _ = capsys.readouterr()
        with Journal.open_readonly(db_path) as j:
            rec = j.get_action("agent", "definitely_nonexistent_sidefx_binary_xyz", "no-bin")
            assert rec is not None and rec.status == ActionStatus.FAILED

    def test_non_utf8_output_does_not_crash_wrap(self, tmp_path: Path) -> None:
        """B6: Binary / non-UTF-8 stdout must confirm cleanly without UnicodeDecodeError."""
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            res = wrap_command(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfe\\n')"],
                journal=journal,
                action_id="bin-out",
            )
            assert res.exit_code == 0
            assert res.status == ActionStatus.CONFIRMED
            assert res.stdout_bytes == b"ok\xff\xfe\n"

    def test_timeout_kills_sigterm_ignoring_grandchild_process_group(self, tmp_path: Path) -> None:
        """N1: Even if parent exits on SIGTERM, SIGTERM-ignoring grandchild must be SIGKILLed."""
        db_path = tmp_path / "journal.db"
        marker_file = tmp_path / "grandchild_wrote.txt"
        pid_file = tmp_path / "grandchild.pid"
        grandchild_code = (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(1.5); "
            f"open({str(marker_file)!r}, 'w').write('leaked')"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "time.sleep(30)"
        )
        with Journal(db_path) as journal:
            res = wrap_command(
                [sys.executable, "-c", parent_code],
                journal=journal,
                action_id="pg-sigkill",
                timeout_seconds=0.4,
            )
            assert res.exit_code == EXIT_TIMEOUT
            assert res.status == ActionStatus.UNKNOWN

        time.sleep(0.2)
        if pid_file.exists():
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            with pytest.raises(ProcessLookupError):
                os.kill(grandchild_pid, 0)
        assert not marker_file.exists()

    def test_sigterm_to_wrap_kills_child_and_marks_unknown(self, tmp_path: Path) -> None:
        """B1: SIGTERM to `sidefx wrap` must kill child group and mark row `unknown`."""
        db_path = tmp_path / "journal.db"
        side_effect_file = tmp_path / "should_not_exist.txt"
        started_file = tmp_path / "started.txt"
        child_code = (
            f"open({str(started_file)!r}, 'w').write('go'); "
            "import time; time.sleep(3); "
            f"open({str(side_effect_file)!r}, 'w').write('ran')"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sidefx",
                "--db",
                str(db_path),
                "wrap",
                "--action-id",
                "term-1",
                "--target",
                "t",
                "--",
                sys.executable,
                "-c",
                child_code,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while not started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started_file.exists()

        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=5.0)
        assert proc.returncode == 128 + signal.SIGTERM
        assert not side_effect_file.exists()

        with Journal.open_readonly(db_path) as j:
            rec = j.get_action("agent", "t", "term-1")
            assert rec is not None and rec.status == ActionStatus.UNKNOWN

    def test_concurrent_external_transition_does_not_crash_wrap_or_drop_stdout(
        self, tmp_path: Path
    ) -> None:
        """B1: External mid-exec state mutation must preserve stdout and avoid crashing."""
        db_path = tmp_path / "journal.db"
        script_fail = textwrap.dedent(
            f"""\
            from sidefx.journal import Journal
            with Journal({str(db_path)!r}) as j:
                j.mark_failed("agent", "t", "race-wrap", attempt=1, error="external")
            print("important-stdout")
            """
        )
        script_reconcile = textwrap.dedent(
            f"""\
            from sidefx.journal import Journal
            with Journal({str(db_path)!r}) as j:
                j.mark_reconciled("agent", "t", "race-reconcile", attempt=1, result_ref="https://ok")
            print("reconciled-stdout")
            """
        )
        with Journal(db_path) as journal:
            res = wrap_command(
                [sys.executable, "-c", script_fail],
                journal=journal,
                action_id="race-wrap",
                target="t",
            )
            assert res.exit_code == 1
            assert "important-stdout" in res.stdout
            assert "concurrently" in (res.error or "")

            # If concurrently reconciled while command succeeded, exit code is 0!
            res_ok = wrap_command(
                [sys.executable, "-c", script_reconcile],
                journal=journal,
                action_id="race-reconcile",
                target="t",
            )
            assert res_ok.exit_code == 0
            assert res_ok.status == ActionStatus.RECONCILED
            assert "reconciled-stdout" in res_ok.stdout

    def test_curl_github_detection_rejects_non_matching_paths_hosts_and_methods(self) -> None:
        """B3: Only exact supported endpoints on the configured GitHub API origin match."""
        gh_api = "https://api.github.com"

        for bad_cmd in (
            ["curl", "-X", "POST", f"{gh_api}/repos/o/r/pulls/7/comments", "-d", '{"body":"x"}'],
            ["curl", "-X", "POST", f"{gh_api}/repos/o/r/pulls/7/reviews", "-d", '{"body":"x"}'],
            ["curl", "-X", "POST", f"{gh_api}/repos/o/r/issues/5/labels", "-d", '{"labels":[]}'],
            ["curl", "-X", "POST", "https://gitea.example.com/api/v1/repos/o/r/issues", "-d", "{}"],
            ["curl", "-X", "POST", "https://ghes.corp/api/v3/repos/o/r/issues", "-d", "{}"],
            ["curl", "-XPATCH", f"{gh_api}/repos/o/r/issues", "-d", '{"body":"x"}'],
            ["curl", "-G", f"{gh_api}/repos/o/r/issues", "-d", '{"body":"x"}'],
        ):
            _, meta, exec_argv, is_gh = _inspect_curl_command(
                bad_cmd, marker="m1", configured_github_api_url=gh_api
            )
            assert is_gh is False, f"Should NOT match GitHub POST: {bad_cmd}"
            assert meta == {}
            assert exec_argv == bad_cmd

        # Attached -XPOST on exact endpoint DOES match!
        _, meta_ok, exec_ok, is_gh_ok = _inspect_curl_command(
            ["curl", "-XPOST", f"{gh_api}/repos/o/r/issues", "-d", '{"title":"T","body":"B"}'],
            marker="m1",
            configured_github_api_url=gh_api,
        )
        assert is_gh_ok is True
        assert meta_ok["kind"] == "issue"
        assert meta_ok["marker_injected"] is True
        assert "<!-- sidefx:id=m1 -->" in exec_ok[-1]

    def test_curl_github_422_is_not_confirmed_and_201_extracts_html_url(
        self,
        tmp_path: Path,
        gh_harness: tuple[str, _WriteTrackingGitHubServer, Path],
    ) -> None:
        """B8: curl exit 0 on HTTP 422 marks UNKNOWN; HTTP 201 extracts html_url (not API url)."""
        base_url, tracker, _ = gh_harness
        db_path = tmp_path / "journal.db"

        fake_curl = tmp_path / "curl"
        fake_curl.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import sys, urllib.error, urllib.request
                args = sys.argv[1:]
                url = [a for a in args if a.startswith("http")][0]
                data = args[args.index("-d") + 1]
                req = urllib.request.Request(
                    url,
                    data=data.encode("utf-8"),
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        print(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    # Mimic curl without -f: prints error body and exits 0!
                    print(exc.read().decode("utf-8"))
                """
            ),
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

        with Journal(db_path) as journal:
            # 1. HTTP 422 with curl exit 0 MUST be marked UNKNOWN, NOT CONFIRMED!
            tracker.fail_posts_status = 422
            res_err = wrap_command(
                [
                    str(fake_curl),
                    "-X",
                    "POST",
                    f"{base_url}/repos/org/repo/issues",
                    "-d",
                    '{"title":"Bad","body":"Hello"}',
                ],
                journal=journal,
                action_id="curl-422",
                github_api_url=base_url,
            )
            assert res_err.exit_code == EXIT_OUTCOME_UNKNOWN
            assert res_err.status == ActionStatus.UNKNOWN
            assert tracker.post_count == 0

            # 2. HTTP 201 extracts html_url (https://github.com/...) rather than API `url`!
            tracker.fail_posts_status = None
            res_ok = wrap_command(
                [
                    str(fake_curl),
                    "-X",
                    "POST",
                    f"{base_url}/repos/org/repo/issues",
                    "-d",
                    '{"title":"FromCurl","body":"Hello"}',
                ],
                journal=journal,
                action_id="curl-201",
                github_api_url=base_url,
            )
            assert res_ok.exit_code == 0
            assert res_ok.status == ActionStatus.CONFIRMED
            assert res_ok.result_ref == "https://github.com/org/repo/issues/1"
            assert tracker.post_count == 1
            assert f"<!-- sidefx:id={res_ok.marker} -->" in tracker.issues[0]["body"]

    def test_secret_redaction_in_target_and_summary(self, tmp_path: Path) -> None:
        """N6: Userinfo, query strings, and generic CLI secret args are not stored in journal."""
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            res = wrap_command(
                [sys.executable, "-c", "print('ok')", "--token", "super_secret_value"],
                journal=journal,
                action_id="sec-1",
            )
            assert res.exit_code == 0
            rec = journal.get_action("agent", res.target, "sec-1")
            assert rec is not None
            assert "super_secret_value" not in (rec.summary or "")
