"""Tests for PR #6 adoption polish features (items 1-8)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from sidefx.cli import main
from sidefx.config import (
    CONFIG_FILENAME,
    ConfigError,
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    load_config,
)
from sidefx.doctor import run_doctor
from sidefx.hook import run_claude_code_stop_hook
from sidefx.journal import ActionStatus, Journal, Surface
from sidefx.mcp_proxy import (
    ERR_OUTCOME_UNKNOWN,
    ERR_PREFLIGHT_DENIED,
    MCPProxy,
    _default_target_from_cmd,
    parse_mcp_proxy_args,
)
from sidefx.preflight import evaluate_preflight
from sidefx.session import (
    ENV_CLAUDE_CODE_SESSION_ID,
    ENV_SESSION_ID,
    ensure_persisted_session,
    read_current_session,
    resolve_session_id,
    session_file_path,
)
from sidefx.wrapper import wrap_command

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers for interactive stdio MCP proxy tests
# ---------------------------------------------------------------------------


class _InteractiveProxy:
    def __init__(
        self,
        *,
        cmd: list[str],
        journal: Journal,
        config: SidefxConfig | None = None,
        session_id: str | None = None,
        target: str | None = None,
        timeout_seconds: float = 5.0,
        deterministic_ids: bool = False,
        inject_instructions: bool = True,
        restart_on_timeout: bool = True,
        upstream_env: dict[str, str] | None = None,
    ) -> None:
        self._c_in_r, self._c_in_w = os.pipe()
        self._c_out_r, self._c_out_w = os.pipe()
        self._c_in_rf = os.fdopen(self._c_in_r, "rb", buffering=0)
        self._c_in_wf = os.fdopen(self._c_in_w, "wb", buffering=0)
        self._c_out_rf = os.fdopen(self._c_out_r, "rb", buffering=0)
        self._c_out_wf = os.fdopen(self._c_out_w, "wb", buffering=0)
        self.err_buf = io.BytesIO()
        self.proxy = MCPProxy(
            cmd,
            journal=journal,
            config=config,
            session_id=session_id,
            target=target,
            timeout_seconds=timeout_seconds,
            deterministic_ids=deterministic_ids,
            inject_instructions=inject_instructions,
            restart_on_timeout=restart_on_timeout,
            client_in=self._c_in_rf,
            client_out=self._c_out_wf,
            err_stream=self.err_buf,
            upstream_env=upstream_env,
        )
        self._thread = threading.Thread(target=self.proxy.run, daemon=True)

    def __enter__(self) -> _InteractiveProxy:
        self._thread.start()
        return self

    def send(self, msg: dict[str, Any]) -> None:
        self._c_in_wf.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._c_in_wf.flush()

    def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        box: list[bytes] = []

        def _read() -> None:
            box.append(self._c_out_rf.readline())

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout)
        assert not t.is_alive(), "Timed out waiting for proxy response"
        assert box and box[0], "EOF from proxy stdout"
        return json.loads(box[0].decode("utf-8"))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for fh in (self._c_in_wf,):
            with contextlib.suppress(OSError):
                fh.close()
        self._thread.join(timeout=5.0)
        for fh in (self._c_in_rf, self._c_out_wf, self._c_out_rf):
            with contextlib.suppress(OSError):
                fh.close()


SINGLE_THREADED_WEDGING_SERVER = r"""
import json
import os
import sys
import time
from pathlib import Path

log_path = Path(os.environ["WEDGE_LOG"])
pid = os.getpid()
initialized = False
saw_initialized_notif = False
upstream_instructions = os.environ.get("UPSTREAM_INSTRUCTIONS", "")
write_count = 0


def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def log_event(event):
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": pid, **event}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    has_id = "id" in msg

    if method == "initialize":
        initialized = True
        log_event({"event": "initialize", "params": msg.get("params")})
        res = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "strict-single-thread"}}
        if upstream_instructions:
            res["instructions"] = upstream_instructions
        send({"jsonrpc": "2.0", "id": req_id, "result": res})
        continue

    if method == "notifications/initialized":
        saw_initialized_notif = True
        log_event({"event": "notifications/initialized"})
        continue

    if method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        continue

    if method == "tools/call":
        if not (initialized and saw_initialized_notif):
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32002, "message": "Server not initialized"},
            })
            continue
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}

        if name == "get_ticket":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"ticket:{args.get('id')} pid={pid}"}],
                    "isError": False,
                },
            })
            continue

        if args.get("hang"):
            log_event({"event": "write_hang", "tool": name, "args": args})
            time.sleep(30.0)
            continue

        write_count += 1
        log_event({"event": "write", "tool": name, "args": args, "seq": write_count})
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"ok pid={pid} seq={write_count}"}],
                "structuredContent": {"pid": pid, "seq": write_count, "url": f"https://t/{pid}/{write_count}"},
                "isError": False,
            },
        })
        continue

    if has_id:
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })
"""


# ---------------------------------------------------------------------------
# Item 1: MCP `initialize` instructions injection
# ---------------------------------------------------------------------------


class TestInitializeInstructionsInjection:
    def test_injects_when_absent_preserves_existing_and_honors_disable_flag(
        self, tmp_path: Path
    ) -> None:
        server = tmp_path / "srv.py"
        server.write_text(SINGLE_THREADED_WEDGING_SERVER, encoding="utf-8")
        log_path = tmp_path / "log.jsonl"
        db_path = tmp_path / "journal.db"

        # 1. Absent upstream instructions -> creates `result.instructions`
        env1 = {**os.environ, "WEDGE_LOG": str(log_path)}
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                session_id="init-s1",
                deterministic_ids=True,
                upstream_env=env1,
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = h.recv()
            instr = resp["result"]["instructions"]
            assert "_sidefx_action_id" in instr
            assert "Duplicate call not re-sent" in instr
            assert "OUTCOME UNKNOWN" in instr
            assert "sidefx reconcile" in instr
            assert "sidefx resolve" in instr

        # 2. Existing upstream instructions -> preserved + sidefx guidance appended
        env2 = {
            **os.environ,
            "WEDGE_LOG": str(log_path),
            "UPSTREAM_INSTRUCTIONS": "Use concise titles.",
        }
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                session_id="init-s2",
                upstream_env=env2,
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = h.recv()
            instr = resp["result"]["instructions"]
            assert instr.startswith("Use concise titles.\n\n")
            assert "_sidefx_action_id" in instr

        # 3. `--no-inject-instructions` / `inject_instructions=False` -> raw upstream response
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                session_id="init-s3",
                inject_instructions=False,
                upstream_env=env1,
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            resp = h.recv()
            assert "instructions" not in resp["result"]

        parsed_default, _ = parse_mcp_proxy_args(["--", "echo", "hi"])
        assert parsed_default.inject_instructions is True
        parsed_no, _ = parse_mcp_proxy_args(["--no-inject-instructions", "--", "echo", "hi"])
        assert parsed_no.inject_instructions is False


# ---------------------------------------------------------------------------
# Item 2: Stable default session ID across proxy restarts + `sidefx session`
# ---------------------------------------------------------------------------


class TestStableSessionPersistence:
    def test_proxy_restarts_share_persisted_session_and_dedupe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)

        server = tmp_path / "srv.py"
        server.write_text(SINGLE_THREADED_WEDGING_SERVER, encoding="utf-8")
        log_path = tmp_path / "log.jsonl"
        db_path = tmp_path / ".sidefx" / "journal.db"
        env = {**os.environ, "WEDGE_LOG": str(log_path)}

        # Proxy instance #1 (no --session-id)
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                target="mcp:tickets",
                deterministic_ids=True,
                upstream_env=env,
            ) as h1,
        ):
            sess1 = h1.proxy.session_id
            assert read_current_session(db_path) == sess1
            h1.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            h1.recv()
            h1.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            h1.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "create_ticket", "arguments": {"title": "same"}},
                }
            )
            r1 = h1.recv()
            assert "error" not in r1

        # Proxy instance #2 (restarted, still no --session-id)
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                target="mcp:tickets",
                deterministic_ids=True,
                upstream_env=env,
            ) as h2,
        ):
            assert h2.proxy.session_id == sess1
            h2.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            h2.recv()
            h2.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            h2.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "create_ticket", "arguments": {"title": "same"}},
                }
            )
            r2 = h2.recv()
            assert "error" not in r2
            assert any(
                "Duplicate call not re-sent" in item.get("text", "")
                for item in r2["result"]["content"]
            )

        writes = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "write"
        ]
        assert len(writes) == 1

    def test_concurrent_session_creation_converges_and_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        db_path = tmp_path / ".sidefx" / "journal.db"

        barrier = threading.Barrier(6)
        results: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            sid = ensure_persisted_session(db_path)
            with lock:
                results.append(sid)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert len(results) == 6
        assert len(set(results)) == 1
        assert (db_path.parent / ".gitignore").is_file()

        # Precedence checks: explicit > SIDEFX_SESSION_ID > config > CLAUDE_CODE_SESSION_ID > file
        monkeypatch.setenv(ENV_CLAUDE_CODE_SESSION_ID, "cc-sess")
        assert resolve_session_id(db_path=db_path) == (
            "cc-sess",
            f"env:{ENV_CLAUDE_CODE_SESSION_ID}",
        )
        cfg = SidefxConfig(default_session_id="cfg-sess")
        assert resolve_session_id(config=cfg, db_path=db_path) == ("cfg-sess", "config")
        monkeypatch.setenv(ENV_SESSION_ID, "sidefx-env-sess")
        assert resolve_session_id(config=cfg, db_path=db_path) == (
            "sidefx-env-sess",
            f"env:{ENV_SESSION_ID}",
        )
        assert resolve_session_id(explicit="cli-sess", config=cfg, db_path=db_path) == (
            "cli-sess",
            "cli",
        )

    def test_session_cli_current_and_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        db_path = tmp_path / ".sidefx" / "journal.db"

        # Read-only current on empty workspace does not create files
        rc0 = main(["session", "current", "--db", str(db_path), "--json"])
        assert rc0 == 0
        out0 = json.loads(capsys.readouterr().out)
        assert out0["session_id"] is None
        assert out0["source"] == "none"
        assert not session_file_path(db_path).exists()

        # `session new` persists a generated session
        rc1 = main(["session", "new", "--db", str(db_path), "--json"])
        assert rc1 == 0
        out1 = json.loads(capsys.readouterr().out)
        first_id = out1["session_id"]
        assert first_id.startswith("sess-")
        assert out1["previous_session_id"] is None
        assert session_file_path(db_path).is_file()

        # `session current` reads the persisted ID
        rc2 = main(["session", "current", "--db", str(db_path)])
        assert rc2 == 0
        assert f"{first_id} (file)" in capsys.readouterr().out

        # `session new --id` rotates cleanly
        rc3 = main(["session", "new", "--db", str(db_path), "--id", "sprint-42", "--json"])
        assert rc3 == 0
        out3 = json.loads(capsys.readouterr().out)
        assert out3["session_id"] == "sprint-42"
        assert out3["previous_session_id"] == first_id


# ---------------------------------------------------------------------------
# Item 3: `required: true` preflight rules
# ---------------------------------------------------------------------------


class TestPreflightRequiredConstraint:
    def test_config_validation_and_roundtrip_for_required(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / CONFIG_FILENAME
        cfg_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "preflight": {
                        "rules": [
                            {
                                "tool": "refund_payment",
                                "arg": "amount",
                                "required": True,
                                "max": 50,
                                "reason": "Amount is required and capped at $50",
                            },
                            {"tool": "create_issue", "arg": "title", "required": True},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = load_config(cfg_file)
        assert loaded.preflight.rules[0].required is True
        assert loaded.preflight.rules[0].max_value == 50
        assert loaded.preflight.rules[1].required is True
        assert loaded.preflight.rules[0].to_dict()["required"] is True

        bad_cfg = tmp_path / "bad.json"
        bad_cfg.write_text(
            json.dumps(
                {
                    "version": 1,
                    "preflight": {
                        "rules": [{"tool": "refund_payment", "arg": "amount", "required": "yes"}]
                    },
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="must be a boolean"):
            load_config(bad_cfg)

    def test_required_blocks_missing_null_empty_list_and_nested_bypass(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="refund_payment",
                        arg="amount",
                        required=True,
                        max_value=50,
                        reason="Refund amount required and <= 50",
                    ),
                    PreflightRule(
                        tool="send_batch",
                        arg="recipients.email",
                        required=True,
                        allow_domains=("acme.com",),
                    ),
                )
            )
        )

        # Missing arg -> denied!
        r_missing = evaluate_preflight("refund_payment", {}, cfg)
        assert not r_missing.allowed
        assert r_missing.violations[0].constraint == "required"
        assert "Refund amount required" in r_missing.violations[0].message

        # Explicit null -> denied!
        r_null = evaluate_preflight("refund_payment", {"amount": None}, cfg)
        assert not r_null.allowed
        assert r_null.violations[0].constraint == "required"

        # Empty list -> denied!
        r_empty = evaluate_preflight("refund_payment", {"amount": []}, cfg)
        assert not r_empty.allowed
        assert r_empty.violations[0].constraint == "required"

        # Empty string and empty dict -> denied by required!
        r_empty_str = evaluate_preflight("refund_payment", {"amount": "   "}, cfg)
        assert not r_empty_str.allowed
        assert r_empty_str.violations[0].constraint == "required"

        r_empty_dict = evaluate_preflight("refund_payment", {"amount": {}}, cfg)
        assert not r_empty_dict.allowed
        assert r_empty_dict.violations[0].constraint == "required"

        # Literal dotted keys MUST NOT bypass `required: true` on nested paths!
        dotted_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="refund_payment",
                        arg="payment.amount",
                        required=True,
                        max_value=50,
                    ),
                )
            )
        )
        r_flat_only = evaluate_preflight("refund_payment", {"payment.amount": 1}, dotted_cfg)
        assert not r_flat_only.allowed
        assert any(v.constraint == "required" for v in r_flat_only.violations)

        r_flat_empty_parent = evaluate_preflight(
            "refund_payment", {"payment.amount": 1, "payment": {}}, dotted_cfg
        )
        assert not r_flat_empty_parent.allowed
        assert any(v.constraint == "required" for v in r_flat_empty_parent.violations)

        assert evaluate_preflight("refund_payment", {"payment": {"amount": 20}}, dotted_cfg).allowed

        # Valid value -> allowed!
        assert evaluate_preflight("refund_payment", {"amount": 25}, cfg).allowed

        # Exceeds max -> denied by max!
        r_max = evaluate_preflight("refund_payment", {"amount": 75}, cfg)
        assert not r_max.allowed
        assert r_max.violations[0].constraint == "max"

        # Nested collection missing element -> denied!
        r_nested = evaluate_preflight(
            "send_batch",
            {"recipients": [{"email": "alice@acme.com"}, {"name": "Bob"}]},
            cfg,
        )
        assert not r_nested.allowed
        assert r_nested.violations[0].constraint == "required"
        assert r_nested.violations[0].arg == "recipients[1].email"

    def test_mcp_proxy_enforces_required_rule_before_dispatch(self, tmp_path: Path) -> None:
        server = tmp_path / "srv.py"
        server.write_text(SINGLE_THREADED_WEDGING_SERVER, encoding="utf-8")
        log_path = tmp_path / "log.jsonl"
        db_path = tmp_path / "journal.db"
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(tool="refund_payment", arg="amount", required=True, max_value=50),
                )
            )
        )
        env = {**os.environ, "WEDGE_LOG": str(log_path)}
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                config=cfg,
                session_id="req-sess",
                upstream_env=env,
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            h.recv()
            h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "refund_payment", "arguments": {}},
                }
            )
            resp = h.recv()
            assert resp["error"]["code"] == ERR_PREFLIGHT_DENIED
            assert "Required argument 'amount' is missing" in resp["error"]["message"]
            assert journal.list_actions() == []


# ---------------------------------------------------------------------------
# Item 4: `sidefx hook claude-code` & session-scoped `sidefx report`
# ---------------------------------------------------------------------------


class TestClaudeCodeHookAndSessionScopedReport:
    def test_hook_silent_on_zero_actions_missing_db_and_malformed_stdin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        # Missing DB -> silent
        assert (
            run_claude_code_stop_hook(
                stdin=io.StringIO(json.dumps({"session_id": "s1", "cwd": str(tmp_path)})),
            )
            is None
        )
        assert not (tmp_path / ".sidefx").exists()

        # Populate session s1 only
        db_path = tmp_path / ".sidefx" / "journal.db"
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="a1",
                session_id="s1",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"x": 1},
            )
            journal.mark_confirmed("agent", "org/repo", "a1", result_ref="https://gh/1")

        # Session s2 has 0 actions -> completely silent!
        assert (
            run_claude_code_stop_hook(
                stdin=io.StringIO(json.dumps({"session_id": "s2", "cwd": str(tmp_path)})),
            )
            is None
        )

        # Malformed stdin & closed stdin -> fail-open silent!
        assert run_claude_code_stop_hook(stdin=io.StringIO("{not valid json")) is None
        assert run_claude_code_stop_hook(stdin=io.StringIO("[1, 2, 3]")) is None
        closed_stream = io.StringIO("{}")
        closed_stream.close()
        assert run_claude_code_stop_hook(stdin=closed_stream) is None

    def test_hook_matches_writer_session_surfaces_other_session_unresolved_and_never_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        db_path = tmp_path / ".sidefx" / "journal.db"

        # 1. Writer uses SIDEFX_SESSION_ID=s-env; hook payload has different session_id="cc-other"
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="env-act-1",
                session_id="s-env",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="org/repo",
                payload={"x": 1},
            )
            journal.mark_confirmed("agent", "org/repo", "env-act-1", result_ref="https://gh/1")

        monkeypatch.setenv(ENV_SESSION_ID, "s-env")
        res_env = run_claude_code_stop_hook(
            stdin=io.StringIO(json.dumps({"session_id": "cc-other", "cwd": str(tmp_path)})),
        )
        assert res_env is not None
        assert "env-act-1" in res_env["systemMessage"]
        monkeypatch.delenv(ENV_SESSION_ID)

        # 2. Writer used file session (`current-session`); hook payload has cc-other -> finds it!
        assert main(["session", "new", "--db", str(db_path), "--id", "s-env"]) == 0
        _ = capsys.readouterr()
        res_file = run_claude_code_stop_hook(
            stdin=io.StringIO(json.dumps({"session_id": "cc-other", "cwd": str(tmp_path)})),
        )
        assert res_file is not None
        assert "env-act-1" in res_file["systemMessage"]

        # 3. After `/clear`: CLAUDE_CODE_SESSION_ID moved to `cc-new`, while old session `cc-old`
        #    left an `unknown` action -> hook MUST surface it loudly AND report warns about it!
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="orphan-unk",
                session_id="cc-old",
                actor="agent",
                surface=Surface.MCP,
                tool_name="post_webhook",
                target="mcp:hooks",
                payload={"x": 2},
            )
            journal.mark_unknown("agent", "mcp:hooks", "orphan-unk", error="timed out")

        monkeypatch.setenv(ENV_CLAUDE_CODE_SESSION_ID, "cc-new")
        res_clear = run_claude_code_stop_hook(
            stdin=io.StringIO(json.dumps({"session_id": "cc-new", "cwd": str(tmp_path)})),
        )
        assert res_clear is not None
        assert "WARNING:" in res_clear["systemMessage"]
        assert "orphan-unk" in res_clear["systemMessage"]
        assert "cc-old" in res_clear["systemMessage"]

        assert main(["report", "--db", str(db_path), "--session-id", "cc-new", "--json"]) == 0
        rep_json = json.loads(capsys.readouterr().out)
        assert rep_json["other_sessions_unresolved_count"] == 1
        assert main(["report", "--db", str(db_path), "--session-id", "cc-new"]) == 0
        assert "other session" in capsys.readouterr().out

        # 4. Bad CLI flags / unknown harness on `sidefx hook` MUST still exit 0 (never block Stop!)
        assert main(["hook", "claude-code", "--totally-unknown-flag"]) == 0
        assert main(["hook", "unknown-harness"]) == 0
        _ = capsys.readouterr()

    def test_hook_emits_system_message_and_highlights_unresolved_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        db_path = tmp_path / ".sidefx" / "journal.db"
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="ok-1",
                session_id="cc-sess-ok",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin:refs/heads/main",
                payload={"x": 1},
            )
            journal.mark_confirmed("agent", "origin:refs/heads/main", "ok-1", result_ref="ref@123")

            journal.begin_action(
                action_id="other-ok",
                session_id="cc-sess-other",
                actor="agent",
                surface=Surface.CLI,
                tool_name="git_push",
                target="origin:refs/heads/other",
                payload={"x": 9},
            )
            journal.mark_confirmed(
                "agent", "origin:refs/heads/other", "other-ok", result_ref="ref@999"
            )

        # Confirmed-only journal -> clean session-scoped systemMessage without WARNING
        res_ok = run_claude_code_stop_hook(
            stdin=io.StringIO(json.dumps({"session_id": "cc-sess-ok", "cwd": str(tmp_path)})),
        )
        assert res_ok is not None
        assert "systemMessage" in res_ok
        assert "ok-1" in res_ok["systemMessage"]
        assert "other-ok" not in res_ok["systemMessage"]
        assert "WARNING:" not in res_ok["systemMessage"]

        # Add an unresolved action in cc-sess-bad
        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="unk-1",
                session_id="cc-sess-bad",
                actor="agent",
                surface=Surface.MCP,
                tool_name="post_webhook",
                target="mcp:hooks",
                payload={"x": 2},
            )
            journal.mark_unknown("agent", "mcp:hooks", "unk-1", error="timed out")

        # Unresolved session via CLI `sidefx hook claude-code` -> loud WARNING banner
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"session_id": "cc-sess-bad", "cwd": str(tmp_path)})),
        )
        rc = main(["hook", "claude-code"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        msg = payload["systemMessage"]
        assert "WARNING:" in msg
        assert "UNRESOLVED" in msg
        assert "unk-1" in msg
        assert "sidefx reconcile" in msg
        assert "sidefx resolve" in msg

    def test_report_defaults_to_active_session_and_supports_all_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)
        db_path = tmp_path / ".sidefx" / "journal.db"
        with Journal(db_path) as journal:
            for sid, aid in (("sess-old", "act-old"), ("sess-cur", "act-cur")):
                journal.begin_action(
                    action_id=aid,
                    session_id=sid,
                    actor="agent",
                    surface=Surface.MCP,
                    tool_name="create_item",
                    target="mcp:test",
                    payload={"id": aid},
                )
                journal.mark_confirmed("agent", "mcp:test", aid, result_ref=f"https://x/{aid}")

        assert main(["session", "new", "--db", str(db_path), "--id", "sess-cur"]) == 0
        _ = capsys.readouterr()

        # Default `sidefx report` scopes to `sess-cur`
        assert main(["report", "--db", str(db_path), "--json"]) == 0
        cur_data = json.loads(capsys.readouterr().out)
        assert cur_data["session_id"] == "sess-cur"
        assert [a["action_id"] for a in cur_data["actions"]] == ["act-cur"]

        # `--all` overrides active session to include everything
        assert main(["report", "--db", str(db_path), "--all", "--json"]) == 0
        all_data = json.loads(capsys.readouterr().out)
        assert all_data["session_id"] is None
        assert {a["action_id"] for a in all_data["actions"]} == {"act-old", "act-cur"}


# ---------------------------------------------------------------------------
# Item 5: Readable default MCP target names
# ---------------------------------------------------------------------------


class TestReadableDefaultTargets:
    @pytest.mark.parametrize(
        ("cmd", "expected_prefix"),
        [
            (["npx", "-y", "@modelcontextprotocol/server-github"], "mcp:server-github:"),
            (
                ["npx", "-y", "@modelcontextprotocol/server-filesystem@0.6.2", "."],
                "mcp:server-filesystem:",
            ),
            (["uvx", "mcp-server-git", "--repository", "."], "mcp:mcp-server-git:"),
            (["uv", "run", "python", "github_mcp.py"], "mcp:github_mcp:"),
            (["node", "/opt/mcp/linear-server/index.js"], "mcp:linear-server:"),
            (
                ["python3", "-u", "-m", "mcp_server_sqlite", "--db", "a.db"],
                "mcp:mcp_server_sqlite:",
            ),
            (
                ["docker", "run", "-i", "--rm", "-e", "TOK", "ghcr.io/github/github-mcp-server:v1"],
                "mcp:github-mcp-server:",
            ),
        ],
    )
    def test_launcher_unwrapping(self, cmd: list[str], expected_prefix: str) -> None:
        target = _default_target_from_cmd(cmd)
        assert target.startswith(expected_prefix), f"got {target!r}"
        suffix = target.removeprefix(expected_prefix)
        assert len(suffix) == 8

    def test_distinct_args_produce_distinct_target_hashes(self) -> None:
        t1 = _default_target_from_cmd(
            ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/a"]
        )
        t2 = _default_target_from_cmd(
            ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/b"]
        )
        assert t1.startswith("mcp:server-filesystem:")
        assert t2.startswith("mcp:server-filesystem:")
        assert t1 != t2


# ---------------------------------------------------------------------------
# Item 6: Wedged single-threaded upstream recovery
# ---------------------------------------------------------------------------


class TestWedgedUpstreamTimeoutRecovery:
    def test_unrelated_calls_succeed_after_single_threaded_wedge_timeout(
        self, tmp_path: Path
    ) -> None:
        server = tmp_path / "strict_single.py"
        server.write_text(SINGLE_THREADED_WEDGING_SERVER, encoding="utf-8")
        log_path = tmp_path / "wedge.jsonl"
        db_path = tmp_path / "journal.db"
        env = {**os.environ, "WEDGE_LOG": str(log_path)}

        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(server)],
                journal=journal,
                session_id="wedge-sess",
                target="mcp:strict",
                timeout_seconds=0.2,
                restart_on_timeout=True,
                upstream_env=env,
            ) as h,
        ):
            # Initial handshake
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "test"}},
                }
            )
            init_resp = h.recv()
            assert init_resp["result"]["serverInfo"]["name"] == "strict-single-thread"
            h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            # First mutation works on PID 1
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {"title": "before", "_sidefx_action_id": "act-before"},
                    },
                }
            )
            r_before = h.recv()
            pid1 = r_before["result"]["structuredContent"]["pid"]

            # Second mutation wedges the single-threaded server for 30s -> times out at 0.2s!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {
                            "title": "hang forever",
                            "hang": True,
                            "_sidefx_action_id": "act-wedged",
                        },
                    },
                }
            )
            r_timeout = h.recv(timeout=2.0)
            assert r_timeout["id"] == 3
            assert r_timeout["error"]["code"] == ERR_OUTCOME_UNKNOWN

            rec_wedged = journal.get_action("agent", "mcp:strict", "act-wedged")
            assert rec_wedged is not None
            assert rec_wedged.status == ActionStatus.UNKNOWN

            # Unrelated read-only call MUST succeed cleanly after restart + handshake replay!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "get_ticket", "arguments": {"id": 99}},
                }
            )
            r_read = h.recv(timeout=5.0)
            assert r_read["id"] == 4
            assert "error" not in r_read
            assert "ticket:99" in r_read["result"]["content"][0]["text"]
            assert f"pid={pid1}" not in r_read["result"]["content"][0]["text"]

            # Unrelated mutating call MUST also succeed on the fresh upstream PID!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {"title": "after", "_sidefx_action_id": "act-after"},
                    },
                }
            )
            r_after = h.recv(timeout=5.0)
            assert r_after["id"] == 5
            assert "error" not in r_after
            pid2 = r_after["result"]["structuredContent"]["pid"]
            assert pid2 != pid1

            # Retrying the timed-out action is STILL blocked with OUTCOME UNKNOWN (0 duplicates!)
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {
                            "title": "hang forever",
                            "hang": True,
                            "_sidefx_action_id": "act-wedged",
                        },
                    },
                }
            )
            r_retry = h.recv(timeout=2.0)
            assert r_retry["id"] == 6
            assert r_retry["error"]["code"] == ERR_OUTCOME_UNKNOWN

        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        hang_writes = [e for e in events if e.get("event") == "write_hang"]
        assert len(hang_writes) == 1
        inits = [e for e in events if e.get("event") == "initialize"]
        assert len(inits) == 2

        parsed_default, _ = parse_mcp_proxy_args(["--", "echo", "hi"])
        assert parsed_default.restart_on_timeout is False
        parsed_yes, _ = parse_mcp_proxy_args(["--restart-on-timeout", "--", "echo", "hi"])
        assert parsed_yes.restart_on_timeout is True

    def test_restart_with_shell_grandchild_and_orphaned_passthrough_does_not_deadlock(
        self, tmp_path: Path
    ) -> None:
        server = tmp_path / "strict_single.py"
        server.write_text(SINGLE_THREADED_WEDGING_SERVER, encoding="utf-8")
        log_path = tmp_path / "grandchild.jsonl"
        db_path = tmp_path / "journal.db"
        env = {**os.environ, "WEDGE_LOG": str(log_path)}

        # Run via `sh -c "...; true"` so the MCP server is a GRANDCHILD process holding stdout open!
        shell_cmd = ["sh", "-c", f'"{sys.executable}" -u "{server}"; true']
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=shell_cmd,
                journal=journal,
                session_id="gc-sess",
                target="mcp:gc",
                timeout_seconds=0.2,
                restart_on_timeout=True,
                upstream_env=env,
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert h.recv()["id"] == 1
            h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            # Wedge the grandchild server, then queue a passthrough read BEFORE timeout fires
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_ticket",
                        "arguments": {"hang": True, "_sidefx_action_id": "gc-wedge"},
                    },
                }
            )
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_ticket", "arguments": {"id": 7}},
                }
            )

            # Collect timeout error for id=2
            r_timeout = h.recv(timeout=2.0)
            assert r_timeout["id"] == 2
            assert r_timeout["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Next call triggers restart: must kill grandchild tree without deadlocking,
            # synthesize an error for orphaned passthrough id=3, and answer id=4 cleanly!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "get_ticket", "arguments": {"id": 8}},
                }
            )
            r_a = h.recv(timeout=5.0)
            r_b = h.recv(timeout=5.0)
            by_id = {r_a["id"]: r_a, r_b["id"]: r_b}
            assert 3 in by_id and "error" in by_id[3]
            assert 4 in by_id and "error" not in by_id[4]
            assert "ticket:8" in by_id[4]["result"]["content"][0]["text"]

    def test_slow_serial_server_reconciles_late_reply_by_default_without_kill(
        self, tmp_path: Path
    ) -> None:
        slow_srv = tmp_path / "slow_serial.py"
        slow_srv.write_text(
            textwrap.dedent(
                """\
                import json, sys, time
                for raw in sys.stdin:
                    msg = json.loads(raw)
                    m = msg.get("method")
                    rid = msg.get("id")
                    if m == "initialize":
                        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}), flush=True)
                    elif m == "tools/call":
                        name = msg["params"]["name"]
                        if name == "create_item":
                            time.sleep(0.35)
                            res = {"content": [{"type": "text", "text": "https://x/1"}],
                                   "isError": False}
                            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}),
                                  flush=True)
                        else:
                            res = {"content": [{"type": "text", "text": "read-ok"}],
                                   "isError": False}
                            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}),
                                  flush=True)
                """
            ),
            encoding="utf-8",
        )
        db_path = tmp_path / "journal.db"
        with (
            Journal(db_path) as journal,
            _InteractiveProxy(
                cmd=[sys.executable, "-u", str(slow_srv)],
                journal=journal,
                session_id="slow-serial-sess",
                target="mcp:slow",
                timeout_seconds=0.15,
                restart_on_timeout=False,  # default!
            ) as h,
        ):
            h.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            h.recv()
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "create_item", "arguments": {"_sidefx_action_id": "s-1"}},
                }
            )
            r_to = h.recv(timeout=2.0)
            assert r_to["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Send read immediately: server is NOT killed, finishes write, reconciles, answers read!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_item", "arguments": {}},
                }
            )
            r_read = h.recv(timeout=3.0)
            assert r_read["id"] == 3
            assert r_read["result"]["content"][0]["text"] == "read-ok"

            rec = journal.get_action("agent", "mcp:slow", "s-1")
            assert rec is not None
            assert rec.status == ActionStatus.RECONCILED
            assert rec.result_ref == "https://x/1"


# ---------------------------------------------------------------------------
# Item 7: Automatic deterministic action IDs in `sidefx wrap`
# ---------------------------------------------------------------------------


class TestWrapAutomaticDeterministicIds:
    def test_git_push_auto_id_reconciles_on_retry_and_changes_on_new_commit(
        self, tmp_path: Path
    ) -> None:
        bare = tmp_path / "remote.git"
        work = tmp_path / "work"
        slow_flag = tmp_path / "slow.flag"
        push_log = tmp_path / "pushes.log"
        db_path = tmp_path / "journal.db"

        git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def run_git(*git_args: str, cwd: Path) -> str:
            proc = subprocess.run(
                ["git", *git_args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=git_env,
                check=True,
            )
            return proc.stdout.strip()

        run_git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
        run_git("init", "-b", "main", str(work), cwd=tmp_path)
        run_git("config", "user.name", "Test", cwd=work)
        run_git("config", "user.email", "test@example.com", cwd=work)
        run_git("config", "commit.gpgsign", "false", cwd=work)
        run_git("remote", "add", "origin", str(bare), cwd=work)
        (work / "a.txt").write_text("v1\n", encoding="utf-8")
        run_git("add", "a.txt", cwd=work)
        run_git("commit", "-m", "init", cwd=work)
        run_git("push", "-u", "origin", "main", cwd=work)

        post_hook = bare / "hooks" / "post-receive"
        post_hook.write_text(
            f'#!/bin/sh\ncat >> "{push_log}"\nif [ -f "{slow_flag}" ]; then sleep 4; fi\n',
            encoding="utf-8",
        )
        post_hook.chmod(0o755)

        run_git("checkout", "-b", "feat/auto", cwd=work)
        (work / "b.txt").write_text("v1\n", encoding="utf-8")
        run_git("add", "b.txt", cwd=work)
        run_git("commit", "-m", "feat 1", cwd=work)

        slow_flag.write_text("1", encoding="utf-8")
        with Journal(db_path) as journal:
            # 1. Push without --action-id times out after remote received commit
            r1 = wrap_command(
                ["git", "push", "origin", "feat/auto"],
                journal=journal,
                cwd=work,
                env=git_env,
                timeout_seconds=0.8,
            )
            assert r1.exit_code == 124
            assert r1.action_id.startswith("git-push-")
            assert r1.status == ActionStatus.UNKNOWN

            # 2. Retry WITHOUT --action-id derives the SAME deterministic ID & reconciles!
            slow_flag.unlink()
            r2 = wrap_command(
                ["git", "push", "origin", "feat/auto"],
                journal=journal,
                cwd=work,
                env=git_env,
                timeout_seconds=5.0,
            )
            assert r2.exit_code == 0
            assert r2.action_id == r1.action_id
            assert r2.reconciled is True
            assert r2.executed is False

            # 3. Same SHA with different flags (`git push -u origin feat/auto`) replays cleanly
            #    without a spurious PayloadMismatchError!
            r_flags = wrap_command(
                ["git", "push", "-u", "origin", "feat/auto"],
                journal=journal,
                cwd=work,
                env=git_env,
                timeout_seconds=5.0,
            )
            assert r_flags.exit_code == 0
            assert r_flags.replayed is True
            assert r_flags.executed is False

            # 4. Delete remote branch, then re-push same SHA -> MUST execute real push (not skip!)
            run_git("push", "origin", "--delete", "feat/auto", cwd=work)
            r_repush = wrap_command(
                ["git", "push", "origin", "feat/auto"],
                journal=journal,
                cwd=work,
                env=git_env,
                timeout_seconds=5.0,
            )
            assert r_repush.exit_code == 0
            assert r_repush.executed is True
            assert r_repush.replayed is False
            ls_out = run_git("ls-remote", str(bare), "refs/heads/feat/auto", cwd=work)
            assert "refs/heads/feat/auto" in ls_out

            # 5. New commit on same branch -> NEW deterministic action ID and pushes cleanly!
            (work / "b.txt").write_text("v2\n", encoding="utf-8")
            run_git("add", "b.txt", cwd=work)
            run_git("commit", "-m", "feat 2", cwd=work)
            r3 = wrap_command(
                ["git", "push", "origin", "feat/auto"],
                journal=journal,
                cwd=work,
                env=git_env,
                timeout_seconds=5.0,
            )
            assert r3.exit_code == 0
            assert r3.executed is True
            assert r3.action_id != r1.action_id

        # 3 pushes landed (feat 1, repush after delete, feat 2)
        non_delete_lines = [
            ln
            for ln in push_log.read_text(encoding="utf-8").splitlines()
            if "feat/auto" in ln and not ln.split()[1].startswith("00000000")
        ]
        assert len(non_delete_lines) == 3

    def test_gh_auto_id_handles_fill_body_file_and_pr_branch_changes(self, tmp_path: Path) -> None:
        from sidefx.wrapper import _derive_gh_auto_context

        repo = tmp_path / "repo"
        repo.mkdir()
        git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        subprocess.run(["git", "init", "-b", "feat-a"], cwd=str(repo), env=git_env, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), env=git_env, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@e"], cwd=str(repo), env=git_env, check=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"], cwd=str(repo), env=git_env, check=True
        )
        (repo / "f.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=str(repo), env=git_env, check=True)
        subprocess.run(["git", "commit", "-m", "a"], cwd=str(repo), env=git_env, check=True)

        # `--fill` and `--body-file -` fall back to random (None)
        assert (
            _derive_gh_auto_context(["gh", "pr", "create", "--fill"], session_id="s", cwd=repo)
            is None
        )
        assert (
            _derive_gh_auto_context(
                ["gh", "pr", "comment", "7", "--body-file", "-"], session_id="s", cwd=repo
            )
            is None
        )

        # `--body-file` content changes produce a distinct action ID!
        body_file = repo / "body.md"
        body_file.write_text("version 1", encoding="utf-8")
        ctx_b1 = _derive_gh_auto_context(
            ["gh", "pr", "comment", "7", "--body-file", "body.md"], session_id="s", cwd=repo
        )
        body_file.write_text("version 2", encoding="utf-8")
        ctx_b2 = _derive_gh_auto_context(
            ["gh", "pr", "comment", "7", "--body-file", "body.md"], session_id="s", cwd=repo
        )
        assert ctx_b1 is not None and ctx_b2 is not None
        assert ctx_b1[0] != ctx_b2[0]

        # `gh pr create` without `--head` distinguishes branch `feat-a` from `feat-b`!
        pr_cmd = ["gh", "pr", "create", "--title", "WIP", "--body", "hi"]
        ctx_a = _derive_gh_auto_context(pr_cmd, session_id="s", cwd=repo)
        subprocess.run(["git", "checkout", "-b", "feat-b"], cwd=str(repo), env=git_env, check=True)
        ctx_b = _derive_gh_auto_context(pr_cmd, session_id="s", cwd=repo)
        assert ctx_a is not None and ctx_b is not None
        assert ctx_a[0] != ctx_b[0]

    def test_gh_issue_create_auto_id_reconciles_and_random_action_id_allows_duplicate(
        self, tmp_path: Path
    ) -> None:
        issues: list[dict[str, Any]] = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                with lock:
                    num = len(issues) + 1
                    item = {
                        "number": num,
                        "title": body.get("title", ""),
                        "body": body.get("body", ""),
                        "state": "open",
                        "html_url": f"https://github.com/org/repo/issues/{num}",
                    }
                    issues.append(item)
                out = json.dumps(item).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                _ = parse_qs(parsed.query)
                with lock:
                    out = json.dumps(list(reversed(issues))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"

        gh_script = tmp_path / "gh"
        gh_script.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, os, sys, time, urllib.request
                args = sys.argv[1:]
                title = args[args.index("--title") + 1]
                body = args[args.index("--body") + 1]
                req = urllib.request.Request(
                    os.environ["API"] + "/repos/org/repo/issues",
                    data=json.dumps({{"title": title, "body": body}}).encode(),
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    item = json.loads(resp.read().decode())
                if os.environ.get("MODE") == "timeout_after":
                    time.sleep(5)
                    sys.exit(1)
                print(item["html_url"])
                """
            ),
            encoding="utf-8",
        )
        gh_script.chmod(gh_script.stat().st_mode | stat.S_IXUSR)

        db_path = tmp_path / "journal.db"
        cmd = [
            str(gh_script),
            "issue",
            "create",
            "-R",
            "org/repo",
            "--title",
            "Auto",
            "--body",
            "Hi",
        ]
        try:
            with Journal(db_path) as journal:
                r1 = wrap_command(
                    cmd,
                    journal=journal,
                    session_id="gh-auto-sess",
                    env={**os.environ, "API": base_url, "MODE": "timeout_after"},
                    github_api_url=base_url,
                    timeout_seconds=0.6,
                )
                assert r1.exit_code == 124
                assert r1.action_id.startswith("gh-issue-create-")

                # Retry without --action-id (even with `--repo=org/repo` form!) -> reconciles!
                cmd_alt = [
                    str(gh_script),
                    "issue",
                    "create",
                    "--repo=org/repo",
                    "--title=Auto",
                    "--body=Hi",
                ]
                r2 = wrap_command(
                    cmd_alt,
                    journal=journal,
                    session_id="gh-auto-sess",
                    env={**os.environ, "API": base_url, "MODE": "normal"},
                    github_api_url=base_url,
                    timeout_seconds=5.0,
                )
                assert r2.exit_code == 0
                assert r2.action_id == r1.action_id
                assert r2.reconciled is True
                assert r2.executed is False
                assert len(issues) == 1

                # Explicit `--random-action-id` creates an intentional duplicate!
                r3 = wrap_command(
                    cmd,
                    journal=journal,
                    random_action_id=True,
                    session_id="gh-auto-sess",
                    env={**os.environ, "API": base_url, "MODE": "normal"},
                    github_api_url=base_url,
                    timeout_seconds=5.0,
                )
                assert r3.exit_code == 0
                assert r3.executed is True
                assert r3.action_id != r1.action_id
                assert len(issues) == 2

                with pytest.raises(ValueError, match="both action_id and random_action_id"):
                    wrap_command(cmd, journal=journal, action_id="x", random_action_id=True)
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------------------
# Item 8: Plugin packaging, SKILL.md, `bin/sidefx`, and `sidefx doctor`
# ---------------------------------------------------------------------------


class TestPluginPackagingAndDoctor:
    def test_claude_plugin_files_skill_and_launcher_are_valid(self) -> None:
        plugin_json = json.loads(
            (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert plugin_json["name"] == "sidefx"
        assert "hooks" not in plugin_json  # auto-loaded from hooks/hooks.json
        assert plugin_json["skills"] == "./skills/"

        marketplace = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        assert marketplace["name"] == "sidefx"
        assert any(p["name"] == "sidefx" for p in marketplace["plugins"])

        hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        stop_cmd = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert stop_cmd == '"${CLAUDE_PLUGIN_ROOT}/bin/sidefx" hook claude-code'

        skill_md = (REPO_ROOT / "skills" / "sidefx" / "SKILL.md").read_text(encoding="utf-8")
        assert skill_md.startswith("---\nname: sidefx\n")
        assert "OUTCOME UNKNOWN" in skill_md
        assert "_sidefx_action_id" in skill_md

        assert (REPO_ROOT / "src" / "sidefx" / "py.typed").is_file()

        launcher = REPO_ROOT / "bin" / "sidefx"
        assert launcher.is_file()
        assert os.access(launcher, os.X_OK)
        proc = subprocess.run(
            [str(launcher), "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert proc.returncode == 0
        assert "sidefx " in proc.stdout

    def test_launcher_skips_old_python3_and_falls_through_to_path_sidefx(
        self, tmp_path: Path
    ) -> None:
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()
        # Fake python3 pretending to be Python 3.9 (exits 1 on version check)
        for pyname in ("python3", "python3.14", "python3.13", "python3.12", "python3.11"):
            fake_py = fake_bin / pyname
            fake_py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_py.chmod(0o755)
        fake_sidefx = fake_bin / "sidefx"
        fake_sidefx.write_text('#!/bin/sh\necho "fallback-sidefx $@"\n', encoding="utf-8")
        fake_sidefx.chmod(0o755)

        proc = subprocess.run(
            ["/bin/sh", str(REPO_ROOT / "bin" / "sidefx"), "--version"],
            env={"PATH": f"{fake_bin}:/bin:/usr/bin"},
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert proc.returncode == 0
        assert "fallback-sidefx --version" in proc.stdout

    def test_doctor_checks_healthy_legacy_and_broken_configs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv(ENV_SESSION_ID, raising=False)
        monkeypatch.delenv(ENV_CLAUDE_CODE_SESSION_ID, raising=False)

        # Fresh repo -> doctor is read-only (does not create .sidefx/) and exits 0 (warns only)
        rc0 = main(["doctor", "--json"])
        assert rc0 == 0
        rep0 = json.loads(capsys.readouterr().out)
        assert rep0["ok"] is True
        by_name0 = {c["name"]: c for c in rep0["checks"]}
        assert by_name0["python"]["status"] == "ok"
        assert by_name0["config"]["status"] == "warn"
        assert by_name0["journal"]["status"] == "ok"
        assert by_name0["claude_code_hook"]["status"] == "warn"
        assert not (tmp_path / ".sidefx").exists()

        # Init config + wire modern Claude Code hook -> config & hook become OK
        assert main(["init"]) == 0
        assert main(["init", "--claude-code", "--write"]) == 0
        _ = capsys.readouterr()

        rc1 = main(["doctor", "--json"])
        assert rc1 == 0
        rep1 = json.loads(capsys.readouterr().out)
        by_name1 = {c["name"]: c for c in rep1["checks"]}
        assert by_name1["config"]["status"] == "ok"
        assert by_name1["claude_code_hook"]["status"] == "ok"

        # Legacy Stop hook -> warns with legacy=True
        legacy_settings = tmp_path / "legacy-settings.json"
        legacy_settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "sidefx report --format markdown",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        rep_legacy = run_doctor(settings_path=legacy_settings, cwd=tmp_path)
        by_legacy = {c["name"]: c for c in rep_legacy["checks"]}
        assert by_legacy["claude_code_hook"]["status"] == "warn"
        assert by_legacy["claude_code_hook"]["details"]["legacy"] is True

        # Broken `.sidefx.json` -> doctor still runs cleanly (doesn't crash) and exits 1 with fail
        (tmp_path / CONFIG_FILENAME).write_text("{broken json", encoding="utf-8")
        rc_bad = main(["doctor", "--json"])
        assert rc_bad == 1
        rep_bad = json.loads(capsys.readouterr().out)
        assert rep_bad["ok"] is False
        by_bad = {c["name"]: c for c in rep_bad["checks"]}
        assert by_bad["config"]["status"] == "fail"
