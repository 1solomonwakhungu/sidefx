"""Automated chaos fault-injection evaluation harness for sidefx.

Exercises real local HTTP mock GitHub servers, real local bare git repositories with
fault-injecting hooks, real subprocess stdio MCP servers, and multi-process CLI races
across all 6 critical side-effect failure windows using Python stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sidefx.config import (
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    default_config_dict,
    load_config,
)
from sidefx.journal import ActionRecord, ActionStatus, Journal
from sidefx.mcp_proxy import (
    ERR_OUTCOME_UNKNOWN,
    ERR_PAYLOAD_MISMATCH,
    ERR_PREFLIGHT_DENIED,
    OUTCOME_UNKNOWN_BANNER,
    MCPProxy,
    ReconcileHookResult,
    ReconcileState,
)
from sidefx.wrapper import (
    EXIT_OUTCOME_UNKNOWN,
    EXIT_PAYLOAD_MISMATCH,
    EXIT_TIMEOUT,
    wrap_command,
)

# ---------------------------------------------------------------------------
# Public structured result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Structured result for one chaos fault-injection scenario."""

    name: str
    title: str
    passed: bool
    duration_ms: float
    duplicate_remote_writes: int
    expected_remote_writes: int
    actual_remote_writes: int
    receipt_accuracy_pct: float | None = None
    details: str = ""
    error: str | None = None
    subchecks: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize scenario result to a JSON-compatible dictionary."""
        data: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 2),
            "duplicate_remote_writes": self.duplicate_remote_writes,
            "expected_remote_writes": self.expected_remote_writes,
            "actual_remote_writes": self.actual_remote_writes,
            "details": self.details,
            "subchecks": list(self.subchecks),
        }
        if self.receipt_accuracy_pct is not None:
            data["receipt_accuracy_pct"] = round(self.receipt_accuracy_pct, 2)
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(frozen=True, slots=True)
class ChaosSuiteReport:
    """Aggregate report returned by ``run_chaos_suite``."""

    scenarios: tuple[ScenarioResult, ...]
    total_duration_ms: float

    @property
    def total_scenarios(self) -> int:
        return len(self.scenarios)

    @property
    def passed_scenarios(self) -> int:
        return sum(1 for s in self.scenarios if s.passed)

    @property
    def failed_scenarios(self) -> int:
        return self.total_scenarios - self.passed_scenarios

    @property
    def duplicate_remote_writes(self) -> int:
        return sum(s.duplicate_remote_writes for s in self.scenarios)

    @property
    def total_expected_remote_writes(self) -> int:
        return sum(s.expected_remote_writes for s in self.scenarios)

    @property
    def total_actual_remote_writes(self) -> int:
        return sum(s.actual_remote_writes for s in self.scenarios)

    @property
    def receipt_accuracy_pct(self) -> float | None:
        scores = [
            s.receipt_accuracy_pct for s in self.scenarios if s.receipt_accuracy_pct is not None
        ]
        if not scores:
            return None
        return sum(scores) / len(scores)

    @property
    def passed(self) -> bool:
        if self.total_scenarios == 0:
            return False
        if self.passed_scenarios != self.total_scenarios:
            return False
        if self.duplicate_remote_writes != 0:
            return False
        accuracy = self.receipt_accuracy_pct
        return accuracy is None or accuracy >= 100.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize aggregate suite report to a JSON-compatible dictionary."""
        accuracy = self.receipt_accuracy_pct
        return {
            "passed": self.passed,
            "total_scenarios": self.total_scenarios,
            "passed_scenarios": self.passed_scenarios,
            "failed_scenarios": self.failed_scenarios,
            "duplicate_remote_writes": self.duplicate_remote_writes,
            "expected_remote_writes": self.total_expected_remote_writes,
            "actual_remote_writes": self.total_actual_remote_writes,
            "receipt_accuracy_pct": round(accuracy, 2) if accuracy is not None else None,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "scenarios": [s.to_dict() for s in self.scenarios],
        }


# ---------------------------------------------------------------------------
# Local mock infrastructure (GitHub HTTP server, bare Git repo, stdio MCP)
# ---------------------------------------------------------------------------

_FAKE_GH_IMPL = textwrap.dedent(
    """\
    import json
    import os
    import sys
    import time
    import urllib.request

    args = sys.argv[1:]
    api_url = os.environ["FAKE_GH_API_URL"]
    mode = os.environ.get("FAKE_GH_MODE", "normal")
    hold_ms = int(os.environ.get("FAKE_GH_HOLD_MS", "0"))

    if args[:2] != ["issue", "create"]:
        print(f"unsupported fake gh args: {args}", file=sys.stderr)
        sys.exit(2)

    title = ""
    body = ""
    repo = "acme/app"
    idx = 2
    while idx < len(args):
        tok = args[idx]
        if tok in ("--title", "-t") and idx + 1 < len(args):
            title = args[idx + 1]
            idx += 2
        elif tok.startswith("--title="):
            title = tok.split("=", 1)[1]
            idx += 1
        elif tok in ("--body", "-b") and idx + 1 < len(args):
            body = args[idx + 1]
            idx += 2
        elif tok.startswith("--body="):
            body = tok.split("=", 1)[1]
            idx += 1
        elif tok in ("--repo", "-R") and idx + 1 < len(args):
            repo = args[idx + 1]
            idx += 2
        elif tok.startswith("--repo="):
            repo = tok.split("=", 1)[1]
            idx += 1
        elif tok.startswith("-R") and len(tok) > 2:
            repo = tok[2:]
            idx += 1
        else:
            idx += 1

    if mode == "pre_commit_crash":
        print("network unreachable before POST", file=sys.stderr)
        sys.exit(1)

    if mode == "pre_commit_timeout":
        time.sleep(10.0)
        sys.exit(1)

    if hold_ms > 0:
        time.sleep(hold_ms / 1000.0)

    req = urllib.request.Request(
        f"{api_url}/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        created = json.loads(resp.read().decode("utf-8"))

    if mode == "post_commit_timeout":
        time.sleep(10.0)
        sys.exit(1)

    if mode == "post_commit_crash":
        print("connection reset by peer after commit", file=sys.stderr)
        sys.exit(1)

    print(created["html_url"])
    """
)


_MOCK_MCP_SERVER_IMPL = textwrap.dedent(
    """\
    import json
    import os
    import sys
    import time
    from pathlib import Path

    ledger_path = Path(os.environ["CHAOS_MCP_LEDGER"])
    mode_file_raw = os.environ.get("CHAOS_MCP_MODE_FILE", "")
    mode_file = Path(mode_file_raw) if mode_file_raw else None


    def current_mode() -> str:
        if mode_file is not None and mode_file.exists():
            val = mode_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        return os.environ.get("CHAOS_MCP_MODE", "normal").strip() or "normal"


    def next_seq() -> int:
        if not ledger_path.exists():
            return 1
        lines = [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return len(lines) + 1


    def append_ledger(tool: str, arguments: dict) -> dict:
        seq = next_seq()
        url = f"https://tickets.sidefx.test/items/{seq}"
        entry = {
            "seq": seq,
            "tool": tool,
            "arguments": arguments,
            "url": url,
        }
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry


    def send(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\\n")
        sys.stdout.flush()


    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        req_id = msg.get("id")
        has_id = "id" in msg

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "chaos-mock-mcp"},
                    },
                }
            )
            continue

        if method == "tools/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {"name": "get_item"},
                            {"name": "create_ticket"},
                            {"name": "refund_payment"},
                            {"name": "push_branch"},
                            {"name": "send_email"},
                        ]
                    },
                }
            )
            continue

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            arguments = params.get("arguments") or {}

            if name == "get_item":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"item:{arguments.get('id')}"}],
                            "isError": False,
                        },
                    }
                )
                continue

            mode = current_mode()
            if mode == "pre_commit_crash":
                os._exit(1)

            if mode == "validation_error":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "validation error: invalid ticket priority",
                                }
                            ],
                            "isError": True,
                        },
                    }
                )
                continue

            entry = append_ledger(name, arguments)

            if mode == "post_commit_crash":
                os._exit(1)

            if mode == "post_commit_timeout":
                time.sleep(2.0)

            if has_id:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Created {name} #{entry['seq']}: {entry['url']}",
                                }
                            ],
                            "structuredContent": {
                                "seq": entry["seq"],
                                "url": entry["url"],
                                "tool": name,
                            },
                            "isError": False,
                        },
                    }
                )
    """
)


class _ChaosGitHubServer:
    """Thread-safe local HTTP mock GitHub server tracking every remote issue write."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.issues: list[dict[str, Any]] = []
        self.post_count = 0
        self.get_count = 0
        self.fail_gets_status: int | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str = ""

    def __enter__(self) -> _ChaosGitHubServer:
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
                owner = parts[1] if len(parts) > 2 else "acme"
                repo = parts[2] if len(parts) > 2 else "app"
                with state.lock:
                    state.post_count += 1
                    num = len(state.issues) + 1
                    item = {
                        "id": num * 1000,
                        "number": num,
                        "url": f"{state.base_url}/repos/{owner}/{repo}/issues/{num}",
                        "title": payload.get("title", ""),
                        "body": payload.get("body", ""),
                        "state": "open",
                        "user": {"login": "sidefx-bot"},
                        "html_url": f"https://github.com/{owner}/{repo}/issues/{num}",
                    }
                    state.issues.append(item)
                body_bytes = json.dumps(item).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

            def do_GET(self) -> None:  # noqa: N802
                with state.lock:
                    state.get_count += 1
                    fail_status = state.fail_gets_status
                    items = list(reversed(state.issues))
                if fail_status is not None:
                    err_bytes = b'{"message":"service unavailable"}'
                    self.send_response(fail_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err_bytes)))
                    self.end_headers()
                    self.wfile.write(err_bytes)
                    return

                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                req_state = (query.get("state") or ["open"])[0]
                if req_state != "all":
                    items = [i for i in items if i["state"] == req_state]
                out_bytes = json.dumps(items).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self.wfile.write(out_bytes)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sidefx-chaos-github",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _create_fake_gh(root: Path) -> Path:
    """Create a portable executable `gh` shim immune to Linux shebang length limits."""
    bin_dir = root / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    impl_py = bin_dir / "_fake_gh_impl.py"
    impl_py.write_text(_FAKE_GH_IMPL, encoding="utf-8")

    gh_shim = bin_dir / "gh"
    gh_shim.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(impl_py))} "$@"\n',
        encoding="utf-8",
    )
    gh_shim.chmod(gh_shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return gh_shim


def _create_mock_mcp_server(root: Path) -> tuple[Path, Path, Path]:
    """Create mock stdio MCP server script, ground-truth ledger path, and mode file path."""
    mcp_dir = root / "mock_mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    script_path = mcp_dir / "server.py"
    script_path.write_text(_MOCK_MCP_SERVER_IMPL, encoding="utf-8")
    ledger_path = mcp_dir / "mcp_writes.jsonl"
    mode_file = mcp_dir / "mode.txt"
    return script_path, ledger_path, mode_file


def _read_mcp_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            entries.append(json.loads(stripped))
    return entries


_AMBIENT_ENV_KEYS_TO_STRIP = (
    "GH_HOST",
    "GH_REPO",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
_AMBIENT_ENV_PREFIXES_TO_STRIP = ("GIT_", "SIDEFX_")
_HERMETIC_ENV_OVERRIDES = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}
_HERMETIC_ENV_LOCK = threading.RLock()


def _isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a hermetic subprocess env stripped of ambient proxy/sidefx/gh/git overrides."""
    env = os.environ.copy()
    for key in _AMBIENT_ENV_KEYS_TO_STRIP:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(_AMBIENT_ENV_PREFIXES_TO_STRIP):
            env.pop(key, None)
    env.update(_HERMETIC_ENV_OVERRIDES)
    if extra:
        env.update(extra)
    return env


@contextlib.contextmanager
def _hermetic_chaos_env() -> Iterator[None]:
    """Temporarily apply hermetic proxy/git/sidefx env to ``os.environ`` for in-process callers."""
    with _HERMETIC_ENV_LOCK:
        saved = os.environ.copy()
        try:
            for key in _AMBIENT_ENV_KEYS_TO_STRIP:
                os.environ.pop(key, None)
            for key in list(os.environ):
                if key.startswith(_AMBIENT_ENV_PREFIXES_TO_STRIP):
                    os.environ.pop(key, None)
            os.environ.update(_HERMETIC_ENV_OVERRIDES)
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)


def _write_default_config(root: Path) -> Path:
    """Write an isolated default `.sidefx.json` so child CLIs never discover ancestor configs."""
    cfg_path = root / "isolated_sidefx.json"
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(default_config_dict(), indent=2), encoding="utf-8")
    return cfg_path


def _git_env() -> dict[str, str]:
    return _isolated_env()


def _run_git(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_git_env(),
        timeout=15.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


@dataclass(slots=True)
class _ChaosGitRepo:
    bare_dir: Path
    work_dir: Path
    push_log: Path
    push_attempts_log: Path
    block_pre_flag: Path
    slow_post_flag: Path

    def create_branch_commit(self, branch: str, filename: str, content: str) -> str:
        _run_git(["checkout", "-B", branch, "main"], cwd=self.work_dir)
        (self.work_dir / filename).write_text(content, encoding="utf-8")
        _run_git(["add", filename], cwd=self.work_dir)
        _run_git(["commit", "-m", f"commit on {branch}"], cwd=self.work_dir)
        return _run_git(["rev-parse", "HEAD"], cwd=self.work_dir).stdout.strip().lower()

    def push_events_for_ref(self, ref_name: str) -> list[tuple[str, str, str]]:
        if not self.push_log.exists():
            return []
        events: list[tuple[str, str, str]] = []
        for line in self.push_log.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 3 and parts[2] == ref_name:
                events.append((parts[0], parts[1], parts[2]))
        return events

    def push_attempt_count(self) -> int:
        if not self.push_attempts_log.exists():
            return 0
        return sum(
            1
            for ln in self.push_attempts_log.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )

    def remote_ref_sha(self, ref_name: str) -> str | None:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", ref_name],
            cwd=str(self.bare_dir),
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip().lower() or None


def _create_chaos_git_repo(root: Path) -> _ChaosGitRepo:
    """Create a real local bare remote + working clone with fault-injecting hooks."""
    bare_dir = root / "remote.git"
    work_dir = root / "work"
    bare_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    push_log = root / "push_events.log"
    push_attempts_log = root / "push_attempts.log"
    block_pre_flag = root / "block_pre_receive.flag"
    slow_post_flag = root / "slow_post_receive.flag"

    # Always specify `-b main` and local identity/signing config for Ubuntu CI portability.
    _run_git(["init", "--bare", "-b", "main", str(bare_dir)], cwd=root)
    _run_git(["init", "-b", "main", str(work_dir)], cwd=root)
    _run_git(["config", "user.name", "sidefx Chaos"], cwd=work_dir)
    _run_git(["config", "user.email", "chaos@sidefx.test"], cwd=work_dir)
    _run_git(["config", "commit.gpgsign", "false"], cwd=work_dir)
    _run_git(["remote", "add", "origin", str(bare_dir)], cwd=work_dir)

    (work_dir / "README.txt").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "README.txt"], cwd=work_dir)
    _run_git(["commit", "-m", "initial commit"], cwd=work_dir)
    _run_git(["push", "-u", "origin", "main"], cwd=work_dir)

    # Track every real `git push` contacting `receive-pack` (even no-op re-pushes!),
    # while leaving read-only `git ls-remote` (`upload-pack`) untouched.
    receive_pack_wrapper = root / "sidefx-receive-pack"
    receive_pack_wrapper.write_text(
        "#!/bin/sh\n"
        f"printf 'attempt\\n' >> {shlex.quote(str(push_attempts_log))}\n"
        'exec git receive-pack "$@"\n',
        encoding="utf-8",
    )
    receive_pack_wrapper.chmod(0o755)
    _run_git(
        ["config", "remote.origin.receivepack", shlex.quote(str(receive_pack_wrapper))],
        cwd=work_dir,
    )

    hooks_dir = bare_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    pre_hook = hooks_dir / "pre-receive"
    pre_hook.write_text(
        f"#!/bin/sh\nif [ -f {shlex.quote(str(block_pre_flag))} ]; then\n"
        f"  sleep 10\n  exit 1\nfi\n",
        encoding="utf-8",
    )
    pre_hook.chmod(0o755)

    post_hook = hooks_dir / "post-receive"
    post_hook.write_text(
        "#!/bin/sh\n"
        "while read oldrev newrev refname; do\n"
        f'  printf \'%s %s %s\\n\' "$oldrev" "$newrev" "$refname" >> '
        f"{shlex.quote(str(push_log))}\n"
        "done\n"
        f"if [ -f {shlex.quote(str(slow_post_flag))} ]; then\n"
        "  sleep 10\n"
        "fi\n",
        encoding="utf-8",
    )
    post_hook.chmod(0o755)

    return _ChaosGitRepo(
        bare_dir=bare_dir,
        work_dir=work_dir,
        push_log=push_log,
        push_attempts_log=push_attempts_log,
        block_pre_flag=block_pre_flag,
        slow_post_flag=slow_post_flag,
    )


class _MCPSubprocessClient:
    """Context manager driving the real `sidefx mcp-proxy` CLI subprocess over stdio."""

    def __init__(
        self,
        *,
        db_path: Path,
        server_script: Path,
        ledger_path: Path,
        mode_file: Path,
        session_id: str,
        cwd: Path,
        target: str = "mcp:chaos",
        config_path: Path | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._db_path = db_path
        self._server_script = server_script
        self._ledger_path = ledger_path
        self._mode_file = mode_file
        self._session_id = session_id
        self._cwd = cwd
        self._target = target
        self._config_path = config_path
        self._timeout_seconds = timeout_seconds
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_chunks: list[bytes] = []
        self._stderr_thread: threading.Thread | None = None
        self._active_readers: list[threading.Thread] = []

    def __enter__(self) -> _MCPSubprocessClient:
        cmd = [
            sys.executable,
            "-m",
            "sidefx",
            "mcp-proxy",
            "--db",
            str(self._db_path),
            "--session-id",
            self._session_id,
            "--target",
            self._target,
            "--timeout",
            str(self._timeout_seconds),
        ]
        effective_cfg = (
            self._config_path if self._config_path is not None else _write_default_config(self._cwd)
        )
        cmd.extend(["--config", str(effective_cfg)])
        cmd.extend(["--", sys.executable, str(self._server_script)])

        env = _isolated_env(
            {
                "CHAOS_MCP_LEDGER": str(self._ledger_path),
                "CHAOS_MCP_MODE_FILE": str(self._mode_file),
            }
        )

        popen_extra: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self._cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **popen_extra,
        )
        assert self._proc.stderr is not None
        self._stderr_chunks = []

        def _drain_stderr(stream: Any) -> None:
            with contextlib.suppress(OSError, ValueError):
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    self._stderr_chunks.append(chunk)

        self._stderr_thread = threading.Thread(
            target=_drain_stderr,
            args=(self._proc.stderr,),
            name="sidefx-chaos-mcp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        return self

    def request(self, payload: dict[str, Any], *, timeout: float = 8.0) -> dict[str, Any]:
        assert (
            self._proc is not None
            and self._proc.stdin is not None
            and self._proc.stdout is not None
        )
        line = (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
            "ascii"
        )
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        result_holder: list[bytes] = []
        stdout_stream = self._proc.stdout

        def _read_line() -> None:
            with contextlib.suppress(OSError, ValueError):
                result_holder.append(stdout_stream.readline())

        reader = threading.Thread(target=_read_line, daemon=True)
        self._active_readers.append(reader)
        reader.start()
        reader.join(timeout=timeout)
        if reader.is_alive() or not result_holder or not result_holder[0]:
            stderr_tail = b"".join(self._stderr_chunks).decode("utf-8", errors="replace").strip()
            extra = f" (stderr: {stderr_tail})" if stderr_tail else ""
            raise TimeoutError(
                f"Timed out waiting for MCP proxy reply to {payload.get('id')!r}{extra}"
            )
        return json.loads(result_holder[0].decode("utf-8"))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._proc is None:
            return
        if self._proc.stdin is not None:
            with contextlib.suppress(OSError):
                self._proc.stdin.close()
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    pgid = os.getpgid(self._proc.pid)
                except OSError:
                    pgid = None
                if pgid is not None:
                    with contextlib.suppress(OSError):
                        os.killpg(pgid, signal.SIGTERM)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._proc.wait(timeout=2.0)
                    # Always SIGKILL the entire process group so SIGTERM-ignoring grandchildren
                    # cannot stay alive holding stdout/stderr pipes open.
                    with contextlib.suppress(OSError):
                        os.killpg(pgid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._proc.wait(timeout=2.0)
                else:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)
        # Child process group has exited, so stdout/stderr hit EOF; join readers BEFORE closing.
        still_reading_stdout = False
        for reader in self._active_readers:
            reader.join(timeout=2.0)
            if reader.is_alive():
                still_reading_stdout = True
        self._active_readers.clear()
        still_reading_stderr = False
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)
            still_reading_stderr = self._stderr_thread.is_alive()
            self._stderr_thread = None
        if self._proc.stdout is not None and not still_reading_stdout:
            with contextlib.suppress(OSError):
                self._proc.stdout.close()
        if self._proc.stderr is not None and not still_reading_stderr:
            with contextlib.suppress(OSError):
                self._proc.stderr.close()
        self._proc = None


class _MCPInProcessClient:
    """Context manager driving `MCPProxy` (with optional `reconciler_hook`) over real pipes."""

    def __init__(
        self,
        *,
        db_path: Path,
        server_script: Path,
        ledger_path: Path,
        mode_file: Path,
        session_id: str,
        target: str = "mcp:chaos",
        config: SidefxConfig | None = None,
        timeout_seconds: float = 2.0,
        reconciler_hook: Callable[[ActionRecord], Any] | None = None,
    ) -> None:
        self._db_path = db_path
        self._server_script = server_script
        self._ledger_path = ledger_path
        self._mode_file = mode_file
        self._session_id = session_id
        self._target = target
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._reconciler_hook = reconciler_hook
        self._journal: Journal | None = None
        self._thread: threading.Thread | None = None
        self._active_readers: list[threading.Thread] = []
        self._c_in_w: Any = None
        self._c_in_r: Any = None
        self._c_out_r: Any = None
        self._c_out_w: Any = None

    def __enter__(self) -> _MCPInProcessClient:
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self._c_in_r = os.fdopen(r_in, "rb", buffering=0)
        self._c_in_w = os.fdopen(w_in, "wb", buffering=0)
        self._c_out_r = os.fdopen(r_out, "rb")
        self._c_out_w = os.fdopen(w_out, "wb", buffering=0)

        env = _isolated_env(
            {
                "CHAOS_MCP_LEDGER": str(self._ledger_path),
                "CHAOS_MCP_MODE_FILE": str(self._mode_file),
            }
        )

        self._journal = Journal(self._db_path)
        proxy = MCPProxy(
            [sys.executable, str(self._server_script)],
            journal=self._journal,
            config=self._config,
            session_id=self._session_id,
            target=self._target,
            timeout_seconds=self._timeout_seconds,
            reconciler_hook=self._reconciler_hook,
            client_in=self._c_in_r,
            client_out=self._c_out_w,
            err_stream=io.BytesIO(),
            upstream_env=env,
        )
        self._thread = threading.Thread(target=proxy.run, name="sidefx-chaos-mcp", daemon=True)
        self._thread.start()
        return self

    def request(self, payload: dict[str, Any], *, timeout: float = 8.0) -> dict[str, Any]:
        assert self._c_in_w is not None and self._c_out_r is not None
        line = (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
            "ascii"
        )
        self._c_in_w.write(line)
        self._c_in_w.flush()

        result_holder: list[bytes] = []
        out_reader = self._c_out_r

        def _read_line() -> None:
            with contextlib.suppress(OSError, ValueError):
                result_holder.append(out_reader.readline())

        reader = threading.Thread(target=_read_line, daemon=True)
        self._active_readers.append(reader)
        reader.start()
        reader.join(timeout=timeout)
        if reader.is_alive() or not result_holder or not result_holder[0]:
            raise TimeoutError(f"Timed out waiting for MCPProxy reply to {payload.get('id')!r}")
        return json.loads(result_holder[0].decode("utf-8"))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # 1. Close client->proxy write end so MCPProxy hits EOF and finishes cleanly.
        if self._c_in_w is not None:
            with contextlib.suppress(OSError):
                self._c_in_w.close()
        if self._thread is not None:
            self._thread.join(timeout=8.5)
        # 2. Close proxy->client write end BEFORE joining readers or closing _c_out_r,
        #    so any reader blocked in readline() after a timeout wakes on EOF instead of hanging.
        if self._c_out_w is not None:
            with contextlib.suppress(OSError):
                self._c_out_w.close()
        for reader in self._active_readers:
            reader.join(timeout=2.0)
        self._active_readers.clear()
        for handle in (self._c_in_r, self._c_out_r):
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()
        if self._journal is not None:
            self._journal.close()
            self._journal = None


def _make_mcp_ledger_reconciler(
    ledger_path: Path, ticket_key_by_action: dict[str, str]
) -> Callable[[ActionRecord], ReconcileHookResult]:
    """Read-only reconciler hook querying the mock MCP server's durable JSONL ledger."""

    def _reconcile(record: ActionRecord) -> ReconcileHookResult:
        expected_key = ticket_key_by_action.get(record.action_id)
        if expected_key is None:
            return ReconcileHookResult(
                state=ReconcileState.UNKNOWN,
                details=f"No lookup key for {record.action_id}",
            )
        entries = _read_mcp_ledger(ledger_path)
        matches = [e for e in entries if e.get("arguments", {}).get("ticket_key") == expected_key]
        if len(matches) == 1:
            found = matches[0]
            return ReconcileHookResult(
                state=ReconcileState.FOUND,
                result_ref=str(found["url"]),
                summary=f"Recovered {found['tool']} #{found['seq']}",
                details=f"Found exact ticket_key={expected_key} in remote ledger",
            )
        if len(matches) == 0:
            return ReconcileHookResult(
                state=ReconcileState.NOT_FOUND,
                details=f"Confirmed 0 remote entries for ticket_key={expected_key}",
            )
        return ReconcileHookResult(
            state=ReconcileState.UNKNOWN,
            details=f"Ambiguous ledger state: {len(matches)} matches for {expected_key}",
        )

    return _reconcile


def _count_duplicates(subchecks: Sequence[dict[str, Any]]) -> tuple[int, int, int]:
    """Return ``(duplicate_writes, expected_writes, actual_writes)`` across subchecks."""
    dups = 0
    expected_total = 0
    actual_total = 0
    for chk in subchecks:
        exp = int(chk["expected_writes"])
        act = int(chk["actual_writes"])
        expected_total += exp
        actual_total += act
        if act > exp:
            dups += act - exp
    return dups, expected_total, actual_total


# ---------------------------------------------------------------------------
# Chaos Scenario 1: Pre-commit crash / timeout before remote write lands
# ---------------------------------------------------------------------------


def _scenario_pre_commit_crash_and_timeout() -> ScenarioResult:
    name = "pre_commit_crash_and_timeout"
    title = "Pre-commit crash & timeout recovery"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s1-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"

        # 1a. CLI wrap -> Mock GitHub: pre-commit timeout -> NOT_FOUND -> safe retry (1 write)
        #     + uncertain reconciliation (API 503) stays UNKNOWN (0 writes).
        with _ChaosGitHubServer() as gh_srv:
            fake_gh = _create_fake_gh(root)
            env = _isolated_env({"FAKE_GH_API_URL": gh_srv.base_url})
            cmd_a = [
                str(fake_gh),
                "issue",
                "create",
                "-R",
                "acme/app",
                "--title",
                "Pre-commit timeout",
                "--body",
                "Should retry safely once",
            ]
            with Journal(db_path) as journal:
                env["FAKE_GH_MODE"] = "pre_commit_timeout"
                r1 = wrap_command(
                    cmd_a,
                    journal=journal,
                    action_id="s1-gh-timeout",
                    session_id="chaos-s1",
                    timeout_seconds=0.35,
                    min_retry_age_seconds=0.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                after_timeout_posts = gh_srv.post_count

                env["FAKE_GH_MODE"] = "normal"
                r2 = wrap_command(
                    cmd_a,
                    journal=journal,
                    action_id="s1-gh-timeout",
                    session_id="chaos-s1",
                    timeout_seconds=5.0,
                    min_retry_age_seconds=0.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                gh_timeout_ok = (
                    r1.exit_code == EXIT_TIMEOUT
                    and r1.status == ActionStatus.UNKNOWN
                    and after_timeout_posts == 0
                    and r2.exit_code == 0
                    and r2.executed is True
                    and r2.status == ActionStatus.CONFIRMED
                    and r2.result_ref == "https://github.com/acme/app/issues/1"
                    and gh_srv.post_count == 1
                )
                subchecks.append(
                    {
                        "check": "cli_github_pre_commit_timeout",
                        "passed": gh_timeout_ok,
                        "expected_writes": 1,
                        "actual_writes": gh_srv.post_count,
                        "detail": (
                            f"timeout_exit={r1.exit_code}, "
                            f"posts_before_retry={after_timeout_posts}, "
                            f"retry_status={r2.status}, posts_after_retry={gh_srv.post_count}"
                        ),
                    }
                )

                # Uncertain reconciliation when GitHub API returns 503 after pre-commit crash
                cmd_b = [
                    str(fake_gh),
                    "issue",
                    "create",
                    "-R",
                    "acme/app",
                    "--title",
                    "Uncertain reconcile",
                    "--body",
                    "Must stay blocked on 503",
                ]
                posts_before_b = gh_srv.post_count
                env["FAKE_GH_MODE"] = "pre_commit_crash"
                r3 = wrap_command(
                    cmd_b,
                    journal=journal,
                    action_id="s1-gh-uncertain",
                    session_id="chaos-s1",
                    timeout_seconds=5.0,
                    min_retry_age_seconds=0.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                gh_srv.fail_gets_status = 503
                env["FAKE_GH_MODE"] = "normal"
                r4 = wrap_command(
                    cmd_b,
                    journal=journal,
                    action_id="s1-gh-uncertain",
                    session_id="chaos-s1",
                    timeout_seconds=5.0,
                    min_retry_age_seconds=0.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                gh_srv.fail_gets_status = None
                writes_b = gh_srv.post_count - posts_before_b
                gh_uncertain_ok = (
                    r3.status == ActionStatus.UNKNOWN
                    and r4.exit_code == EXIT_OUTCOME_UNKNOWN
                    and r4.executed is False
                    and r4.status == ActionStatus.UNKNOWN
                    and writes_b == 0
                )
                subchecks.append(
                    {
                        "check": "cli_github_uncertain_stays_blocked",
                        "passed": gh_uncertain_ok,
                        "expected_writes": 0,
                        "actual_writes": writes_b,
                        "detail": (
                            f"retry_exit={r4.exit_code}, status={r4.status}, writes={writes_b}"
                        ),
                    }
                )

        # 1b. CLI wrap -> Local bare git repo: pre-receive timeout -> NOT_FOUND -> safe retry once
        git_repo = _create_chaos_git_repo(root / "git_s1")
        expected_sha = git_repo.create_branch_commit("feat-pre", "pre.txt", "pre-commit test\n")
        git_repo.block_pre_flag.write_text("1", encoding="utf-8")

        with Journal(db_path) as journal:
            git_env = _git_env()
            g1 = wrap_command(
                ["git", "push", "origin", "feat-pre"],
                journal=journal,
                action_id="s1-git-pre",
                session_id="chaos-s1",
                timeout_seconds=0.4,
                min_retry_age_seconds=0.0,
                cwd=git_repo.work_dir,
                env=git_env,
            )
            events_after_timeout = git_repo.push_events_for_ref("refs/heads/feat-pre")
            git_repo.block_pre_flag.unlink(missing_ok=True)

            g2 = wrap_command(
                ["git", "push", "origin", "feat-pre"],
                journal=journal,
                action_id="s1-git-pre",
                session_id="chaos-s1",
                timeout_seconds=5.0,
                min_retry_age_seconds=0.0,
                cwd=git_repo.work_dir,
                env=git_env,
            )
            events_after_retry = git_repo.push_events_for_ref("refs/heads/feat-pre")
            git_pre_ok = (
                g1.exit_code == EXIT_TIMEOUT
                and g1.status == ActionStatus.UNKNOWN
                and len(events_after_timeout) == 0
                and g2.exit_code == 0
                and g2.executed is True
                and g2.status == ActionStatus.CONFIRMED
                and len(events_after_retry) == 1
                and events_after_retry[0][1] == expected_sha
            )
            subchecks.append(
                {
                    "check": "cli_git_push_pre_commit_timeout",
                    "passed": git_pre_ok,
                    "expected_writes": 1,
                    "actual_writes": len(events_after_retry),
                    "detail": (
                        f"events_before={len(events_after_timeout)}, "
                        f"events_after={len(events_after_retry)}, status={g2.status}"
                    ),
                }
            )

        # 1c. MCP proxy -> Mock stdio MCP server: pre-commit crash -> blocked without hook,
        #     then safely retried once when reconciler_hook proves NOT_FOUND.
        server_script, ledger_path, mode_file = _create_mock_mcp_server(root / "mcp_s1")
        call_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "create_ticket",
                "arguments": {
                    "_sidefx_action_id": "s1-mcp-pre",
                    "ticket_key": "T-PRE-1",
                    "title": "Pre-commit MCP test",
                },
            },
        }
        mode_file.write_text("pre_commit_crash", encoding="utf-8")
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s1",
            target="mcp:s1",
        ) as client1:
            resp_crash = client1.request(call_payload)

        writes_after_crash = len(_read_mcp_ledger(ledger_path))

        # Retry WITHOUT reconciler hook -> must stay blocked!
        mode_file.write_text("normal", encoding="utf-8")
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s1",
            target="mcp:s1",
        ) as client2:
            resp_blocked = client2.request({**call_payload, "id": 2})

        writes_after_blocked = len(_read_mcp_ledger(ledger_path))

        # Retry WITH read-only reconciler hook proving NOT_FOUND -> safe single retry!
        hook = _make_mcp_ledger_reconciler(ledger_path, {"s1-mcp-pre": "T-PRE-1"})
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s1",
            target="mcp:s1",
            reconciler_hook=hook,
        ) as client3:
            resp_retried = client3.request({**call_payload, "id": 3})

        writes_final = len(_read_mcp_ledger(ledger_path))
        mcp_pre_ok = (
            resp_crash.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and writes_after_crash == 0
            and resp_blocked.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and writes_after_blocked == 0
            and "result" in resp_retried
            and resp_retried["result"].get("isError") is False
            and writes_final == 1
        )
        subchecks.append(
            {
                "check": "mcp_proxy_pre_commit_crash",
                "passed": mcp_pre_ok,
                "expected_writes": 1,
                "actual_writes": writes_final,
                "detail": (
                    f"crash_err={resp_crash.get('error', {}).get('code')}, "
                    f"blocked_err={resp_blocked.get('error', {}).get('code')}, "
                    f"final_writes={writes_final}"
                ),
            }
        )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        details=f"{sum(1 for c in subchecks if c['passed'])}/{len(subchecks)} surfaces verified",
        error=None if all_passed else "One or more pre-commit recovery checks failed",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Chaos Scenario 2: Post-commit response-loss crash / timeout (all 3 surfaces)
# ---------------------------------------------------------------------------


def _scenario_post_commit_response_loss() -> ScenarioResult:
    name = "post_commit_response_loss"
    title = "Post-commit response-loss reconciliation"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s2-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"

        # 2a. CLI wrap -> Mock GitHub: post-commit timeout + post-commit crash -> 0 duplicate writes
        with _ChaosGitHubServer() as gh_srv:
            fake_gh = _create_fake_gh(root)
            env = _isolated_env({"FAKE_GH_API_URL": gh_srv.base_url})

            cmd_timeout = [
                str(fake_gh),
                "issue",
                "create",
                "-R",
                "acme/app",
                "--title",
                "Lost after timeout",
                "--body",
                "Post-commit timeout test",
            ]
            cmd_crash = [
                str(fake_gh),
                "issue",
                "create",
                "-R",
                "acme/app",
                "--title",
                "Lost after crash",
                "--body",
                "Post-commit crash test",
            ]

            with Journal(db_path) as journal:
                env["FAKE_GH_MODE"] = "post_commit_timeout"
                t1 = wrap_command(
                    cmd_timeout,
                    journal=journal,
                    action_id="s2-gh-timeout",
                    session_id="chaos-s2",
                    timeout_seconds=1.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                env["FAKE_GH_MODE"] = "normal"
                t2 = wrap_command(
                    cmd_timeout,
                    journal=journal,
                    action_id="s2-gh-timeout",
                    session_id="chaos-s2",
                    timeout_seconds=5.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                posts_after_t = gh_srv.post_count
                gh_timeout_ok = (
                    t1.exit_code == EXIT_TIMEOUT
                    and t1.status == ActionStatus.UNKNOWN
                    and t2.exit_code == 0
                    and t2.executed is False
                    and t2.reconciled is True
                    and t2.status == ActionStatus.RECONCILED
                    and t2.result_ref == "https://github.com/acme/app/issues/1"
                    and posts_after_t == 1
                )
                subchecks.append(
                    {
                        "check": "cli_github_post_commit_timeout",
                        "passed": gh_timeout_ok,
                        "expected_writes": 1,
                        "actual_writes": posts_after_t,
                        "detail": (
                            f"recovered={t2.result_ref}, executed_on_retry={t2.executed}, "
                            f"posts={posts_after_t}"
                        ),
                    }
                )

                env["FAKE_GH_MODE"] = "post_commit_crash"
                c1 = wrap_command(
                    cmd_crash,
                    journal=journal,
                    action_id="s2-gh-crash",
                    session_id="chaos-s2",
                    timeout_seconds=5.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                env["FAKE_GH_MODE"] = "normal"
                c2 = wrap_command(
                    cmd_crash,
                    journal=journal,
                    action_id="s2-gh-crash",
                    session_id="chaos-s2",
                    timeout_seconds=5.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                crash_posts = gh_srv.post_count - posts_after_t
                gh_crash_ok = (
                    c1.exit_code != 0
                    and c1.status == ActionStatus.UNKNOWN
                    and c2.exit_code == 0
                    and c2.executed is False
                    and c2.reconciled is True
                    and c2.status == ActionStatus.RECONCILED
                    and c2.result_ref == "https://github.com/acme/app/issues/2"
                    and crash_posts == 1
                )
                subchecks.append(
                    {
                        "check": "cli_github_post_commit_crash",
                        "passed": gh_crash_ok,
                        "expected_writes": 1,
                        "actual_writes": crash_posts,
                        "detail": (
                            f"recovered={c2.result_ref}, executed_on_retry={c2.executed}, "
                            f"posts={crash_posts}"
                        ),
                    }
                )

        # 2b. CLI wrap -> Local bare git repo: slow post-receive hook timeout -> ls-remote reconcile
        git_repo = _create_chaos_git_repo(root / "git_s2")
        expected_sha = git_repo.create_branch_commit("feat-post", "post.txt", "post-commit push\n")
        git_repo.slow_post_flag.write_text("1", encoding="utf-8")

        with Journal(db_path) as journal:
            git_env = _git_env()
            p1 = wrap_command(
                ["git", "push", "origin", "feat-post"],
                journal=journal,
                action_id="s2-git-post",
                session_id="chaos-s2",
                timeout_seconds=1.0,
                cwd=git_repo.work_dir,
                env=git_env,
            )
            git_repo.slow_post_flag.unlink(missing_ok=True)
            p2 = wrap_command(
                ["git", "push", "origin", "feat-post"],
                journal=journal,
                action_id="s2-git-post",
                session_id="chaos-s2",
                timeout_seconds=5.0,
                cwd=git_repo.work_dir,
                env=git_env,
            )
            events = git_repo.push_events_for_ref("refs/heads/feat-post")
            push_attempts = git_repo.push_attempt_count()
            remote_sha = git_repo.remote_ref_sha("refs/heads/feat-post")
            expected_ref = f"origin refs/heads/feat-post@{expected_sha[:12]}"
            git_post_ok = (
                p1.exit_code == EXIT_TIMEOUT
                and p1.status == ActionStatus.UNKNOWN
                and p2.exit_code == 0
                and p2.executed is False
                and p2.reconciled is True
                and p2.status == ActionStatus.RECONCILED
                and p2.result_ref == expected_ref
                and len(events) == 1
                and push_attempts == 1
                and remote_sha == expected_sha
            )
            subchecks.append(
                {
                    "check": "cli_git_push_post_commit_timeout",
                    "passed": git_post_ok,
                    "expected_writes": 1,
                    "actual_writes": max(len(events), push_attempts),
                    "detail": (
                        f"recovered={p2.result_ref}, executed_on_retry={p2.executed}, "
                        f"remote_pushes={len(events)}, receive_pack_contacts={push_attempts}"
                    ),
                }
            )

        # 2c. MCP proxy -> Mock stdio MCP server: post-commit crash & timeout -> 0 duplicate writes
        server_script, ledger_path, mode_file = _create_mock_mcp_server(root / "mcp_s2")
        crash_call = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "create_ticket",
                "arguments": {
                    "_sidefx_action_id": "s2-mcp-crash",
                    "ticket_key": "T-POST-CRASH",
                    "title": "Crash after commit",
                },
            },
        }
        mode_file.write_text("post_commit_crash", encoding="utf-8")
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s2",
            target="mcp:s2",
        ) as c_crash:
            resp_c1 = c_crash.request(crash_call)

        # Retry WITHOUT hook -> blocked with OUTCOME UNKNOWN, 0 duplicate writes
        mode_file.write_text("normal", encoding="utf-8")
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s2",
            target="mcp:s2",
        ) as c_block:
            resp_c2 = c_block.request({**crash_call, "id": 11})

        # Retry WITH reconciler hook -> recovers exact remote URL with 0 duplicate writes
        hook = _make_mcp_ledger_reconciler(ledger_path, {"s2-mcp-crash": "T-POST-CRASH"})
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s2",
            target="mcp:s2",
            reconciler_hook=hook,
        ) as c_rec:
            resp_c3 = c_rec.request({**crash_call, "id": 12})

        crash_entries = [
            e
            for e in _read_mcp_ledger(ledger_path)
            if e.get("arguments", {}).get("ticket_key") == "T-POST-CRASH"
        ]
        sidefx_meta = resp_c3.get("result", {}).get("_sidefx", {})
        mcp_crash_ok = (
            resp_c1.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and OUTCOME_UNKNOWN_BANNER in resp_c1.get("error", {}).get("message", "")
            and resp_c2.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and sidefx_meta.get("replayed") is True
            and sidefx_meta.get("status") == "reconciled"
            and sidefx_meta.get("result_ref") == "https://tickets.sidefx.test/items/1"
            and len(crash_entries) == 1
        )
        subchecks.append(
            {
                "check": "mcp_proxy_post_commit_crash",
                "passed": mcp_crash_ok,
                "expected_writes": 1,
                "actual_writes": len(crash_entries),
                "detail": (
                    f"blocked_without_hook={resp_c2.get('error', {}).get('code')}, "
                    f"reconciled_ref={sidefx_meta.get('result_ref')}, writes={len(crash_entries)}"
                ),
            }
        )

        # Also test MCP post-commit timeout -> retry blocked while in-flight, 0 duplicate writes
        timeout_call = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "create_ticket",
                "arguments": {
                    "_sidefx_action_id": "s2-mcp-timeout",
                    "ticket_key": "T-POST-TIMEOUT",
                    "title": "Timeout after commit",
                },
            },
        }
        mode_file.write_text("post_commit_timeout", encoding="utf-8")
        with _MCPInProcessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s2",
            target="mcp:s2",
            timeout_seconds=0.5,
        ) as c_to:
            # Warm up upstream interpreter so cold start does not eat the timeout window.
            _ = c_to.request({"jsonrpc": "2.0", "id": 19, "method": "initialize", "params": {}})
            resp_t1 = c_to.request(timeout_call)
            # Immediate retry on the live proxy must be blocked (never double-executes!)
            resp_t2 = c_to.request({**timeout_call, "id": 21})

        timeout_entries = [
            e
            for e in _read_mcp_ledger(ledger_path)
            if e.get("arguments", {}).get("ticket_key") == "T-POST-TIMEOUT"
        ]
        mcp_timeout_ok = (
            resp_t1.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and resp_t2.get("error", {}).get("code") == ERR_OUTCOME_UNKNOWN
            and len(timeout_entries) == 1
        )
        subchecks.append(
            {
                "check": "mcp_proxy_post_commit_timeout",
                "passed": mcp_timeout_ok,
                "expected_writes": 1,
                "actual_writes": len(timeout_entries),
                "detail": (
                    f"timeout_err={resp_t1.get('error', {}).get('code')}, "
                    f"retry_err={resp_t2.get('error', {}).get('code')}, "
                    f"writes={len(timeout_entries)}"
                ),
            }
        )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        details=f"{sum(1 for c in subchecks if c['passed'])}/{len(subchecks)} surfaces verified",
        error=None if all_passed else "One or more post-commit reconciliation checks failed",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Chaos Scenario 3: Concurrent duplicate dispatch from multiple workers
# ---------------------------------------------------------------------------


def _scenario_concurrent_duplicate_dispatch() -> ScenarioResult:
    name = "concurrent_duplicate_dispatch"
    title = "Concurrent multi-worker duplicate dispatch"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []
    worker_count = 4

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s3-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"

        with _ChaosGitHubServer() as gh_srv:
            fake_gh = _create_fake_gh(root)
            env = _isolated_env({"FAKE_GH_API_URL": gh_srv.base_url})
            env["FAKE_GH_MODE"] = "normal"
            env["FAKE_GH_HOLD_MS"] = "350"

            cfg_path = _write_default_config(root)
            wrap_cmd = [
                sys.executable,
                "-m",
                "sidefx",
                "wrap",
                "--config",
                str(cfg_path),
                "--db",
                str(db_path),
                "--session-id",
                "chaos-s3",
                "--action-id",
                "s3-race-issue",
                "--github-api-url",
                gh_srv.base_url,
                "--",
                str(fake_gh),
                "issue",
                "create",
                "-R",
                "acme/app",
                "--title",
                "Concurrent Race",
                "--body",
                "Exactly one remote write",
            ]

            popen_extra: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
            procs = [
                subprocess.Popen(
                    wrap_cmd,
                    cwd=str(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    **popen_extra,
                )
                for _ in range(worker_count)
            ]
            exit_codes: list[int] = []
            outputs: list[tuple[str, str]] = []
            try:
                for proc in procs:
                    out, err = proc.communicate(timeout=15.0)
                    exit_codes.append(proc.returncode)
                    outputs.append((out.strip(), err.strip()))
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        if os.name == "posix":
                            with contextlib.suppress(OSError):
                                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            with contextlib.suppress(OSError):
                                proc.kill()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            proc.wait(timeout=2.0)
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            with contextlib.suppress(OSError):
                                stream.close()

            # Follow-up call must replay cleanly with 0 additional POSTs
            env["FAKE_GH_HOLD_MS"] = "0"
            replay_proc = subprocess.run(
                wrap_cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                env=env,
                timeout=10.0,
                check=False,
            )

            with Journal.open_readonly(db_path) as journal:
                rows = journal.list_actions(session_id="chaos-s3")

            valid_codes = all(code in (0, EXIT_OUTCOME_UNKNOWN) for code in exit_codes)
            at_least_one_ok = any(code == 0 for code in exit_codes)
            race_ok = (
                gh_srv.post_count == 1
                and valid_codes
                and at_least_one_ok
                and replay_proc.returncode == 0
                and "https://github.com/acme/app/issues/1" in replay_proc.stdout
                and len(rows) == 1
                and rows[0].status in (ActionStatus.CONFIRMED, ActionStatus.RECONCILED)
                and rows[0].result_ref == "https://github.com/acme/app/issues/1"
            )
            subchecks.append(
                {
                    "check": "multiprocess_wrap_race",
                    "passed": race_ok,
                    "expected_writes": 1,
                    "actual_writes": gh_srv.post_count,
                    "detail": (
                        f"workers={worker_count}, exit_codes={exit_codes}, "
                        f"remote_posts={gh_srv.post_count}, journal_rows={len(rows)}"
                    ),
                }
            )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        details=f"{worker_count} concurrent OS workers -> {act_w} remote write",
        error=None if all_passed else "Concurrent dispatch race allowed duplicate or invalid state",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Chaos Scenario 4: Payload mutation under reused action_id (CLI + MCP)
# ---------------------------------------------------------------------------


def _scenario_payload_mutation_reused_id() -> ScenarioResult:
    name = "payload_mutation_reused_id"
    title = "Payload mutation under reused action_id"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s4-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"

        # 4a. CLI wrap: mutated args under both confirmed AND unknown action_id rejected
        with _ChaosGitHubServer() as gh_srv:
            fake_gh = _create_fake_gh(root)
            env = _isolated_env({"FAKE_GH_API_URL": gh_srv.base_url})

            with Journal(db_path) as journal:
                env["FAKE_GH_MODE"] = "normal"
                r1 = wrap_command(
                    [
                        str(fake_gh),
                        "issue",
                        "create",
                        "-R",
                        "acme/app",
                        "--title",
                        "Original",
                        "--body",
                        "Original body",
                    ],
                    journal=journal,
                    action_id="s4-cli-confirmed",
                    session_id="chaos-s4",
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                r2 = wrap_command(
                    [
                        str(fake_gh),
                        "issue",
                        "create",
                        "-R",
                        "acme/app",
                        "--title",
                        "Tampered",
                        "--body",
                        "Original body",
                    ],
                    journal=journal,
                    action_id="s4-cli-confirmed",
                    session_id="chaos-s4",
                    env=env,
                    github_api_url=gh_srv.base_url,
                )

                # Also verify payload mutation against an UNKNOWN row is rejected before dispatch
                env["FAKE_GH_MODE"] = "post_commit_crash"
                r3 = wrap_command(
                    [
                        str(fake_gh),
                        "issue",
                        "create",
                        "-R",
                        "acme/app",
                        "--title",
                        "Unknown original",
                        "--body",
                        "Crash body",
                    ],
                    journal=journal,
                    action_id="s4-cli-unknown",
                    session_id="chaos-s4",
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                env["FAKE_GH_MODE"] = "normal"
                r4 = wrap_command(
                    [
                        str(fake_gh),
                        "issue",
                        "create",
                        "-R",
                        "acme/app",
                        "--title",
                        "Unknown tampered",
                        "--body",
                        "Crash body",
                    ],
                    journal=journal,
                    action_id="s4-cli-unknown",
                    session_id="chaos-s4",
                    env=env,
                    github_api_url=gh_srv.base_url,
                )

            cli_mut_ok = (
                r1.exit_code == 0
                and r2.exit_code == EXIT_PAYLOAD_MISMATCH
                and r2.executed is False
                and r3.status == ActionStatus.UNKNOWN
                and r4.exit_code == EXIT_PAYLOAD_MISMATCH
                and r4.executed is False
                and gh_srv.post_count == 2
            )
            subchecks.append(
                {
                    "check": "cli_wrap_payload_mutation",
                    "passed": cli_mut_ok,
                    "expected_writes": 2,
                    "actual_writes": gh_srv.post_count,
                    "detail": (
                        f"confirmed_tamper_exit={r2.exit_code}, "
                        f"unknown_tamper_exit={r4.exit_code}, posts={gh_srv.post_count}"
                    ),
                }
            )

        # 4b. MCP proxy subprocess: mutated arguments under reused _sidefx_action_id rejected
        server_script, ledger_path, mode_file = _create_mock_mcp_server(root / "mcp_s4")
        mode_file.write_text("normal", encoding="utf-8")
        with _MCPSubprocessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s4",
            cwd=root,
            target="mcp:s4",
        ) as mcp_cli:
            m1 = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {
                            "_sidefx_action_id": "s4-mcp-1",
                            "title": "Original ticket",
                        },
                    },
                }
            )
            m2 = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {
                            "_sidefx_action_id": "s4-mcp-1",
                            "title": "Tampered ticket",
                        },
                    },
                }
            )

        mcp_writes = len(_read_mcp_ledger(ledger_path))
        mcp_mut_ok = (
            "result" in m1
            and m2.get("error", {}).get("code") == ERR_PAYLOAD_MISMATCH
            and m2.get("error", {}).get("data", {}).get("kind") == "payload_mismatch"
            and mcp_writes == 1
        )
        subchecks.append(
            {
                "check": "mcp_proxy_payload_mutation",
                "passed": mcp_mut_ok,
                "expected_writes": 1,
                "actual_writes": mcp_writes,
                "detail": (
                    f"tamper_err={m2.get('error', {}).get('code')}, ledger_writes={mcp_writes}"
                ),
            }
        )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        details="CLI & MCP reused-ID mutations rejected pre-dispatch",
        error=None if all_passed else "Payload mutation check failed",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Chaos Scenario 5: Preflight numeric cap & string/domain denylist violations
# ---------------------------------------------------------------------------


def _scenario_preflight_policy_enforcement() -> ScenarioResult:
    name = "preflight_policy_enforcement"
    title = "Preflight caps & denylist enforcement"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s5-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"
        cfg_path = root / ".sidefx.json"
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="refund_payment",
                        arg="amount",
                        max_value=50,
                        reason="Refund cap is $50",
                    ),
                    PreflightRule(
                        tool="push_branch",
                        arg="branch",
                        deny=("main", "master", "release/*"),
                        reason="Protected branch",
                    ),
                    PreflightRule(
                        tool="send_email",
                        arg="to",
                        allow_domains=("acme.test",),
                        deny_domains=("evil.test",),
                        reason="Internal recipients only",
                    ),
                )
            )
        )
        cfg_path.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
        # Validate config parses cleanly via load_config
        _ = load_config(cfg_path)

        server_script, ledger_path, mode_file = _create_mock_mcp_server(root / "mcp_s5")
        mode_file.write_text("normal", encoding="utf-8")

        with _MCPSubprocessClient(
            db_path=db_path,
            server_script=server_script,
            ledger_path=ledger_path,
            mode_file=mode_file,
            session_id="chaos-s5",
            cwd=root,
            target="mcp:s5",
            config_path=cfg_path,
        ) as mcp_cli:
            # Blocked calls (0 remote writes expected)
            r_cap = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "refund_payment",
                        "arguments": {"_sidefx_action_id": "s5-cap-bad", "amount": 250},
                    },
                }
            )
            r_branch = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "push_branch",
                        "arguments": {"_sidefx_action_id": "s5-branch-bad", "branch": "main"},
                    },
                }
            )
            r_deny_dom = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "send_email",
                        "arguments": {
                            "_sidefx_action_id": "s5-email-deny",
                            "to": "attacker@evil.test",
                        },
                    },
                }
            )
            r_outside_dom = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "send_email",
                        "arguments": {
                            "_sidefx_action_id": "s5-email-outside",
                            "to": "user@other.org",
                        },
                    },
                }
            )

            writes_after_blocks = len(_read_mcp_ledger(ledger_path))

            # Allowed calls (2 remote writes expected — proves rules are selective)
            r_cap_ok = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "refund_payment",
                        "arguments": {"_sidefx_action_id": "s5-cap-ok", "amount": 25},
                    },
                }
            )
            r_email_ok = mcp_cli.request(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "send_email",
                        "arguments": {
                            "_sidefx_action_id": "s5-email-ok",
                            "to": "ops@acme.test",
                        },
                    },
                }
            )

        ledger_entries = _read_mcp_ledger(ledger_path)
        with Journal.open_readonly(db_path) as journal:
            recorded_ids = {rec.action_id for rec in journal.list_actions(session_id="chaos-s5")}

        blocked_resps = (r_cap, r_branch, r_deny_dom, r_outside_dom)
        all_blocked = all(
            resp.get("error", {}).get("code") == ERR_PREFLIGHT_DENIED
            and resp.get("error", {}).get("data", {}).get("kind") == "preflight_denied"
            for resp in blocked_resps
        )
        subchecks.append(
            {
                "check": "preflight_blocked_violations",
                "passed": all_blocked and writes_after_blocks == 0,
                "expected_writes": 0,
                "actual_writes": writes_after_blocks,
                "detail": (
                    f"blocked_calls={len(blocked_resps)}, writes_on_blocked={writes_after_blocks}"
                ),
            }
        )

        allowed_writes = len(ledger_entries) - writes_after_blocks
        allowed_ok = (
            "result" in r_cap_ok
            and "result" in r_email_ok
            and allowed_writes == 2
            and recorded_ids == {"s5-cap-ok", "s5-email-ok"}
        )
        subchecks.append(
            {
                "check": "preflight_allowed_control_calls",
                "passed": allowed_ok,
                "expected_writes": 2,
                "actual_writes": allowed_writes,
                "detail": f"allowed_writes={allowed_writes}, journal_ids={sorted(recorded_ids)}",
            }
        )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        details="4 policy violations blocked (0 writes); 2 valid calls allowed",
        error=None if all_passed else "Preflight policy enforcement check failed",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Chaos Scenario 6: Session-end receipt ground-truth completeness
# ---------------------------------------------------------------------------


def _scenario_session_receipt_ground_truth() -> ScenarioResult:
    name = "session_receipt_ground_truth"
    title = "Session-end receipt ground-truth accuracy"
    started = time.perf_counter()
    subchecks: list[dict[str, Any]] = []
    session_id = "chaos-receipt-session"

    with tempfile.TemporaryDirectory(prefix="sidefx-chaos-s6-") as tmp:
        root = Path(tmp)
        db_path = root / "journal.db"

        with _ChaosGitHubServer() as gh_srv:
            fake_gh = _create_fake_gh(root)
            git_repo = _create_chaos_git_repo(root / "git_s6")
            server_script, ledger_path, mode_file = _create_mock_mcp_server(root / "mcp_s6")

            env = _isolated_env({"FAKE_GH_API_URL": gh_srv.base_url})

            with Journal(db_path) as journal:
                # 1. CLI GitHub confirmed
                env["FAKE_GH_MODE"] = "normal"
                gh_ok = wrap_command(
                    [
                        str(fake_gh),
                        "issue",
                        "create",
                        "-R",
                        "acme/app",
                        "--title",
                        "Receipt Confirmed",
                        "--body",
                        "Confirmed issue",
                    ],
                    journal=journal,
                    action_id="s6-gh-confirmed",
                    session_id=session_id,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )

                # 2. CLI GitHub reconciled (post-commit timeout -> retry recovers URL)
                cmd_gh_rec = [
                    str(fake_gh),
                    "issue",
                    "create",
                    "-R",
                    "acme/app",
                    "--title",
                    "Receipt Reconciled",
                    "--body",
                    "Reconciled issue",
                ]
                env["FAKE_GH_MODE"] = "post_commit_timeout"
                _ = wrap_command(
                    cmd_gh_rec,
                    journal=journal,
                    action_id="s6-gh-reconciled",
                    session_id=session_id,
                    timeout_seconds=1.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )
                env["FAKE_GH_MODE"] = "normal"
                gh_rec = wrap_command(
                    cmd_gh_rec,
                    journal=journal,
                    action_id="s6-gh-reconciled",
                    session_id=session_id,
                    timeout_seconds=5.0,
                    env=env,
                    github_api_url=gh_srv.base_url,
                )

                # 3. CLI Git push reconciled (slow post-receive timeout -> retry recovers ref)
                push_sha = git_repo.create_branch_commit(
                    "feat-receipt", "receipt.txt", "ground truth\n"
                )
                git_env = _git_env()
                git_repo.slow_post_flag.write_text("1", encoding="utf-8")
                _ = wrap_command(
                    ["git", "push", "origin", "feat-receipt"],
                    journal=journal,
                    action_id="s6-git-reconciled",
                    session_id=session_id,
                    timeout_seconds=1.0,
                    cwd=git_repo.work_dir,
                    env=git_env,
                )
                git_repo.slow_post_flag.unlink(missing_ok=True)
                git_rec = wrap_command(
                    ["git", "push", "origin", "feat-receipt"],
                    journal=journal,
                    action_id="s6-git-reconciled",
                    session_id=session_id,
                    timeout_seconds=5.0,
                    cwd=git_repo.work_dir,
                    env=git_env,
                )

                # 4. CLI generic failed command (0 remote writes)
                cli_fail = wrap_command(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('boom', file=sys.stderr); sys.exit(1)",
                    ],
                    journal=journal,
                    action_id="s6-cli-failed",
                    session_id=session_id,
                    target="deploy:script",
                    tool_name="deploy_script",
                    env=_isolated_env(),
                )

            # 5-7. MCP confirmed + MCP failed + MCP unknown (crashed after write)
            mode_file.write_text("normal", encoding="utf-8")
            with _MCPSubprocessClient(
                db_path=db_path,
                server_script=server_script,
                ledger_path=ledger_path,
                mode_file=mode_file,
                session_id=session_id,
                cwd=root,
                target="mcp:s6",
            ) as mcp_cli:
                # Read-only call must NOT appear in receipt
                _ = mcp_cli.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "get_item", "arguments": {"id": 99}},
                    }
                )
                _ = mcp_cli.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_ticket",
                            "arguments": {
                                "_sidefx_action_id": "s6-mcp-confirmed",
                                "title": "Deploy checklist",
                            },
                        },
                    }
                )
                mode_file.write_text("validation_error", encoding="utf-8")
                _ = mcp_cli.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "create_ticket",
                            "arguments": {
                                "_sidefx_action_id": "s6-mcp-failed",
                                "title": "Invalid ticket",
                            },
                        },
                    }
                )
                mode_file.write_text("post_commit_crash", encoding="utf-8")
                _ = mcp_cli.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "create_ticket",
                            "arguments": {
                                "_sidefx_action_id": "s6-mcp-unknown",
                                "title": "Crashed ticket",
                            },
                        },
                    }
                )

            cfg_path = _write_default_config(root)
            # Run the REAL `sidefx report` CLI in both JSON and Markdown formats
            report_json_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sidefx",
                    "report",
                    "--config",
                    str(cfg_path),
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--format",
                    "json",
                ],
                cwd=str(root),
                env=_isolated_env(),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            report_md_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sidefx",
                    "report",
                    "--config",
                    str(cfg_path),
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--format",
                    "markdown",
                ],
                cwd=str(root),
                env=_isolated_env(),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )

            # Build ground-truth sets strictly from the remote mock servers & bare git repo
            gh_urls = {item["html_url"] for item in gh_srv.issues}
            git_events = git_repo.push_events_for_ref("refs/heads/feat-receipt")
            git_attempts = git_repo.push_attempt_count()
            remote_git_sha = git_repo.remote_ref_sha("refs/heads/feat-receipt")
            expected_git_ref = (
                f"origin refs/heads/feat-receipt@{remote_git_sha[:12]}"
                if remote_git_sha is not None
                else None
            )
            mcp_entries = _read_mcp_ledger(ledger_path)
            mcp_confirmed_entries = [
                e for e in mcp_entries if e.get("arguments", {}).get("title") == "Deploy checklist"
            ]
            mcp_crashed_entries = [
                e for e in mcp_entries if e.get("arguments", {}).get("title") == "Crashed ticket"
            ]

            expected_confirmed_reconciled_refs = set(gh_urls)
            if expected_git_ref is not None:
                expected_confirmed_reconciled_refs.add(expected_git_ref)
            if mcp_confirmed_entries:
                expected_confirmed_reconciled_refs.add(str(mcp_confirmed_entries[0]["url"]))

            report_data: dict[str, Any] = {}
            if report_json_proc.returncode == 0 and report_json_proc.stdout.strip():
                report_data = json.loads(report_json_proc.stdout)
            md_text = report_md_proc.stdout
            groups = report_data.get(
                "groups", {"confirmed": [], "reconciled": [], "failed": [], "unknown": []}
            )

            actual_confirmed_reconciled_refs = {
                row["result_ref"]
                for row in (groups["confirmed"] + groups["reconciled"])
                if row.get("result_ref")
            }
            actual_confirmed_ids = {row["action_id"] for row in groups["confirmed"]}
            actual_reconciled_ids = {row["action_id"] for row in groups["reconciled"]}
            actual_failed_ids = {row["action_id"] for row in groups["failed"]}
            actual_unknown_ids = {row["action_id"] for row in groups["unknown"]}

            missing_refs = expected_confirmed_reconciled_refs - actual_confirmed_reconciled_refs
            hallucinated_refs = (
                actual_confirmed_reconciled_refs - expected_confirmed_reconciled_refs
            )

            # Pure receipt-content ground-truth checks (JSON + Markdown fidelity).
            receipt_assertions = [
                report_json_proc.returncode == 0,
                report_md_proc.returncode == 0,
                report_data.get("total") == 7,
                actual_confirmed_ids == {"s6-gh-confirmed", "s6-mcp-confirmed"},
                actual_reconciled_ids == {"s6-gh-reconciled", "s6-git-reconciled"},
                actual_failed_ids == {"s6-cli-failed", "s6-mcp-failed"},
                actual_unknown_ids == {"s6-mcp-unknown"},
                len(expected_confirmed_reconciled_refs) == 4,
                len(missing_refs) == 0,
                len(hallucinated_refs) == 0,
                "## sidefx Session Receipt" in md_text,
                "### Confirmed (2)" in md_text,
                "### Reconciled (2)" in md_text,
                "### Failed (2)" in md_text,
                "### Unknown / Unresolved (1)" in md_text,
                all(ref in md_text for ref in expected_confirmed_reconciled_refs),
                "s6-mcp-unknown" in md_text,
            ]
            matched = sum(1 for ok in receipt_assertions if ok)
            accuracy_pct = (matched / len(receipt_assertions)) * 100.0

            subchecks.append(
                {
                    "check": "github_ground_truth_writes",
                    "passed": (
                        gh_srv.post_count == 2
                        and gh_ok.status == ActionStatus.CONFIRMED
                        and gh_rec.status == ActionStatus.RECONCILED
                    ),
                    "expected_writes": 2,
                    "actual_writes": gh_srv.post_count,
                    "detail": f"github_issues={sorted(gh_urls)}",
                }
            )
            subchecks.append(
                {
                    "check": "git_ground_truth_pushes",
                    "passed": (
                        len(git_events) == 1
                        and git_attempts == 1
                        and remote_git_sha == push_sha
                        and git_rec.status == ActionStatus.RECONCILED
                    ),
                    "expected_writes": 1,
                    "actual_writes": max(len(git_events), git_attempts),
                    "detail": (f"git_ref={expected_git_ref}, receive_pack_contacts={git_attempts}"),
                }
            )
            subchecks.append(
                {
                    "check": "cli_failed_command_zero_writes",
                    "passed": cli_fail.status == ActionStatus.FAILED,
                    "expected_writes": 0,
                    "actual_writes": 0,
                    "detail": f"cli_fail_status={cli_fail.status}",
                }
            )
            subchecks.append(
                {
                    "check": "mcp_ground_truth_writes",
                    "passed": (
                        len(mcp_entries) == 2
                        and len(mcp_confirmed_entries) == 1
                        and len(mcp_crashed_entries) == 1
                    ),
                    "expected_writes": 2,
                    "actual_writes": len(mcp_entries),
                    "detail": (
                        f"mcp_writes={len(mcp_entries)} (1 confirmed + 1 crashed-unknown; "
                        "0 for failed/read)"
                    ),
                }
            )
            subchecks.append(
                {
                    "check": "receipt_json_and_markdown_accuracy",
                    "passed": accuracy_pct == 100.0,
                    "expected_writes": 0,
                    "actual_writes": 0,
                    "detail": (
                        f"accuracy={accuracy_pct:.1f}% ({matched}/{len(receipt_assertions)}), "
                        f"missing={sorted(missing_refs)}, hallucinated={sorted(hallucinated_refs)}"
                    ),
                }
            )

    dups, exp_w, act_w = _count_duplicates(subchecks)
    all_passed = all(c["passed"] for c in subchecks) and dups == 0 and accuracy_pct == 100.0
    return ScenarioResult(
        name=name,
        title=title,
        passed=all_passed,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        duplicate_remote_writes=dups,
        expected_remote_writes=exp_w,
        actual_remote_writes=act_w,
        receipt_accuracy_pct=accuracy_pct,
        details=f"JSON & Markdown receipts matched {accuracy_pct:.1f}% of remote ground truth",
        error=None if all_passed else "Receipt did not match remote ground truth 100%",
        subchecks=tuple(subchecks),
    )


# ---------------------------------------------------------------------------
# Suite runner, formatting, and CLI handler
# ---------------------------------------------------------------------------

SCENARIO_REGISTRY: tuple[tuple[str, Callable[[], ScenarioResult]], ...] = (
    ("pre_commit_crash_and_timeout", _scenario_pre_commit_crash_and_timeout),
    ("post_commit_response_loss", _scenario_post_commit_response_loss),
    ("concurrent_duplicate_dispatch", _scenario_concurrent_duplicate_dispatch),
    ("payload_mutation_reused_id", _scenario_payload_mutation_reused_id),
    ("preflight_policy_enforcement", _scenario_preflight_policy_enforcement),
    ("session_receipt_ground_truth", _scenario_session_receipt_ground_truth),
)


def available_scenario_names() -> tuple[str, ...]:
    """Return ordered tuple of all registered chaos scenario names."""
    return tuple(name for name, _ in SCENARIO_REGISTRY)


def run_chaos_suite(
    *,
    scenarios: Sequence[str] | None = None,
) -> ChaosSuiteReport:
    """Execute the automated chaos fault-injection suite and return structured metrics."""
    registry_map = dict(SCENARIO_REGISTRY)
    if scenarios is not None:
        unknown = [s for s in scenarios if s not in registry_map]
        if unknown:
            valid = ", ".join(available_scenario_names())
            raise ValueError(f"Unknown chaos scenario(s): {', '.join(unknown)}. Valid: {valid}")
        selected = [(s, registry_map[s]) for s in dict.fromkeys(scenarios)]
    else:
        selected = list(SCENARIO_REGISTRY)

    suite_start = time.perf_counter()
    results: list[ScenarioResult] = []
    with _hermetic_chaos_env():
        for scenario_name, fn in selected:
            sc_start = time.perf_counter()
            try:
                results.append(fn())
            except Exception as exc:
                tb_lines = traceback.format_exc().strip().splitlines()
                tb_tail = " | ".join(tb_lines[-3:]) if tb_lines else f"{type(exc).__name__}: {exc}"
                results.append(
                    ScenarioResult(
                        name=scenario_name,
                        title=scenario_name,
                        passed=False,
                        duration_ms=(time.perf_counter() - sc_start) * 1000.0,
                        duplicate_remote_writes=0,
                        expected_remote_writes=0,
                        actual_remote_writes=0,
                        details="Scenario raised unhandled exception (writes unmeasured)",
                        error=tb_tail,
                    )
                )

    total_ms = (time.perf_counter() - suite_start) * 1000.0
    return ChaosSuiteReport(scenarios=tuple(results), total_duration_ms=total_ms)


def format_chaos_report(report: ChaosSuiteReport, *, verbose: bool = False) -> str:
    """Format a human-scannable ASCII summary table for ``sidefx eval``."""
    name_width = max((len(s.name) for s in report.scenarios), default=20)
    name_width = max(name_width, len("Scenario"))

    header = (
        f"{'Scenario':<{name_width}}  {'Status':<6}  {'Dupes':>5}  "
        f"{'Writes':>8}  {'Receipt':>8}  {'Time':>8}"
    )
    sep = "-" * len(header)
    lines = [
        "sidefx Chaos Fault-Injection Evaluation",
        sep,
        header,
        sep,
    ]
    for sc in report.scenarios:
        status_str = "PASS" if sc.passed else "FAIL"
        writes_str = f"{sc.actual_remote_writes}/{sc.expected_remote_writes}"
        receipt_str = (
            f"{sc.receipt_accuracy_pct:.1f}%" if sc.receipt_accuracy_pct is not None else "-"
        )
        time_str = f"{sc.duration_ms:.0f}ms"
        lines.append(
            f"{sc.name:<{name_width}}  {status_str:<6}  {sc.duplicate_remote_writes:>5}  "
            f"{writes_str:>8}  {receipt_str:>8}  {time_str:>8}"
        )
        if verbose:
            if sc.details:
                lines.append(f"  detail: {sc.details}")
            if sc.error:
                lines.append(f"  error:  {sc.error}")
            for chk in sc.subchecks:
                chk_status = "PASS" if chk.get("passed") else "FAIL"
                lines.append(f"  - [{chk_status}] {chk.get('check')}: {chk.get('detail', '')}")
        elif not sc.passed and sc.error:
            lines.append(f"  error:  {sc.error}")

    lines.append(sep)
    overall = "PASS" if report.passed else "FAIL"
    accuracy_str = (
        f"{report.receipt_accuracy_pct:.1f}%" if report.receipt_accuracy_pct is not None else "n/a"
    )
    dupes_str = (
        f"{report.duplicate_remote_writes} (incomplete)"
        if report.failed_scenarios > 0 and report.duplicate_remote_writes == 0
        else str(report.duplicate_remote_writes)
    )
    lines.append(
        f"Result: {overall} | "
        f"Scenarios: {report.passed_scenarios}/{report.total_scenarios} passed | "
        f"Duplicate remote writes: {dupes_str} | "
        f"Receipt accuracy: {accuracy_str} | "
        f"Total time: {report.total_duration_ms:.0f}ms"
    )
    return "\n".join(lines)


def _build_eval_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx eval",
        description=(
            "Run the automated chaos fault-injection evaluation suite across CLI wrap, "
            "GitHub & git push reconcilers, MCP proxy, preflight guardrails, and receipts."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON evaluation metrics.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Include per-subcheck breakdown in human-readable output.",
    )
    parser.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=available_scenario_names(),
        default=None,
        help="Run only the named scenario (repeatable; default runs all scenarios).",
    )
    return parser


def handle_eval_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler invoked by ``sidefx.cli`` for ``sidefx eval``."""
    del config  # Eval runs in fully isolated temporary workspaces.
    parser = _build_eval_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))

    report = run_chaos_suite(scenarios=sub_args.scenarios)
    if sub_args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_chaos_report(report, verbose=sub_args.verbose))
    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point for running the chaos evaluation suite directly."""
    parser = _build_eval_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    report = run_chaos_suite(scenarios=parsed.scenarios)
    if parsed.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_chaos_report(report, verbose=parsed.verbose))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
