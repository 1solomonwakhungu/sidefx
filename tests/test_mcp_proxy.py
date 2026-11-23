"""Integration and unit tests for the transparent stdio JSON-RPC 2.0 MCP proxy."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sidefx.config import (
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    ToolClassificationConfig,
    UnknownToolPolicy,
)
from sidefx.journal import ActionRecord, ActionStatus, Journal, Surface
from sidefx.mcp_proxy import (
    ERR_OUTCOME_UNKNOWN,
    ERR_PAYLOAD_MISMATCH,
    ERR_PREFLIGHT_DENIED,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    OUTCOME_UNKNOWN_BANNER,
    MCPProxy,
    ReconcileHookResult,
    ReconcileState,
    parse_mcp_proxy_args,
)

MOCK_MCP_SERVER_CODE = r"""
import json
import os
import sys
import threading
import time
from pathlib import Path

log_path = Path(os.environ["MOCK_MCP_LOG"])
counter_lock = threading.Lock()
call_counter = 0
write_lock = threading.Lock()


def send(obj):
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n"
    with write_lock:
        sys.stdout.write(data)
        sys.stdout.flush()


def record_mutation(tool_name, arguments):
    global call_counter
    with counter_lock:
        call_counter += 1
        seq = call_counter
    entry = {"seq": seq, "tool": tool_name, "arguments": arguments}
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return seq


def handle_message(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    has_id = "id" in msg

    # Handle client responses to server-initiated requests (e.g. roots/list)
    if method is None and has_id:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"client_reply_id": req_id, "result": msg.get("result")}) + "\n")
            fh.flush()
        return

    if method == "initialize":
        sys.stderr.write("mock-mcp-server initialized on stderr\n")
        sys.stderr.flush()
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "mock-mcp"}},
        })
        return

    if method == "notifications/initialized":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"text": "ready"}})
        return

    if method == "notifications/cancelled":
        req_cancel_id = (msg.get("params") or {}).get("requestId")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cancelled_request_id": req_cancel_id}) + "\n")
            fh.flush()
        return

    if method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": "get_issue"},
                    {"name": "create_issue"},
                    {"name": "refund_payment"},
                    {"name": "send_email"},
                ]
            },
        })
        return

    if method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"pong": True}})
        return

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}

        if name == "get_issue":
            text_out = (
                "broken-\ud83d-surrogate"
                if arguments.get("return_surrogate")
                else f"issue #{arguments.get('id')}"
            )
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text_out}],
                    "isError": False,
                },
            })
            return

        if name == "fetch_url":
            record_mutation(name, arguments)
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"fetched {arguments.get('url')}"}],
                    "isError": False,
                },
            })
            return

        # Mutating tools
        if arguments.get("ask_client_roots"):
            send({"jsonrpc": "2.0", "id": "srv-req-1", "method": "roots/list", "params": {}})
            # Wait briefly so client reply can flow concurrently without deadlock
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if log_path.exists() and "srv-req-1" in log_path.read_text(encoding="utf-8"):
                    break
                time.sleep(0.02)

        if arguments.get("jsonrpc_error"):
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32050, "message": "upstream validation failed"},
            })
            return

        if arguments.get("tool_is_error"):
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "invalid issue payload"}],
                    "isError": True,
                },
            })
            return

        if arguments.get("timeout_tool_error"):
            err_msg = (
                arguments["timeout_tool_error"]
                if isinstance(arguments["timeout_tool_error"], str)
                else "upstream gateway timed out"
            )
            record_mutation(name, arguments)
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": err_msg}],
                    "isError": True,
                },
            })
            return

        if arguments.get("return_empty_response"):
            record_mutation(name, arguments)
            send({"jsonrpc": "2.0", "id": req_id})
            return

        if arguments.get("delay_before_write_ms"):
            time.sleep(arguments["delay_before_write_ms"] / 1000.0)

        seq = record_mutation(name, arguments)

        if arguments.get("crash_after_write"):
            os._exit(1)

        delay_ms = arguments.get("delay_ms", 0)
        if delay_ms:
            time.sleep(delay_ms / 1000.0)

        url = f"https://example.test/items/{seq}"
        text_val = f"Created {name} #{seq}: {url}"
        if arguments.get("return_surrogate"):
            text_val = f"Created \ud83d #{seq}: {url}"
        structured = {
            "url": url,
            "received_keys": sorted(arguments.keys()),
            "seq": seq,
        }
        if arguments.get("return_deep_nesting"):
            nested = {"leaf": url}
            for _ in range(600):
                nested = {"child": nested}
            structured["nested"] = nested
        if has_id:
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": text_val,
                        }
                    ],
                    "structuredContent": structured,
                    "isError": False,
                },
            })
        return

    if has_id:
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    message = json.loads(line)
    # Handle delayed/bidirectional calls on worker threads so server can process concurrently
    if (
        message.get("method") == "tools/call"
        and isinstance((message.get("params") or {}).get("arguments"), dict)
        and (
            (message["params"]["arguments"].get("delay_ms") or 0) > 0
            or (message["params"]["arguments"].get("delay_before_write_ms") or 0) > 0
            or message["params"]["arguments"].get("ask_client_roots")
        )
    ):
        t = threading.Thread(target=handle_message, args=(message,), daemon=True)
        t.start()
    else:
        handle_message(message)
"""


class _LiveProxyHarness:
    """Drives a real `MCPProxy` over OS pipes for interactive JSON-RPC testing."""

    def __init__(
        self,
        *,
        server_script: Path,
        log_path: Path,
        journal: Journal,
        config: SidefxConfig | None = None,
        session_id: str = "sess-test",
        actor: str = "agent",
        target: str = "mcp:mock",
        timeout_seconds: float = 5.0,
        deterministic_ids: bool = False,
        reconciler_hook: Any = None,
    ) -> None:
        self.c2p_r, self.c2p_w = os.pipe()
        self.p2c_r, self.p2c_w = os.pipe()
        self.client_writer = os.fdopen(self.c2p_w, "wb", buffering=0)
        self.client_reader = os.fdopen(self.p2c_r, "rb")
        self.proxy_in = os.fdopen(self.c2p_r, "rb")
        self.proxy_out = os.fdopen(self.p2c_w, "wb", buffering=0)
        self.err_buf = io.BytesIO()

        env = dict(os.environ)
        env["MOCK_MCP_LOG"] = str(log_path)

        self.proxy = MCPProxy(
            [sys.executable, "-u", str(server_script)],
            journal=journal,
            config=config,
            session_id=session_id,
            actor=actor,
            target=target,
            timeout_seconds=timeout_seconds,
            deterministic_ids=deterministic_ids,
            reconciler_hook=reconciler_hook,
            client_in=self.proxy_in,
            client_out=self.proxy_out,
            err_stream=self.err_buf,
            upstream_env=env,
        )
        self.exit_code: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.exit_code = self.proxy.run()
        finally:
            self.proxy_out.close()
            self.proxy_in.close()

    def send(self, msg: dict[str, Any]) -> None:
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        self.client_writer.write(raw)
        self.client_writer.flush()

    def send_raw(self, raw: bytes) -> None:
        self.client_writer.write(raw)
        self.client_writer.flush()

    def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        result: list[bytes] = []

        def _read() -> None:
            result.append(self.client_reader.readline())

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout)
        assert not t.is_alive(), "Timed out waiting for JSON-RPC response from proxy"
        line = result[0]
        assert line, "Unexpected EOF reading from proxy"
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.client_writer.close()
        self.thread.join(timeout=5.0)
        assert not self.thread.is_alive(), "Proxy thread did not shut down cleanly"
        with contextlib.suppress(OSError):
            self.client_reader.close()

    def __enter__(self) -> _LiveProxyHarness:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


@pytest.fixture
def mock_mcp_server(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "mock_mcp_server.py"
    script.write_text(MOCK_MCP_SERVER_CODE, encoding="utf-8")
    log_path = tmp_path / "upstream_writes.jsonl"
    return script, log_path


def _read_upstream_writes(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            if "tool" in obj:
                entries.append(obj)
    return entries


class TestMCPProxyPassthroughAndMutations:
    def test_protocol_and_readonly_passthrough_plus_id_zero(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            # id=0 must NEVER be treated as falsy/missing
            h.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
            init_resp = h.recv()
            assert init_resp["id"] == 0
            assert init_resp["result"]["serverInfo"]["name"] == "mock-mcp"

            # Notification triggers server-initiated notification
            h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            notif = h.recv()
            assert notif["method"] == "notifications/message"
            assert notif["params"]["text"] == "ready"

            # tools/list passthrough
            h.send({"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"})
            list_resp = h.recv()
            assert list_resp["id"] == "list-1"
            assert len(list_resp["result"]["tools"]) == 4

            # Read-only tools/call passes through without journaling
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_issue", "arguments": {"id": 42}},
                }
            )
            read_resp = h.recv()
            assert read_resp["id"] == 2
            assert read_resp["result"]["content"][0]["text"] == "issue #42"

            # Empty string id passthrough
            h.send({"jsonrpc": "2.0", "id": "", "method": "ping"})
            ping_resp = h.recv()
            assert ping_resp["id"] == ""
            assert ping_resp["result"]["pong"] is True

            assert journal.list_actions() == []
            assert _read_upstream_writes(log_path) == []

        # Upstream stderr was captured cleanly without corrupting JSON-RPC stdout
        assert b"mock-mcp-server initialized on stderr" in h.err_buf.getvalue()

    def test_mutating_call_journals_strips_sidefx_keys_and_replays(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with Journal(db_path) as journal:
            with _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h:
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "Fix bug",
                                "_sidefx_action_id": "issue-1",
                                "_sidefx_extra": "strip-me",
                                "idempotency_key": "keep-me",
                            },
                        },
                    }
                )
                resp1 = h.recv()
                assert resp1["id"] == 10
                assert resp1["result"]["structuredContent"]["url"] == "https://example.test/items/1"
                # Private _sidefx_* keys stripped; idempotency_key preserved
                received_keys = resp1["result"]["structuredContent"]["received_keys"]
                assert "_sidefx_action_id" not in received_keys
                assert "_sidefx_extra" not in received_keys
                assert "idempotency_key" in received_keys
                assert "title" in received_keys

                # In-session replay returns cached result with 0 duplicate upstream writes
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "Fix bug",
                                "_sidefx_action_id": "issue-1",
                                "idempotency_key": "keep-me",
                            },
                        },
                    }
                )
                resp2 = h.recv()
                assert resp2["id"] == 11
                assert resp2["result"] == resp1["result"]

            # Cross-restart replay (fresh proxy instance, empty memory cache) uses SQLite row
            with _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h2:
                h2.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "Fix bug",
                                "_sidefx_action_id": "issue-1",
                                "idempotency_key": "keep-me",
                            },
                        },
                    }
                )
                resp3 = h2.recv()
                assert resp3["id"] == 12
                assert resp3["result"]["_sidefx"]["replayed"] is True
                assert resp3["result"]["_sidefx"]["result_ref"] == "https://example.test/items/1"

            # Verify durable journal row and exact single upstream write
            rec = journal.get_action("agent", "mcp:mock", "issue-1")
            assert rec is not None
            assert rec.status == ActionStatus.CONFIRMED
            assert rec.surface == Surface.MCP
            assert rec.attempt == 1
            assert rec.result_ref == "https://example.test/items/1"
            assert len(_read_upstream_writes(log_path)) == 1

    def test_bidirectional_server_initiated_request_does_not_deadlock(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"title": "needs roots", "ask_client_roots": True},
                    },
                }
            )
            # Server sends server-initiated request `roots/list` while mutation is in-flight
            srv_req = h.recv()
            assert srv_req["method"] == "roots/list"
            assert srv_req["id"] == "srv-req-1"

            # Client answers server-initiated request -> proxy forwards it without deadlock
            h.send({"jsonrpc": "2.0", "id": "srv-req-1", "result": {"roots": []}})

            tool_resp = h.recv()
            assert tool_resp["id"] == 1
            assert tool_resp["result"]["isError"] is False


class TestMCPProxySafetyAndFaults:
    def test_preflight_denial_blocks_before_journal_and_upstream(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"
        cfg = SidefxConfig(
            tools=ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.DENY),
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="refund_payment",
                        arg="amount",
                        max_value=50,
                        reason="max $50 without approval",
                    ),
                    PreflightRule(
                        tool="send_email",
                        arg="to",
                        allow_domains=("company.com",),
                    ),
                )
            ),
        )

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script, log_path=log_path, journal=journal, config=cfg
            ) as h,
        ):
            # 1. Numeric cap violation
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "refund_payment",
                        "arguments": {"amount": 250, "_sidefx_action_id": "ref-1"},
                    },
                }
            )
            r1 = h.recv()
            assert r1["error"]["code"] == ERR_PREFLIGHT_DENIED
            assert "max $50 without approval" in r1["error"]["message"]
            assert r1["error"]["data"]["kind"] == "preflight_denied"

            # 2. Domain allowlist violation
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "send_email",
                        "arguments": {"to": "victim@evil.com@company.com"},
                    },
                }
            )
            r2 = h.recv()
            assert r2["error"]["code"] == ERR_PREFLIGHT_DENIED

            # 3. Unknown tool denied by unknown_tool_policy=deny
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "mystery_tool", "arguments": {}},
                }
            )
            r3 = h.recv()
            assert r3["error"]["code"] == ERR_PREFLIGHT_DENIED

            assert journal.list_actions() == []
            assert _read_upstream_writes(log_path) == []

    def test_upstream_timeout_marks_unknown_and_suppresses_late_response(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                timeout_seconds=0.15,
            ) as h,
        ):
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "slow write",
                            "delay_ms": 400,
                            "_sidefx_action_id": "slow-1",
                        },
                    },
                }
            )
            timeout_resp = h.recv(timeout=2.0)
            assert timeout_resp["id"] == 1
            assert timeout_resp["error"]["code"] == ERR_OUTCOME_UNKNOWN
            assert OUTCOME_UNKNOWN_BANNER in timeout_resp["error"]["message"]
            assert "slow-1" in timeout_resp["error"]["message"]
            assert timeout_resp["error"]["data"]["action_id"] == "slow-1"
            assert timeout_resp["error"]["data"]["marker"]

            rec = journal.get_action("agent", "mcp:mock", "slow-1")
            assert rec is not None
            assert rec.status == ActionStatus.UNKNOWN

            # Wait for the late upstream reply (400ms total) to arrive, reconcile in SQLite,
            # and be dropped from client stdout without corrupting subsequent frames.
            time.sleep(0.4)

            # Reuse the EXACT SAME client id=1 on a subsequent call: must NOT receive stale reply!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"title": "fresh write", "_sidefx_action_id": "fresh-2"},
                    },
                }
            )
            fresh_resp = h.recv()
            assert fresh_resp["id"] == 1
            assert "error" not in fresh_resp
            assert fresh_resp["result"]["structuredContent"]["seq"] == 2

            # N1: Late success reconciled in SQLite -> retry replays cached result (0 dup writes!)
            rec_late = journal.get_action("agent", "mcp:mock", "slow-1")
            assert rec_late is not None
            assert rec_late.status == ActionStatus.RECONCILED
            assert rec_late.result_ref == "https://example.test/items/1"

            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "slow write",
                            "delay_ms": 400,
                            "_sidefx_action_id": "slow-1",
                        },
                    },
                }
            )
            replayed = h.recv()
            assert replayed["id"] == 2
            assert "error" not in replayed
            assert replayed["result"]["structuredContent"]["seq"] == 1

        # Exactly 2 total writes: slow-1 (once) + fresh-2 (once). Zero duplicates!
        writes = _read_upstream_writes(log_path)
        assert len(writes) == 2

    def test_upstream_crash_mid_write_blocks_retry_with_zero_duplicates(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with Journal(db_path) as journal:
            with _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h:
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "crash after write",
                                "crash_after_write": True,
                                "_sidefx_action_id": "crash-1",
                            },
                        },
                    }
                )
                crash_resp = h.recv()
                assert crash_resp["id"] == 7
                assert crash_resp["error"]["code"] == ERR_OUTCOME_UNKNOWN
                assert OUTCOME_UNKNOWN_BANNER in crash_resp["error"]["message"]
                assert "crash-1" in crash_resp["error"]["message"]

                # Retry on the SAME proxy session is blocked
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "crash after write",
                                "crash_after_write": True,
                                "_sidefx_action_id": "crash-1",
                            },
                        },
                    }
                )
                retry_same = h.recv()
                assert retry_same["id"] == 8
                assert retry_same["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Retry in a BRAND NEW proxy session pointing at the same SQLite DB is ALSO blocked!
            with _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h2:
                h2.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "crash after write",
                                "crash_after_write": True,
                                "_sidefx_action_id": "crash-1",
                            },
                        },
                    }
                )
                retry_new = h2.recv()
                assert retry_new["id"] == 9
                assert retry_new["error"]["code"] == ERR_OUTCOME_UNKNOWN
                assert OUTCOME_UNKNOWN_BANNER in retry_new["error"]["message"]

            rec = journal.get_action("agent", "mcp:mock", "crash-1")
            assert rec is not None
            assert rec.status == ActionStatus.UNKNOWN
            # Upstream wrote ONCE and NEVER duplicated!
            assert len(_read_upstream_writes(log_path)) == 1

    def test_payload_mismatch_under_reused_action_id_is_rejected(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"title": "Original", "_sidefx_action_id": "reuse-1"},
                    },
                }
            )
            assert "result" in h.recv()

            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"title": "Mutated!", "_sidefx_action_id": "reuse-1"},
                    },
                }
            )
            mismatch = h.recv()
            assert mismatch["id"] == 2
            assert mismatch["error"]["code"] == ERR_PAYLOAD_MISMATCH
            assert mismatch["error"]["data"]["kind"] == "payload_mismatch"
            assert len(_read_upstream_writes(log_path)) == 1

    def test_clean_errors_mark_failed_and_allow_safe_retry(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            # 1. JSON-RPC error -> marked failed
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"jsonrpc_error": True, "_sidefx_action_id": "err-rpc"},
                    },
                }
            )
            r1 = h.recv()
            assert r1["error"]["code"] == -32050
            rec1 = journal.get_action("agent", "mcp:mock", "err-rpc")
            assert rec1 is not None
            assert rec1.status == ActionStatus.FAILED

            # 2. MCP isError=true -> marked failed
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"tool_is_error": True, "_sidefx_action_id": "err-tool"},
                    },
                }
            )
            r2 = h.recv()
            assert r2["result"]["isError"] is True
            rec2 = journal.get_action("agent", "mcp:mock", "err-tool")
            assert rec2 is not None
            assert rec2.status == ActionStatus.FAILED
            assert rec2.error == "invalid issue payload"

            # Retrying the failed action with same payload bumps attempt=2
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"tool_is_error": True, "_sidefx_action_id": "err-tool"},
                    },
                }
            )
            _ = h.recv()
            rec2_after = journal.get_action("agent", "mcp:mock", "err-tool")
            assert rec2_after is not None
            assert rec2_after.attempt == 2
            assert rec2_after.status == ActionStatus.FAILED

    def test_reconciler_hook_found_not_found_and_error(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with Journal(db_path) as journal:
            # First create a crash-induced unknown record
            with _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h:
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "recoverable",
                                "crash_after_write": True,
                                "_sidefx_action_id": "rec-found",
                            },
                        },
                    }
                )
                assert h.recv()["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Hook returns FOUND -> transitions unknown -> reconciled, 0 duplicate upstream calls!
            seen_records: list[ActionRecord] = []

            def hook_found(rec: ActionRecord) -> ReconcileHookResult:
                seen_records.append(rec)
                return ReconcileHookResult(
                    state=ReconcileState.FOUND,
                    result_ref="https://example.test/items/1",
                    summary="Recovered from remote log",
                )

            with _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                reconciler_hook=hook_found,
            ) as h2:
                h2.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "recoverable",
                                "crash_after_write": True,
                                "_sidefx_action_id": "rec-found",
                            },
                        },
                    }
                )
                recovered = h2.recv()
                assert "error" not in recovered
                assert recovered["result"]["_sidefx"]["status"] == "reconciled"
                assert (
                    recovered["result"]["_sidefx"]["result_ref"] == "https://example.test/items/1"
                )

            assert len(seen_records) == 1
            rec = journal.get_action("agent", "mcp:mock", "rec-found")
            assert rec is not None
            assert rec.status == ActionStatus.RECONCILED
            assert len(_read_upstream_writes(log_path)) == 1

            # Seed an unknown record where remote write never landed -> hook returns NOT_FOUND
            journal.begin_action(
                action_id="rec-not-found",
                session_id="sess-test",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="mcp:mock",
                payload={"tool": "create_issue", "arguments": {"title": "safe-retry"}},
            )
            journal.mark_unknown("agent", "mcp:mock", "rec-not-found", error="pre-write timeout")

            def hook_not_found(_rec: ActionRecord) -> ReconcileHookResult:
                return ReconcileHookResult(
                    state=ReconcileState.NOT_FOUND,
                    details="verified absent on remote",
                )

            with _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                reconciler_hook=hook_not_found,
            ) as h3:
                h3.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "safe-retry",
                                "_sidefx_action_id": "rec-not-found",
                            },
                        },
                    }
                )
                retried = h3.recv()
                assert "error" not in retried
                assert retried["result"]["isError"] is False

            rec_nf = journal.get_action("agent", "mcp:mock", "rec-not-found")
            assert rec_nf is not None
            assert rec_nf.status == ActionStatus.CONFIRMED
            assert rec_nf.attempt == 2

            # Hook raising an exception must fail closed (never re-execute!)
            journal.begin_action(
                action_id="rec-boom",
                session_id="sess-test",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_issue",
                target="mcp:mock",
                payload={"tool": "create_issue", "arguments": {"title": "boom"}},
            )
            journal.mark_unknown("agent", "mcp:mock", "rec-boom", error="timeout")

            def hook_raises(_rec: ActionRecord) -> ReconcileHookResult:
                raise RuntimeError("reconciler backend offline")

            with _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                reconciler_hook=hook_raises,
            ) as h4:
                h4.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {"title": "boom", "_sidefx_action_id": "rec-boom"},
                        },
                    }
                )
                blocked = h4.recv()
                assert blocked["error"]["code"] == ERR_OUTCOME_UNKNOWN

    def test_deterministic_ids_deduplicate_within_session(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                session_id="det-sess",
                deterministic_ids=True,
            ) as h,
        ):
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_issue", "arguments": {"title": "Same call"}},
            }
            h.send(req)
            r1 = h.recv()
            h.send({**req, "id": 2})
            r2 = h.recv()
            assert r1["result"] == r2["result"]
            assert len(_read_upstream_writes(log_path)) == 1

            actions = journal.list_actions(session_id="det-sess")
            assert len(actions) == 1
            assert actions[0].action_id.startswith("det-")

    def test_malformed_json_batches_and_invalid_params_handled_cleanly(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            # Blank lines ignored
            h.send_raw(b"\n   \r\n")
            # Malformed JSON -> -32700
            h.send_raw(b"{not valid json\n")
            assert h.recv()["error"]["code"] == JSONRPC_PARSE_ERROR
            # Non-standard NaN -> -32700
            h.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n')
            assert h.recv()["error"]["code"] == JSONRPC_PARSE_ERROR
            # Invalid UTF-8 -> -32700
            h.send_raw(b"\xff\xfe\n")
            assert h.recv()["error"]["code"] == JSONRPC_PARSE_ERROR
            # Batch array rejected (prevents smuggling mutations in batch) -> -32600
            h.send_raw(
                b'[{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"create_issue","arguments":{}}}]\n'
            )
            assert h.recv()["error"]["code"] == JSONRPC_INVALID_REQUEST
            # Invalid tools/call params -> -32602
            h.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": ""}})
            assert h.recv()["error"]["code"] == JSONRPC_INVALID_PARAMS
            # Invalid _sidefx_action_id -> -32602
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"_sidefx_action_id": 123},
                    },
                }
            )
            assert h.recv()["error"]["code"] == JSONRPC_INVALID_PARAMS

            # Proxy is still healthy and processes valid requests afterward
            h.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
            assert h.recv()["result"]["pong"] is True
            assert _read_upstream_writes(log_path) == []

    def test_b1_surrogates_huge_floats_and_deep_nesting_never_crash_proxy(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            # 1. Client read/ping with lone surrogate survives round-trip
            h.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"s":"\\ud83d"}}\n')
            assert h.recv()["result"]["pong"] is True

            # 2. Client 1e999 (float overflow -> inf) rejected cleanly as Parse error
            h.send_raw(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"x":1e999}}\n')
            assert h.recv()["error"]["code"] == JSONRPC_PARSE_ERROR

            # 3. Client mutating call with lone surrogate succeeds and journals cleanly
            h.send_raw(
                b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_issue",'
                b'"arguments":{"title":"hi \\ud83d","return_surrogate":true,'
                b'"_sidefx_action_id":"surr-1"}}}\n'
            )
            r3 = h.recv()
            assert r3["id"] == 3
            assert r3["result"]["isError"] is False
            rec3 = journal.get_action("agent", "mcp:mock", "surr-1")
            assert rec3 is not None
            assert rec3.status == ActionStatus.CONFIRMED

            # 4. Upstream read call returning lone surrogate does not kill reader thread
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "get_issue",
                        "arguments": {"id": 1, "return_surrogate": True},
                    },
                }
            )
            r4 = h.recv()
            assert r4["id"] == 4
            assert "surrogate" in r4["result"]["content"][0]["text"]

            # 5. Client deeply nested arguments rejected BEFORE creating a pending row
            deep_raw = (
                b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"create_issue",'
                b'"arguments":{"_sidefx_action_id":"deep-client","x":'
                + b'{"a":' * 1500
                + b"1"
                + b"}" * 1500
                + b"}}}\n"
            )
            h.send_raw(deep_raw)
            r5 = h.recv()
            assert r5["error"]["code"] in (JSONRPC_PARSE_ERROR, JSONRPC_INVALID_PARAMS)
            assert journal.get_action("agent", "mcp:mock", "deep-client") is None

            # 6. Upstream 600-deep mutation result confirms and replays without deepcopy error
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "deep-resp",
                            "return_deep_nesting": True,
                            "_sidefx_action_id": "deep-up",
                        },
                    },
                }
            )
            r6 = h.recv()
            assert r6["id"] == 6
            assert r6["result"]["isError"] is False
            rec6 = journal.get_action("agent", "mcp:mock", "deep-up")
            assert rec6 is not None
            assert rec6.status == ActionStatus.CONFIRMED

            # Replay of deeply nested cached response works without RecursionError
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "deep-resp",
                            "return_deep_nesting": True,
                            "_sidefx_action_id": "deep-up",
                        },
                    },
                }
            )
            r7 = h.recv()
            assert r7["id"] == 7
            assert r7["result"]["structuredContent"]["url"] == "https://example.test/items/2"

            # Proxy still alive and responsive
            h.send({"jsonrpc": "2.0", "id": 8, "method": "ping"})
            assert h.recv()["result"]["pong"] is True

    def test_b3_preflight_rules_enforced_on_read_tools_without_journaling(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(PreflightRule(tool="*", arg="url", deny_domains=("evil.com",)),)
            )
        )

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script, log_path=log_path, journal=journal, config=cfg
            ) as h,
        ):
            # Denied read tool is blocked before upstream
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_url",
                        "arguments": {"url": "https://evil.com/secret"},
                    },
                }
            )
            denied = h.recv()
            assert denied["error"]["code"] == ERR_PREFLIGHT_DENIED
            assert _read_upstream_writes(log_path) == []

            # Allowed read tool passes through without journaling
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "fetch_url", "arguments": {"url": "https://good.com/page"}},
                }
            )
            ok = h.recv()
            assert ok["result"]["isError"] is False
            assert journal.list_actions() == []
            assert len(_read_upstream_writes(log_path)) == 1

    def test_b4_reconciler_not_found_blocked_while_timed_out_request_still_inflight(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        def hook_not_found(_rec: ActionRecord) -> ReconcileHookResult:
            return ReconcileHookResult(state=ReconcileState.NOT_FOUND, details="not visible yet")

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script,
                log_path=log_path,
                journal=journal,
                timeout_seconds=0.15,
                reconciler_hook=hook_not_found,
            ) as h,
        ):
            # Upstream sleeps 500ms BEFORE writing -> timed out at 150ms while still running!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "in-flight race",
                            "delay_before_write_ms": 500,
                            "idempotency_key": "race-key-1",
                        },
                    },
                }
            )
            r1 = h.recv(timeout=2.0)
            assert r1["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Retry immediately while upstream is still executing attempt 1:
            # reconciler says NOT_FOUND, but proxy MUST block re-dispatch!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "in-flight race",
                            "delay_before_write_ms": 500,
                            "idempotency_key": "race-key-1",
                        },
                    },
                }
            )
            r2 = h.recv(timeout=2.0)
            assert r2["error"]["code"] == ERR_OUTCOME_UNKNOWN

            # Wait for original attempt 1 to complete on upstream
            time.sleep(0.55)

        # Upstream executed EXACTLY ONCE despite reconciler saying NOT_FOUND!
        assert len(_read_upstream_writes(log_path)) == 1

    def test_n2_n8_n10_empty_response_ambiguous_error_and_cancellation_translation(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(
                server_script=script, log_path=log_path, journal=journal, timeout_seconds=2.0
            ) as h,
        ):
            # N2: empty JSON-RPC response (no result, no error) -> marked UNKNOWN, not confirmed
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "return_empty_response": True,
                            "_sidefx_action_id": "empty-resp-1",
                        },
                    },
                }
            )
            r1 = h.recv()
            assert r1["error"]["code"] == ERR_OUTCOME_UNKNOWN
            rec1 = journal.get_action("agent", "mcp:mock", "empty-resp-1")
            assert rec1 is not None
            assert rec1.status == ActionStatus.UNKNOWN

            # N10: ambiguous timeout/socket errors -> marked UNKNOWN and retry is BLOCKED!
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "timeout_tool_error": "connect ETIMEDOUT 10.0.0.1:443",
                            "idempotency_key": "ambig-err-1",
                        },
                    },
                }
            )
            r2 = h.recv()
            assert r2["error"]["code"] == ERR_OUTCOME_UNKNOWN
            rec2 = journal.get_action("agent", "mcp:mock", "ambig-err-1")
            assert rec2 is not None
            assert rec2.status == ActionStatus.UNKNOWN

            # Retry with same idempotency_key MUST be blocked (0 duplicate upstream writes!)
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "timeout_tool_error": "connect ETIMEDOUT 10.0.0.1:443",
                            "idempotency_key": "ambig-err-1",
                        },
                    },
                }
            )
            r2_retry = h.recv()
            assert r2_retry["error"]["code"] == ERR_OUTCOME_UNKNOWN
            etimedout_writes = [
                w
                for w in _read_upstream_writes(log_path)
                if w["arguments"].get("idempotency_key") == "ambig-err-1"
            ]
            assert len(etimedout_writes) == 1

            # N8: notifications/cancelled rewrites client requestId to upstream __sidefx_ ID
            h.send(
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {"title": "cancel-me", "delay_ms": 200},
                    },
                }
            )
            time.sleep(0.05)
            h.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 42, "reason": "user cancelled"},
                }
            )
            r42 = h.recv()
            assert r42["id"] == 42

        log_entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cancelled_ids = [
            e["cancelled_request_id"] for e in log_entries if "cancelled_request_id" in e
        ]
        assert len(cancelled_ids) == 1
        assert isinstance(cancelled_ids[0], str)
        assert cancelled_ids[0].startswith("__sidefx_")

    def test_oversized_frame_id_null_client_eof_inflight_and_two_proxies_sharing_journal(
        self,
        tmp_path: Path,
        mock_mcp_server: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "journal.db"

        # 1. Oversized frame + id=null handling
        import sidefx.mcp_proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "MAX_MESSAGE_BYTES", 256)
        with (
            Journal(db_path) as journal,
            _LiveProxyHarness(server_script=script, log_path=log_path, journal=journal) as h,
        ):
            oversized = (
                b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * 400 + b'"}}\n'
            )
            h.send_raw(oversized)
            err = h.recv()
            assert err["error"]["code"] == JSONRPC_PARSE_ERROR

            # Framing stays synchronized; id=null ping works cleanly
            h.send({"jsonrpc": "2.0", "id": None, "method": "ping"})
            r_null = h.recv()
            assert r_null["id"] is None
            assert r_null["result"]["pong"] is True
        monkeypatch.undo()

        # 2. Client EOF while a mutating call is in flight still delivers response cleanly
        env = dict(os.environ)
        env["MOCK_MCP_LOG"] = str(log_path)
        req_in = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "create_issue",
                        "arguments": {
                            "title": "eof-inflight",
                            "delay_ms": 150,
                            "_sidefx_action_id": "eof-1",
                        },
                    },
                }
            )
            + "\n"
        ).encode("utf-8")
        out_buf = io.BytesIO()
        with Journal(db_path) as journal:
            proxy = MCPProxy(
                [sys.executable, "-u", str(script)],
                journal=journal,
                target="mcp:mock",
                timeout_seconds=3.0,
                client_in=io.BytesIO(req_in),
                client_out=out_buf,
                err_stream=io.BytesIO(),
                upstream_env=env,
            )
            assert proxy.run() == 0
            rec = journal.get_action("agent", "mcp:mock", "eof-1")
            assert rec is not None
            assert rec.status == ActionStatus.CONFIRMED
        out_resp = json.loads(out_buf.getvalue().decode("utf-8").strip())
        assert out_resp["id"] == 99
        assert out_resp["result"]["isError"] is False

        # 3. Two concurrent proxies sharing one SQLite journal racing on same action_id -> 1 write
        shared_log = tmp_path / "shared_writes.jsonl"
        shared_db = tmp_path / "shared_journal.db"
        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()

        def worker(idx: int) -> None:
            with (
                Journal(shared_db) as j,
                _LiveProxyHarness(
                    server_script=script,
                    log_path=shared_log,
                    journal=j,
                    target="mcp:shared",
                ) as h,
            ):
                barrier.wait(timeout=5.0)
                h.send(
                    {
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "tools/call",
                        "params": {
                            "name": "create_issue",
                            "arguments": {
                                "title": "concurrent-write",
                                "delay_ms": 150,
                                "_sidefx_action_id": "shared-act-1",
                            },
                        },
                    }
                )
                resp = h.recv(timeout=5.0)
                with results_lock:
                    results.append(resp)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert len(results) == 2
        # Exactly one proxy dispatched & confirmed; the other saw OUTCOME_UNKNOWN or REPLAY.
        assert len(_read_upstream_writes(shared_log)) == 1
        successes = [r for r in results if "result" in r]
        assert len(successes) >= 1

    def test_n3_n9_default_target_distinguishes_commands_and_env_session_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "j.db"
        cfg = SidefxConfig(default_session_id="cfg-sess")
        monkeypatch.setenv("SIDEFX_SESSION_ID", "env-sess")

        with Journal(db_path) as journal:
            p_a = MCPProxy(["python3", "server_a.py"], journal=journal, config=cfg)
            p_b = MCPProxy(["python3", "server_b.py"], journal=journal, config=cfg)
            p_a2 = MCPProxy(["python3", "server_a.py"], journal=journal, config=cfg)
            assert p_a.target != p_b.target
            assert p_a.target == p_a2.target
            assert p_a.session_id == "env-sess"

            p_explicit = MCPProxy(
                ["python3", "server_a.py"],
                journal=journal,
                config=cfg,
                session_id="cli-sess",
            )
            assert p_explicit.session_id == "cli-sess"

    @pytest.mark.parametrize(
        "err_text",
        [
            "connect ETIMEDOUT 10.0.0.1:443",
            "ESOCKETTIMEDOUT",
            "TimeoutError: operation exceeded limit",
            "httpx.ReadTimeout",
            "asyncio.TimeoutError",
            "UND_ERR_HEADERS_TIMEOUT",
            "socket hang up",
            "fetch failed",
            "502 Bad Gateway",
            "write EPIPE",
            "504 Gateway Timeout",
            "ECONNRESET",
        ],
    )
    def test_ambiguous_error_classifier_catches_real_timeout_spellings(self, err_text: str) -> None:
        from sidefx.mcp_proxy import _is_ambiguous_error_text

        assert _is_ambiguous_error_text(err_text) is True

    @pytest.mark.parametrize(
        "clean_err",
        [
            "invalid issue payload",
            "upstream validation failed",
            "401 Unauthorized",
            "404 Not Found",
            "Permission denied",
        ],
    )
    def test_ambiguous_error_classifier_preserves_clean_rejections(self, clean_err: str) -> None:
        from sidefx.mcp_proxy import _is_ambiguous_error_text

        assert _is_ambiguous_error_text(clean_err) is False


class TestMCPProxyCLI:
    def test_parse_mcp_proxy_args_preserves_upstream_flags(self) -> None:
        parsed, upstream = parse_mcp_proxy_args(
            [
                "--db",
                "/tmp/sidefx.db",
                "--timeout",
                "12.5",
                "--deterministic-ids",
                "--",
                "python3",
                "server.py",
                "--db",
                "upstream.db",
                "--timeout",
                "99",
            ]
        )
        assert parsed.db_path == "/tmp/sidefx.db"
        assert parsed.timeout == 12.5
        assert parsed.deterministic_ids is True
        assert upstream == ["python3", "server.py", "--db", "upstream.db", "--timeout", "99"]

    def test_parse_mcp_proxy_args_requires_upstream_command(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_mcp_proxy_args(["--timeout", "5"])
        assert exc_info.value.code == 2

    def test_end_to_end_cli_subprocess_invocation(
        self, tmp_path: Path, mock_mcp_server: tuple[Path, Path]
    ) -> None:
        script, log_path = mock_mcp_server
        db_path = tmp_path / "cli_journal.db"
        cfg_path = tmp_path / ".sidefx.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "preflight": {
                        "rules": [
                            {"tool": "refund_payment", "arg": "amount", "max": 25},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "create_issue",
                    "arguments": {"title": "CLI test", "_sidefx_action_id": "cli-1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "refund_payment",
                    "arguments": {"amount": 100, "_sidefx_action_id": "cli-2"},
                },
            },
        ]
        stdin_bytes = "".join(json.dumps(r) + "\n" for r in requests).encode("utf-8")

        env = dict(os.environ)
        env["MOCK_MCP_LOG"] = str(log_path)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "sidefx",
                "mcp-proxy",
                "--db",
                str(db_path),
                "--config",
                str(cfg_path),
                "--session-id",
                "cli-sess",
                "--actor",
                "cli-agent",
                "--target",
                "mcp:cli-mock",
                "--timeout",
                "5",
                "--",
                sys.executable,
                "-u",
                str(script),
            ],
            input=stdin_bytes,
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

        responses = [
            json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()
        ]
        by_id = {r["id"]: r for r in responses}
        assert by_id[1]["result"]["serverInfo"]["name"] == "mock-mcp"
        assert by_id[2]["result"]["structuredContent"]["url"] == "https://example.test/items/1"
        assert by_id[3]["error"]["code"] == ERR_PREFLIGHT_DENIED

        with Journal.open_readonly(db_path) as journal:
            rec = journal.get_action("cli-agent", "mcp:cli-mock", "cli-1")
            assert rec is not None
            assert rec.status == ActionStatus.CONFIRMED
            assert rec.session_id == "cli-sess"
            assert journal.get_action("cli-agent", "mcp:cli-mock", "cli-2") is None
