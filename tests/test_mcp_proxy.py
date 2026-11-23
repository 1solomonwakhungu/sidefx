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
    data = json.dumps(obj, separators=(",", ":")) + "\n"
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
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"issue #{arguments.get('id')}"}],
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

        seq = record_mutation(name, arguments)

        if arguments.get("crash_after_write"):
            os._exit(1)

        delay_ms = arguments.get("delay_ms", 0)
        if delay_ms:
            time.sleep(delay_ms / 1000.0)

        url = f"https://example.test/items/{seq}"
        if has_id:
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Created {name} #{seq}: {url}",
                        }
                    ],
                    "structuredContent": {
                        "url": url,
                        "received_keys": sorted(arguments.keys()),
                        "seq": seq,
                    },
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

            # Wait for the late upstream reply (400ms total) to arrive and be dropped cleanly.
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

            # Retry of the timed-out action is still blocked (no duplicate write!)
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
            blocked = h.recv()
            assert blocked["id"] == 2
            assert blocked["error"]["code"] == ERR_OUTCOME_UNKNOWN
            assert OUTCOME_UNKNOWN_BANNER in blocked["error"]["message"]

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
