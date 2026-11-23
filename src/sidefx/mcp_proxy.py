"""Transparent stdio JSON-RPC 2.0 MCP proxy with write-ahead journaling and preflight checks."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from sidefx.config import MAX_TIMEOUT_SECONDS, SidefxConfig, load_config
from sidefx.journal import (
    ActionRecord,
    ActionStatus,
    BeginDecision,
    InvalidStateTransitionError,
    Journal,
    JournalError,
    PayloadMismatchError,
    StaleAttemptError,
    Surface,
    compute_payload_hash,
    resolve_db_path,
)
from sidefx.preflight import ToolKind, classify_tool, evaluate_preflight

ENV_SESSION_ID = "SIDEFX_SESSION_ID"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 MiB per newline-delimited JSON-RPC frame
_MAX_TIMED_OUT_HISTORY = 10_000
_SHUTDOWN_GRACE_SECONDS = 2.0
_MAX_REF_LENGTH = 500
_MAX_SUMMARY_LENGTH = 1000
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+")
_RESULT_REF_KEYS = ("result_ref", "url", "html_url", "uri", "id")

# Standard JSON-RPC 2.0 and sidefx application error codes
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INVALID_PARAMS = -32602
ERR_PREFLIGHT_DENIED = -32001
ERR_OUTCOME_UNKNOWN = -32002
ERR_UPSTREAM_UNAVAILABLE = -32003
ERR_PAYLOAD_MISMATCH = -32004
ERR_JOURNAL_ERROR = -32005

OUTCOME_UNKNOWN_BANNER = "OUTCOME UNKNOWN — reconcile before retrying"
_MISSING: Any = object()


class ReconcileState(StrEnum):
    """Outcome reported by a pluggable reconciler hook."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReconcileHookResult:
    """Result returned by a reconciler hook invoked on retry of an unknown action."""

    state: ReconcileState
    result_ref: str | None = None
    summary: str | None = None
    details: str | None = None
    cached_result: dict[str, Any] | None = None


ReconcilerHook = Callable[[ActionRecord], Any]


@dataclass
class _InFlightMutation:
    """Tracks one mutating `tools/call` currently dispatched to the upstream MCP server."""

    upstream_id: str
    client_id: Any
    has_client_id: bool
    action_id: str
    marker: str
    tool_name: str
    attempt: int
    timer: threading.Timer | None = None
    settled: bool = False


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {token!r}")


def _sanitize_text(text: str | None, *, max_len: int | None = None) -> str | None:
    if text is None:
        return None
    cleaned = text.replace("\x00", "")
    if max_len is not None and len(cleaned) > max_len:
        cleaned = f"{cleaned[: max_len - 3]}..."
    return cleaned


def _jsonrpc_error_response(
    req_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_obj: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error_obj}


def _jsonrpc_result_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _serialize_message(message: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return encoded.encode("utf-8") + b"\n"


def _extract_result_ref_and_summary(result: Any) -> tuple[str | None, str | None]:
    """Extract concise ``result_ref`` and ``summary`` strings from an MCP tool result."""
    if not isinstance(result, dict):
        summary = _sanitize_text(str(result), max_len=_MAX_SUMMARY_LENGTH)
        return None, summary

    result_ref: str | None = None

    def _find_ref_in_dict(mapping: dict[str, Any]) -> str | None:
        for key in _RESULT_REF_KEYS:
            val = mapping.get(key)
            if isinstance(val, str) and val.strip():
                return _sanitize_text(val.strip(), max_len=_MAX_REF_LENGTH)
            if isinstance(val, int) and not isinstance(val, bool):
                return str(val)
        return None

    result_ref = _find_ref_in_dict(result)

    structured = result.get("structuredContent")
    if result_ref is None and isinstance(structured, dict):
        result_ref = _find_ref_in_dict(structured)

    text_chunks: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_val = item.get("text")
                if isinstance(text_val, str) and text_val.strip():
                    text_chunks.append(text_val.strip())

    joined_text = "\n".join(text_chunks).strip()
    if result_ref is None and joined_text:
        # Check if the text content itself is a JSON object with a known ref key.
        if joined_text.startswith("{") and joined_text.endswith("}"):
            try:
                parsed_text = json.loads(joined_text)
            except ValueError:
                parsed_text = None
            if isinstance(parsed_text, dict):
                result_ref = _find_ref_in_dict(parsed_text)
        if result_ref is None:
            url_match = _URL_RE.search(joined_text)
            if url_match is not None:
                result_ref = _sanitize_text(url_match.group(0), max_len=_MAX_REF_LENGTH)

    if joined_text:
        summary = _sanitize_text(joined_text, max_len=_MAX_SUMMARY_LENGTH)
    elif structured is not None:
        try:
            summary = _sanitize_text(
                json.dumps(structured, sort_keys=True, separators=(",", ":")),
                max_len=_MAX_SUMMARY_LENGTH,
            )
        except (TypeError, ValueError):
            summary = _sanitize_text(str(structured), max_len=_MAX_SUMMARY_LENGTH)
    else:
        summary = result_ref

    return result_ref, summary


def _extract_tool_error_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_val = item.get("text")
                if isinstance(text_val, str) and text_val.strip():
                    parts.append(text_val.strip())
        if parts:
            return _sanitize_text(" ".join(parts), max_len=_MAX_SUMMARY_LENGTH) or "tool error"
    return "MCP tool returned isError=true"


def _build_replay_result(record: ActionRecord) -> dict[str, Any]:
    """Synthesize a clean MCP `tools/call` result from a durable journal row."""
    parts = [f"Replayed {record.status.value} action {record.action_id}"]
    if record.result_ref:
        parts.append(f"ref={record.result_ref}")
    if record.summary and record.summary != record.result_ref:
        parts.append(record.summary)
    return {
        "content": [{"type": "text", "text": " | ".join(parts)}],
        "isError": False,
        "_sidefx": {
            "replayed": True,
            "status": record.status.value,
            "action_id": record.action_id,
            "marker": record.marker,
            "result_ref": record.result_ref,
            "summary": record.summary,
        },
    }


def _default_target_from_cmd(upstream_cmd: Sequence[str]) -> str:
    if not upstream_cmd:
        return "mcp:upstream"
    base = Path(upstream_cmd[0]).name.strip()
    if not base:
        return "mcp:upstream"
    return f"mcp:{base}"


def _derive_deterministic_action_id(
    *,
    session_id: str,
    actor: str,
    target: str,
    tool_name: str,
    payload_hash: str,
) -> str:
    encoded = json.dumps(
        ["sidefx-mcp-action-v1", session_id, actor, target, tool_name, payload_hash],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return f"det-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _coerce_reconcile_hook_result(raw: Any) -> ReconcileHookResult | None:
    if raw is None:
        return None
    if isinstance(raw, ReconcileHookResult):
        return raw
    state_raw = getattr(raw, "state", None)
    if state_raw is None:
        return None
    try:
        state = ReconcileState(str(state_raw))
    except ValueError:
        return None
    result_ref = getattr(raw, "result_ref", None)
    summary = getattr(raw, "summary", None)
    details = getattr(raw, "details", None)
    cached_result = getattr(raw, "cached_result", None)
    return ReconcileHookResult(
        state=state,
        result_ref=result_ref if isinstance(result_ref, str) else None,
        summary=summary if isinstance(summary, str) else None,
        details=details if isinstance(details, str) else None,
        cached_result=cached_result if isinstance(cached_result, dict) else None,
    )


class MCPProxy:
    """Transparent stdio JSON-RPC 2.0 MCP proxy with write-ahead journaling."""

    def __init__(
        self,
        upstream_cmd: Sequence[str],
        *,
        journal: Journal,
        config: SidefxConfig | None = None,
        session_id: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        timeout_seconds: float | None = None,
        deterministic_ids: bool = False,
        reconciler_hook: ReconcilerHook | None = None,
        client_in: BinaryIO | None = None,
        client_out: BinaryIO | None = None,
        err_stream: BinaryIO | None = None,
        upstream_env: dict[str, str] | None = None,
    ) -> None:
        if not upstream_cmd:
            raise ValueError("upstream_cmd must not be empty")
        self._upstream_cmd = [str(part) for part in upstream_cmd]
        self._journal = journal
        self._config = config if config is not None else SidefxConfig()
        self._session_id = (
            session_id
            or self._config.default_session_id
            or os.environ.get(ENV_SESSION_ID, "").strip()
            or f"mcp-{uuid.uuid4().hex[:12]}"
        )
        self._actor = actor or self._config.default_actor
        self._target = target or _default_target_from_cmd(self._upstream_cmd)
        timeout = (
            timeout_seconds if timeout_seconds is not None else self._config.default_timeout_seconds
        )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be a finite positive number")
        timeout_float = float(timeout)
        if not math.isfinite(timeout_float) or timeout_float <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if timeout_float > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be <= {int(MAX_TIMEOUT_SECONDS)}")
        self._timeout_seconds = timeout_float
        self._deterministic_ids = bool(deterministic_ids)
        self._reconciler_hook = reconciler_hook
        self._client_in: BinaryIO = client_in if client_in is not None else sys.stdin.buffer
        self._client_out: BinaryIO = client_out if client_out is not None else sys.stdout.buffer
        self._err_stream: BinaryIO | None = err_stream
        self._upstream_env = upstream_env

        self._proxy_nonce = os.urandom(8).hex()
        self._seq_counter = 0

        self._client_write_lock = threading.Lock()
        self._upstream_write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._state_lock)

        self._pending_mutations: dict[str, _InFlightMutation] = {}
        self._timed_out_upstream_ids: OrderedDict[str, str] = OrderedDict()
        self._confirmed_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._upstream_closed = False
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def actor(self) -> str:
        return self._actor

    @property
    def target(self) -> str:
        return self._target

    def _log_err(self, message: str) -> None:
        data = f"[sidefx mcp-proxy] {message}\n".encode("utf-8", errors="replace")
        try:
            if self._err_stream is not None:
                self._err_stream.write(data)
                self._err_stream.flush()
            else:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
        except OSError:
            pass

    def _next_upstream_id(self) -> str:
        with self._state_lock:
            self._seq_counter += 1
            return f"__sidefx_{self._proxy_nonce}_{self._seq_counter}"

    def _write_client_bytes(self, payload: bytes) -> None:
        with self._client_write_lock:
            try:
                self._client_out.write(payload)
                self._client_out.flush()
            except (BrokenPipeError, OSError):
                pass

    def _send_client_message(self, message: dict[str, Any]) -> None:
        self._write_client_bytes(_serialize_message(message))

    def _write_upstream_bytes(self, payload: bytes) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        with self._upstream_write_lock:
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def _read_framed_line(self, stream: BinaryIO) -> tuple[bytes | None, bool]:
        """Read one newline-delimited frame up to ``MAX_MESSAGE_BYTES``.

        Returns ``(line_bytes, overflowed)``. Returns ``(None, False)`` on EOF.
        """
        chunk = stream.readline(MAX_MESSAGE_BYTES + 1)
        if not chunk:
            return None, False
        if len(chunk) > MAX_MESSAGE_BYTES and not chunk.endswith(b"\n"):
            # Drain remainder of oversized line so framing stays synchronized.
            while True:
                extra = stream.readline(65536)
                if not extra or extra.endswith(b"\n"):
                    break
            return b"", True
        return chunk, False

    def _record_timed_out_id_locked(self, upstream_id: str, action_id: str) -> None:
        self._timed_out_upstream_ids[upstream_id] = action_id
        while len(self._timed_out_upstream_ids) > _MAX_TIMED_OUT_HISTORY:
            self._timed_out_upstream_ids.popitem(last=False)

    def _send_outcome_unknown(
        self,
        *,
        has_client_id: bool,
        client_id: Any,
        action_id: str,
        marker: str,
        tool_name: str,
        status: ActionStatus,
        detail: str,
    ) -> None:
        if not has_client_id:
            return
        message = (
            f"{OUTCOME_UNKNOWN_BANNER} "
            f"(action_id={action_id}, marker={marker}, tool={tool_name}): {detail}"
        )
        self._send_client_message(
            _jsonrpc_error_response(
                client_id,
                ERR_OUTCOME_UNKNOWN,
                message,
                data={
                    "kind": "outcome_unknown",
                    "action_id": action_id,
                    "marker": marker,
                    "tool": tool_name,
                    "status": status.value,
                    "detail": detail,
                },
            )
        )

    def _on_mutation_timeout(self, upstream_id: str) -> None:
        with self._pending_cond:
            entry = self._pending_mutations.get(upstream_id)
            if entry is None or entry.settled:
                return
            entry.settled = True
            self._pending_mutations.pop(upstream_id, None)
            self._record_timed_out_id_locked(upstream_id, entry.action_id)
            self._pending_cond.notify_all()

        detail = f"upstream MCP server timed out after {self._timeout_seconds:g}s"
        try:
            self._journal.mark_unknown(
                self._actor,
                self._target,
                entry.action_id,
                attempt=entry.attempt,
                error=detail,
            )
        except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
            self._log_err(f"failed to mark timed-out action {entry.action_id} unknown: {exc}")

        self._send_outcome_unknown(
            has_client_id=entry.has_client_id,
            client_id=entry.client_id,
            action_id=entry.action_id,
            marker=entry.marker,
            tool_name=entry.tool_name,
            status=ActionStatus.UNKNOWN,
            detail=detail,
        )

    def _handle_upstream_response_for_mutation(
        self,
        upstream_id: str,
        msg: dict[str, Any],
    ) -> bool:
        """Handle an upstream JSON-RPC response if it matches an intercepted mutation.

        Returns True if the message belonged to an intercepted mutation (handled or dropped
        as a late response), False if it should be passed through verbatim to the client.
        """
        with self._pending_cond:
            entry = self._pending_mutations.get(upstream_id)
            if entry is not None:
                if entry.settled:
                    return True
                entry.settled = True
                self._pending_mutations.pop(upstream_id, None)
                timer = entry.timer
                self._pending_cond.notify_all()
            else:
                late_action_id = self._timed_out_upstream_ids.pop(upstream_id, None)
                if late_action_id is not None:
                    self._log_err(
                        f"dropping late upstream response for timed-out action {late_action_id}"
                    )
                    return True
                return False

        if timer is not None:
            timer.cancel()

        # Finalize journal row BEFORE forwarding response back to the client.
        if "error" in msg and msg["error"] is not None:
            err_obj = msg["error"]
            if isinstance(err_obj, dict):
                err_code = err_obj.get("code")
                err_msg = err_obj.get("message", "upstream JSON-RPC error")
                err_text = _sanitize_text(
                    f"JSON-RPC error {err_code}: {err_msg}", max_len=_MAX_SUMMARY_LENGTH
                )
            else:
                err_text = _sanitize_text(str(err_obj), max_len=_MAX_SUMMARY_LENGTH)
            try:
                self._journal.mark_failed(
                    self._actor,
                    self._target,
                    entry.action_id,
                    attempt=entry.attempt,
                    error=err_text,
                )
            except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                self._log_err(f"failed to mark action {entry.action_id} failed: {exc}")
        else:
            result = msg.get("result")
            is_tool_error = isinstance(result, dict) and result.get("isError") is True
            if is_tool_error:
                err_text = _extract_tool_error_text(result)
                try:
                    self._journal.mark_failed(
                        self._actor,
                        self._target,
                        entry.action_id,
                        attempt=entry.attempt,
                        error=err_text,
                    )
                except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                    self._log_err(f"failed to mark action {entry.action_id} failed: {exc}")
            else:
                result_ref, summary = _extract_result_ref_and_summary(result)
                try:
                    self._journal.mark_confirmed(
                        self._actor,
                        self._target,
                        entry.action_id,
                        attempt=entry.attempt,
                        result_ref=result_ref,
                        summary=summary,
                    )
                except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                    self._log_err(f"failed to mark action {entry.action_id} confirmed: {exc}")
                if isinstance(result, dict):
                    with self._state_lock:
                        self._confirmed_cache[(self._actor, self._target, entry.action_id)] = (
                            copy.deepcopy(result)
                        )

        if entry.has_client_id:
            outbound = dict(msg)
            outbound["id"] = entry.client_id
            self._send_client_message(outbound)
        return True

    def _fail_all_inflight_on_upstream_eof(self) -> None:
        with self._pending_cond:
            self._upstream_closed = True
            orphaned: list[_InFlightMutation] = []
            for entry in self._pending_mutations.values():
                if not entry.settled:
                    entry.settled = True
                    orphaned.append(entry)
            self._pending_mutations.clear()
            self._pending_cond.notify_all()

        detail = "upstream MCP server closed connection or crashed before responding"
        for entry in orphaned:
            if entry.timer is not None:
                entry.timer.cancel()
            try:
                self._journal.mark_unknown(
                    self._actor,
                    self._target,
                    entry.action_id,
                    attempt=entry.attempt,
                    error=detail,
                )
            except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                self._log_err(f"failed to mark orphaned action {entry.action_id} unknown: {exc}")
            self._send_outcome_unknown(
                has_client_id=entry.has_client_id,
                client_id=entry.client_id,
                action_id=entry.action_id,
                marker=entry.marker,
                tool_name=entry.tool_name,
                status=ActionStatus.UNKNOWN,
                detail=detail,
            )

    def _upstream_reader_loop(self, upstream_stdout: BinaryIO) -> None:
        try:
            while True:
                raw_line, overflowed = self._read_framed_line(upstream_stdout)
                if raw_line is None:
                    break
                if overflowed:
                    self._log_err("dropped oversized frame from upstream MCP server")
                    continue
                if not raw_line.strip():
                    continue
                try:
                    decoded = raw_line.decode("utf-8")
                    msg = json.loads(decoded, parse_constant=_reject_json_constant)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    self._log_err(f"ignored non-JSON line from upstream stdout: {exc}")
                    continue

                if isinstance(msg, dict) and "method" not in msg and "id" in msg:
                    msg_id = msg["id"]
                    if isinstance(msg_id, str) and self._handle_upstream_response_for_mutation(
                        msg_id, msg
                    ):
                        continue

                # Passthrough response, notification, or server-initiated request.
                if isinstance(msg, dict):
                    self._send_client_message(msg)
                else:
                    self._log_err("ignored non-object JSON message from upstream stdout")
        finally:
            self._fail_all_inflight_on_upstream_eof()

    def _stderr_pump_loop(self, upstream_stderr: BinaryIO) -> None:
        try:
            while True:
                chunk = upstream_stderr.read(4096)
                if not chunk:
                    break
                if self._err_stream is not None:
                    try:
                        self._err_stream.write(chunk)
                        self._err_stream.flush()
                    except OSError:
                        break
        except OSError:
            pass

    def _send_replay_for_record(
        self,
        *,
        has_client_id: bool,
        client_id: Any,
        record: ActionRecord,
        explicit_result: dict[str, Any] | None = None,
    ) -> None:
        if not has_client_id:
            return
        if explicit_result is not None:
            result = copy.deepcopy(explicit_result)
        else:
            with self._state_lock:
                cached = self._confirmed_cache.get((record.actor, record.target, record.action_id))
            result = copy.deepcopy(cached) if cached is not None else _build_replay_result(record)
        self._send_client_message(_jsonrpc_result_response(client_id, result))

    def _is_action_currently_inflight(self, action_id: str) -> bool:
        with self._state_lock:
            return any(
                not entry.settled and entry.action_id == action_id
                for entry in self._pending_mutations.values()
            )

    def _try_reconcile_unknown(
        self,
        record: ActionRecord,
        *,
        has_client_id: bool,
        client_id: Any,
    ) -> str:
        """Attempt recovery via ``reconciler_hook``.

        Returns:
          - ``"handled"`` if a response was already sent to the client (recovered or blocked).
          - ``"retry"`` if reconciler proved ``not_found`` and transitioned row to ``failed``.
        """
        # Never reconcile a row that is still actively in-flight in this proxy instance.
        if self._reconciler_hook is None or self._is_action_currently_inflight(record.action_id):
            return "block"

        try:
            hook_raw = self._reconciler_hook(record)
        except Exception as exc:
            self._log_err(f"reconciler hook raised for action {record.action_id}: {exc}")
            return "block"

        hook_res = _coerce_reconcile_hook_result(hook_raw)
        if hook_res is None or hook_res.state == ReconcileState.UNKNOWN:
            return "block"

        if hook_res.state == ReconcileState.FOUND:
            ref = _sanitize_text(hook_res.result_ref, max_len=_MAX_REF_LENGTH)
            summary = _sanitize_text(
                hook_res.summary or hook_res.details, max_len=_MAX_SUMMARY_LENGTH
            )
            try:
                reconciled = self._journal.mark_reconciled(
                    record.actor,
                    record.target,
                    record.action_id,
                    attempt=record.attempt,
                    result_ref=ref,
                    summary=summary,
                )
            except (StaleAttemptError, InvalidStateTransitionError):
                latest = self._journal.get_action(record.actor, record.target, record.action_id)
                if latest is not None and latest.status in (
                    ActionStatus.CONFIRMED,
                    ActionStatus.RECONCILED,
                ):
                    self._send_replay_for_record(
                        has_client_id=has_client_id,
                        client_id=client_id,
                        record=latest,
                    )
                    return "handled"
                return "block"
            except JournalError as exc:
                self._log_err(f"failed to persist reconciliation for {record.action_id}: {exc}")
                return "block"

            if hook_res.cached_result is not None:
                with self._state_lock:
                    self._confirmed_cache[(record.actor, record.target, record.action_id)] = (
                        copy.deepcopy(hook_res.cached_result)
                    )
            self._send_replay_for_record(
                has_client_id=has_client_id,
                client_id=client_id,
                record=reconciled,
                explicit_result=hook_res.cached_result,
            )
            return "handled"

        # ReconcileState.NOT_FOUND: only safe to transition an `unknown` record to `failed`.
        if record.status != ActionStatus.UNKNOWN:
            return "block"
        try:
            self._journal.mark_failed(
                record.actor,
                record.target,
                record.action_id,
                attempt=record.attempt,
                error=_sanitize_text(
                    hook_res.details or "reconciler confirmed action did not land",
                    max_len=_MAX_SUMMARY_LENGTH,
                ),
            )
        except (StaleAttemptError, InvalidStateTransitionError, JournalError):
            return "block"
        return "retry"

    def _handle_tools_call(
        self,
        msg: dict[str, Any],
        *,
        has_client_id: bool,
        client_id: Any,
    ) -> None:
        params = msg.get("params")
        if not isinstance(params, dict):
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid params: tools/call requires an object 'params'",
                    )
                )
            return

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid params: tools/call requires a non-empty string 'name'",
                    )
                )
            return

        raw_arguments = params.get("arguments", _MISSING)
        if raw_arguments is _MISSING or raw_arguments is None:
            tool_args: dict[str, Any] = {}
        elif isinstance(raw_arguments, dict):
            tool_args = raw_arguments
        else:
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid params: tools/call 'arguments' must be an object",
                    )
                )
            return

        tool_kind = classify_tool(tool_name, self._config)

        # Read-only calls pass straight through without journaling.
        if tool_kind == ToolKind.READ:
            self._forward_passthrough(msg, has_client_id=has_client_id, client_id=client_id)
            return

        # Extract explicit action ID before stripping private `_sidefx_*` metadata keys.
        explicit_action_id: str | None = None
        if "_sidefx_action_id" in tool_args:
            raw_aid = tool_args["_sidefx_action_id"]
            if not isinstance(raw_aid, str) or not raw_aid.strip() or "\x00" in raw_aid:
                if has_client_id:
                    self._send_client_message(
                        _jsonrpc_error_response(
                            client_id,
                            JSONRPC_INVALID_PARAMS,
                            "Invalid params: '_sidefx_action_id' must be a non-empty string",
                        )
                    )
                return
            explicit_action_id = raw_aid.strip()
        elif "idempotency_key" in tool_args:
            raw_ik = tool_args["idempotency_key"]
            if isinstance(raw_ik, str) and raw_ik.strip() and "\x00" not in raw_ik:
                explicit_action_id = raw_ik.strip()

        forwarded_args = {k: v for k, v in tool_args.items() if not k.startswith("_sidefx_")}

        # Run deterministic preflight checks BEFORE journaling or touching upstream.
        preflight_res = evaluate_preflight(tool_name, forwarded_args, self._config)
        if not preflight_res.allowed:
            if has_client_id:
                summary = "; ".join(preflight_res.reasons)
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        ERR_PREFLIGHT_DENIED,
                        f"Preflight check denied tool {tool_name!r}: {summary}",
                        data={
                            "kind": "preflight_denied",
                            "tool": tool_name,
                            "tool_kind": preflight_res.tool_kind.value,
                            "violations": [v.to_dict() for v in preflight_res.violations],
                        },
                    )
                )
            return

        # Compute canonical payload hash over (tool_name, forwarded_args).
        canonical_payload = {"tool": tool_name, "arguments": forwarded_args}
        try:
            payload_hash = compute_payload_hash(canonical_payload)
        except (TypeError, ValueError) as exc:
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Invalid tool arguments for canonical hashing: {exc}",
                    )
                )
            return

        if explicit_action_id is not None:
            action_id = explicit_action_id
        elif self._deterministic_ids:
            action_id = _derive_deterministic_action_id(
                session_id=self._session_id,
                actor=self._actor,
                target=self._target,
                tool_name=tool_name,
                payload_hash=payload_hash,
            )
        else:
            action_id = f"mcp-{uuid.uuid4().hex}"

        # Write-ahead durable claim BEFORE dispatching to upstream.
        begin_res = self._begin_or_reconcile(
            action_id=action_id,
            tool_name=tool_name,
            payload_hash=payload_hash,
            has_client_id=has_client_id,
            client_id=client_id,
        )
        if begin_res is None:
            return

        record = begin_res.record

        # Build forwarded message with `_sidefx_*` keys stripped and unique upstream request ID.
        upstream_id = self._next_upstream_id()
        forwarded_params = dict(params)
        if raw_arguments is not _MISSING or forwarded_args:
            forwarded_params["arguments"] = forwarded_args
        forwarded_msg = dict(msg)
        forwarded_msg["id"] = upstream_id
        forwarded_msg["params"] = forwarded_params

        entry = _InFlightMutation(
            upstream_id=upstream_id,
            client_id=client_id,
            has_client_id=has_client_id,
            action_id=record.action_id,
            marker=record.marker,
            tool_name=tool_name,
            attempt=record.attempt,
        )
        timer = threading.Timer(
            self._timeout_seconds,
            self._on_mutation_timeout,
            args=(upstream_id,),
        )
        timer.daemon = True
        entry.timer = timer

        with self._pending_cond:
            if self._upstream_closed:
                upstream_dead = True
            else:
                upstream_dead = False
                self._pending_mutations[upstream_id] = entry

        if upstream_dead:
            detail = "upstream MCP server is not running"
            with contextlib.suppress(StaleAttemptError, InvalidStateTransitionError, JournalError):
                self._journal.mark_failed(
                    self._actor,
                    self._target,
                    record.action_id,
                    attempt=record.attempt,
                    error=detail,
                )
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        ERR_UPSTREAM_UNAVAILABLE,
                        f"Upstream MCP server unavailable: {detail}",
                        data={
                            "kind": "upstream_unavailable",
                            "action_id": record.action_id,
                            "tool": tool_name,
                        },
                    )
                )
            return

        timer.start()
        sent = self._write_upstream_bytes(_serialize_message(forwarded_msg))
        if not sent:
            # Pipe broke during write: fail-safe as unknown because partial delivery is possible.
            with self._pending_cond:
                still_active = not entry.settled
                if still_active:
                    entry.settled = True
                    self._pending_mutations.pop(upstream_id, None)
                    self._pending_cond.notify_all()
            if still_active:
                timer.cancel()
                detail = "failed to write request to upstream MCP server stdin"
                try:
                    self._journal.mark_unknown(
                        self._actor,
                        self._target,
                        record.action_id,
                        attempt=record.attempt,
                        error=detail,
                    )
                except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                    self._log_err(f"failed to mark write-failed action unknown: {exc}")
                self._send_outcome_unknown(
                    has_client_id=has_client_id,
                    client_id=client_id,
                    action_id=record.action_id,
                    marker=record.marker,
                    tool_name=tool_name,
                    status=ActionStatus.UNKNOWN,
                    detail=detail,
                )

    def _begin_or_reconcile(
        self,
        *,
        action_id: str,
        tool_name: str,
        payload_hash: str,
        has_client_id: bool,
        client_id: Any,
    ) -> Any:
        """Run `journal.begin_action`, handling replay, mismatch, and reconciler recovery."""
        for attempt_round in range(2):
            try:
                begin_res = self._journal.begin_action(
                    action_id=action_id,
                    session_id=self._session_id,
                    actor=self._actor,
                    surface=Surface.MCP,
                    tool_name=tool_name,
                    target=self._target,
                    payload_hash=payload_hash,
                )
            except PayloadMismatchError as exc:
                if has_client_id:
                    self._send_client_message(
                        _jsonrpc_error_response(
                            client_id,
                            ERR_PAYLOAD_MISMATCH,
                            str(exc),
                            data={
                                "kind": "payload_mismatch",
                                "action_id": action_id,
                                "expected_hash": exc.expected_hash,
                                "actual_hash": exc.actual_hash,
                            },
                        )
                    )
                return None
            except (JournalError, ValueError) as exc:
                if has_client_id:
                    self._send_client_message(
                        _jsonrpc_error_response(
                            client_id,
                            ERR_JOURNAL_ERROR,
                            f"sidefx journal error: {exc}",
                            data={"kind": "journal_error", "action_id": action_id},
                        )
                    )
                return None

            if begin_res.decision == BeginDecision.PROCEED:
                return begin_res

            if begin_res.decision == BeginDecision.REPLAY:
                self._send_replay_for_record(
                    has_client_id=has_client_id,
                    client_id=client_id,
                    record=begin_res.record,
                )
                return None

            # BeginDecision.OUTCOME_UNKNOWN
            if attempt_round == 0:
                reconcile_outcome = self._try_reconcile_unknown(
                    begin_res.record,
                    has_client_id=has_client_id,
                    client_id=client_id,
                )
                if reconcile_outcome == "handled":
                    return None
                if reconcile_outcome == "retry":
                    continue

            detail = (
                begin_res.record.error
                or f"prior attempt is in state {begin_res.record.status.value!r}"
            )
            self._send_outcome_unknown(
                has_client_id=has_client_id,
                client_id=client_id,
                action_id=begin_res.record.action_id,
                marker=begin_res.record.marker,
                tool_name=begin_res.record.tool_name,
                status=begin_res.record.status,
                detail=detail,
            )
            return None

        return None

    def _forward_passthrough(
        self,
        msg: dict[str, Any],
        *,
        has_client_id: bool,
        client_id: Any,
    ) -> None:
        with self._state_lock:
            dead = self._upstream_closed
        if (dead or not self._write_upstream_bytes(_serialize_message(msg))) and has_client_id:
            self._send_client_message(
                _jsonrpc_error_response(
                    client_id,
                    ERR_UPSTREAM_UNAVAILABLE,
                    "Upstream MCP server is unavailable",
                    data={"kind": "upstream_unavailable"},
                )
            )

    def _handle_client_line(self, raw_line: bytes, overflowed: bool) -> None:
        if overflowed:
            self._send_client_message(
                _jsonrpc_error_response(
                    None,
                    JSONRPC_PARSE_ERROR,
                    f"Parse error: message exceeds {MAX_MESSAGE_BYTES} bytes limit",
                )
            )
            return
        if not raw_line.strip():
            return
        try:
            decoded = raw_line.decode("utf-8")
            msg = json.loads(decoded, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            self._send_client_message(
                _jsonrpc_error_response(
                    None,
                    JSONRPC_PARSE_ERROR,
                    f"Parse error: {exc}",
                )
            )
            return

        if not isinstance(msg, dict):
            self._send_client_message(
                _jsonrpc_error_response(
                    None,
                    JSONRPC_INVALID_REQUEST,
                    "Invalid Request: JSON-RPC message must be a JSON object",
                )
            )
            return

        has_client_id = "id" in msg
        client_id = msg.get("id")
        method = msg.get("method")

        if method == "tools/call":
            self._handle_tools_call(msg, has_client_id=has_client_id, client_id=client_id)
            return

        # Pass through protocol methods, notifications, and client replies to server requests.
        is_request_or_notification = isinstance(method, str) and bool(method)
        is_response = "method" not in msg and has_client_id and ("result" in msg or "error" in msg)
        if not is_request_or_notification and not is_response:
            self._send_client_message(
                _jsonrpc_error_response(
                    client_id if has_client_id else None,
                    JSONRPC_INVALID_REQUEST,
                    "Invalid Request: missing valid 'method' or response payload",
                )
            )
            return

        self._forward_passthrough(msg, has_client_id=has_client_id, client_id=client_id)

    def run(self) -> int:
        """Spawn the upstream MCP server and proxy stdio JSON-RPC messages until EOF."""
        stderr_pipe = subprocess.PIPE if self._err_stream is not None else None
        try:
            proc = subprocess.Popen(
                self._upstream_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_pipe,
                env=self._upstream_env,
            )
        except OSError as exc:
            self._log_err(f"failed to launch upstream command {self._upstream_cmd[0]!r}: {exc}")
            return 1

        self._proc = proc
        assert proc.stdin is not None
        assert proc.stdout is not None

        reader_thread = threading.Thread(
            target=self._upstream_reader_loop,
            args=(proc.stdout,),
            name="sidefx-mcp-upstream-reader",
            daemon=True,
        )
        reader_thread.start()

        stderr_thread: threading.Thread | None = None
        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=self._stderr_pump_loop,
                args=(proc.stderr,),
                name="sidefx-mcp-upstream-stderr",
                daemon=True,
            )
            stderr_thread.start()

        try:
            while True:
                raw_line, overflowed = self._read_framed_line(self._client_in)
                if raw_line is None:
                    break
                self._handle_client_line(raw_line, overflowed)
        finally:
            self._shutdown(proc, reader_thread, stderr_thread)

        return 0

    def _shutdown(
        self,
        proc: subprocess.Popen[bytes],
        reader_thread: threading.Thread,
        stderr_thread: threading.Thread | None,
    ) -> None:
        # Wait for in-flight intercepted mutations to settle (response or timeout) before closing
        # upstream stdin so piped single-shot clients still receive their responses.
        with self._pending_cond:
            while self._pending_mutations and not self._upstream_closed:
                self._pending_cond.wait(timeout=0.5)

        # Signal clean EOF to upstream stdin.
        with self._upstream_write_lock:
            if proc.stdin is not None:
                with contextlib.suppress(OSError):
                    proc.stdin.close()

        # Allow upstream to finish sending any queued passthrough responses and exit cleanly.
        reader_thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)

        if proc.poll() is None:
            try:
                proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        reader_thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)

        # Cancel any leftover timers defensively.
        with self._pending_cond:
            for entry in self._pending_mutations.values():
                if entry.timer is not None:
                    entry.timer.cancel()
            self._pending_mutations.clear()

        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()
        if proc.stderr is not None:
            with contextlib.suppress(OSError):
                proc.stderr.close()


def _positive_finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    if value > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"timeout must be <= {int(MAX_TIMEOUT_SECONDS)}")
    return value


def build_mcp_proxy_parser() -> argparse.ArgumentParser:
    """Build argument parser for `sidefx mcp-proxy [options] -- <upstream cmd ...>`."""
    parser = argparse.ArgumentParser(
        prog="sidefx mcp-proxy",
        usage="sidefx mcp-proxy [options] -- <upstream_cmd ...>",
        description=(
            "Run a transparent stdio JSON-RPC 2.0 MCP proxy with write-ahead SQLite "
            "journaling and deterministic preflight checks."
        ),
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Path to SQLite journal database (overrides SIDEFX_DB and config).",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to .sidefx.json configuration file.",
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Session identifier recorded on journaled MCP mutations.",
    )
    parser.add_argument(
        "--actor",
        dest="actor",
        default=None,
        help="Actor name recorded in the journal (defaults to config default_actor).",
    )
    parser.add_argument(
        "--target",
        dest="target",
        default=None,
        help="Target identifier for journal deduplication scope.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=_positive_finite_float,
        default=None,
        help="Per-call upstream timeout in seconds for mutating tools/call requests.",
    )
    parser.add_argument(
        "--deterministic-ids",
        dest="deterministic_ids",
        action="store_true",
        help=(
            "Derive deterministic session-scoped action IDs from "
            "(session_id, tool_name, arguments) when no explicit key is supplied."
        ),
    )
    return parser


def parse_mcp_proxy_args(raw_args: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse `mcp-proxy` flags and split upstream command after `--`."""
    parser = build_mcp_proxy_parser()
    tokens = list(raw_args)
    if "--" in tokens:
        sep = tokens.index("--")
        proxy_tokens = tokens[:sep]
        upstream_cmd = tokens[sep + 1 :]
    else:
        proxy_tokens = tokens
        upstream_cmd = []

    parsed = parser.parse_args(proxy_tokens)
    if not upstream_cmd:
        parser.error("missing upstream command after '--'")
    return parsed, upstream_cmd


def handle_mcp_proxy_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler invoked by `sidefx.cli` for `sidefx mcp-proxy`."""
    raw_args: list[str] = list(getattr(args, "args", []))
    parsed, upstream_cmd = parse_mcp_proxy_args(raw_args)

    effective_config = load_config(parsed.config_path) if parsed.config_path is not None else config
    db_override = parsed.db_path if parsed.db_path is not None else getattr(args, "db_path", None)
    session_override = parsed.session_id or getattr(args, "session_id", None)
    db_path = resolve_db_path(db_override, config=effective_config)

    with Journal(db_path) as journal:
        proxy = MCPProxy(
            upstream_cmd,
            journal=journal,
            config=effective_config,
            session_id=session_override,
            actor=parsed.actor,
            target=parsed.target,
            timeout_seconds=parsed.timeout,
            deterministic_ids=parsed.deterministic_ids,
        )
        return proxy.run()
