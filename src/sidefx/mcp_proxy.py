"""Transparent stdio JSON-RPC 2.0 MCP proxy with write-ahead journaling and preflight checks."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from sidefx.config import MAX_TIMEOUT_SECONDS, SidefxConfig, load_config
from sidefx.guidance import OUTCOME_UNKNOWN_BANNER as OUTCOME_UNKNOWN_BANNER
from sidefx.guidance import (
    mcp_initialize_instructions,
    outcome_unknown_data,
    outcome_unknown_message,
    payload_mismatch_message,
    preflight_denied_message,
    replay_notice,
)
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
from sidefx.preflight import ToolKind, evaluate_preflight
from sidefx.session import ENV_SESSION_ID as ENV_SESSION_ID
from sidefx.session import resolve_session_id

MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 MiB per newline-delimited JSON-RPC frame
_MAX_TIMED_OUT_HISTORY = 10_000
_MAX_CONFIRMED_CACHE_ENTRIES = 256
_MAX_CONFIRMED_CACHE_BYTES = 8 * 1024 * 1024  # 8 MiB total in-memory replay cache budget
_MAX_SINGLE_CACHE_BYTES = 1 * 1024 * 1024  # Skip caching individual responses > 1 MiB
_SHUTDOWN_GRACE_SECONDS = 2.0
_MAX_REF_LENGTH = 500
_MAX_SUMMARY_LENGTH = 1000
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+")
_RESULT_REF_KEYS = ("result_ref", "url", "html_url", "uri", "id")
_AMBIGUOUS_ERROR_RE = re.compile(
    r"(?:"
    r"timed?\s*out|timeout|etimedout|esockettimedout|"
    r"deadline[\s_]*exceeded|"
    r"econnreset|connection\s+reset|socket\s+hang\s+up|"
    r"broken\s+pipe|epipe|eof\s+before|unexpected\s+eof|"
    r"fetch\s+failed|network\s+error|econnaborted|"
    r"502\s+bad\s+gateway|503\s+service\s+unavailable|504\s+gateway"
    r")",
    re.IGNORECASE,
)

# Standard JSON-RPC 2.0 and sidefx application error codes
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
ERR_PREFLIGHT_DENIED = -32001
ERR_OUTCOME_UNKNOWN = -32002
ERR_UPSTREAM_UNAVAILABLE = -32003
ERR_PAYLOAD_MISMATCH = -32004
ERR_JOURNAL_ERROR = -32005

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
    upstream_gen: int = 0
    timer: threading.Timer | None = None
    settled: bool = False


@dataclass(frozen=True, slots=True)
class _TimedOutMutation:
    """Tracks an upstream mutation that timed out so late responses and retries stay safe."""

    upstream_id: str
    client_id: Any
    has_client_id: bool
    action_id: str
    marker: str
    tool_name: str
    attempt: int


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {token!r}")


def _parse_finite_float(raw: str) -> float:
    val = float(raw)
    if not math.isfinite(val):
        raise ValueError(f"Non-finite float literal: {raw!r}")
    return val


def _loads_strict_json(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        parse_float=_parse_finite_float,
    )


def _rpc_id_key(rpc_id: Any) -> tuple[type, Any]:
    """Build a collision-free dict key distinguishing e.g. `0`, `False`, `1`, `1.0`, `'1'`."""
    return (type(rpc_id), rpc_id)


def _sanitize_text(text: str | None, *, max_len: int | None = None) -> str | None:
    """Strip NUL bytes and replace lone UTF-16 surrogates so SQLite UTF-8 encoding never fails."""
    if text is None:
        return None
    cleaned = text.replace("\x00", "")
    # Replace lone surrogates cleanly so SQLite accepts the string without UnicodeEncodeError.
    cleaned = cleaned.encode("utf-8", errors="replace").decode("utf-8")
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
    """Serialize a JSON-RPC message as ASCII-safe UTF-8 JSON + newline.

    ``ensure_ascii=True`` ensures lone UTF-16 surrogates (e.g. from sliced JS strings)
    round-trip cleanly as ``\\uXXXX`` escapes instead of raising ``UnicodeEncodeError``.
    """
    encoded = json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return encoded.encode("ascii") + b"\n"


def _serialize_json_value(value: Any) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _build_cached_response_frame(client_id: Any, cached_result_json: bytes) -> bytes:
    id_bytes = _serialize_json_value(client_id)
    return b'{"jsonrpc":"2.0","id":' + id_bytes + b',"result":' + cached_result_json + b"}\n"


def _is_ambiguous_error_text(error_text: str | None) -> bool:
    if not error_text:
        return False
    return _AMBIGUOUS_ERROR_RE.search(error_text) is not None


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
        if joined_text.startswith("{") and joined_text.endswith("}"):
            try:
                parsed_text = _loads_strict_json(joined_text)
            except (ValueError, RecursionError):
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
                json.dumps(structured, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                max_len=_MAX_SUMMARY_LENGTH,
            )
        except (TypeError, ValueError, RecursionError):
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


def _result_with_replay_notice(record: ActionRecord, cached_bytes: bytes | None) -> dict[str, Any]:
    """Return the original (or synthesized) result with a duplicate-call notice appended."""
    base: dict[str, Any] | None = None
    if cached_bytes is not None:
        try:
            parsed = _loads_strict_json(cached_bytes.decode("ascii"))
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            base = parsed
    result = dict(base) if base is not None else _build_replay_result(record)
    notice = {
        "type": "text",
        "text": replay_notice(
            tool_name=record.tool_name,
            action_id=record.action_id,
            status=record.status.value,
        ),
    }
    content = result.get("content")
    result["content"] = [*content, notice] if isinstance(content, list) else [notice]
    return result


_PYTHON_EXE_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$", re.IGNORECASE)
_TARGET_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_GENERIC_SCRIPT_STEMS = frozenset({"__main__", "index", "main"})
_GENERIC_PARENT_DIRS = frozenset({"", ".", "..", "src", "dist", "build", "bin", "lib", "out"})


def _sanitize_target_label(raw: str) -> str:
    cleaned = _TARGET_LABEL_UNSAFE_RE.sub("-", raw.strip()).strip("-.")
    return cleaned[:48]


def _extract_npm_package_label(token: str) -> str:
    """Extract readable package name from npm specifiers like ``@scope/server-github@1.2``."""
    spec = token.strip()
    if spec.startswith("@") and "/" in spec:
        # Scoped package: @scope/pkg or @scope/pkg@version
        _, after_scope = spec.split("/", 1)
        name = after_scope.split("@", 1)[0]
    else:
        name = spec.split("@", 1)[0]
    return _sanitize_target_label(name)


def _extract_uv_package_label(token: str) -> str:
    """Extract readable package name from uv/uvx specifiers like ``pkg[extra]==1.0``."""
    spec = token.strip()
    if "://" in spec:
        tail = spec.rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        return _sanitize_target_label(tail)
    name = re.split(r"[@\[=<>!~;]", spec, maxsplit=1)[0]
    return _sanitize_target_label(name)


def _extract_script_label(token: str) -> str:
    """Extract a readable label from a script path like ``/path/to/my-mcp/index.js``."""
    p = Path(token)
    stem = p.stem
    if stem.lower() in _GENERIC_SCRIPT_STEMS:
        parent = p.parent.name
        if parent and parent.lower() not in _GENERIC_PARENT_DIRS:
            return _sanitize_target_label(parent)
    return _sanitize_target_label(stem or p.name)


def _extract_readable_target_label(upstream_cmd: Sequence[str], *, _depth: int = 0) -> str:
    """Unwrap common MCP launcher commands into a readable target label."""
    if not upstream_cmd or _depth > 3:
        return "upstream"

    base = Path(upstream_cmd[0]).name.strip() or "upstream"
    lower_base = base.lower()
    args = list(upstream_cmd[1:])

    # 1. npx / pnpx / bunx / pnpm dlx / yarn dlx / npm exec
    if lower_base in ("npx", "npx.cmd", "pnpx", "bunx"):
        return _extract_npx_label(args) or _sanitize_target_label(base) or "upstream"
    if lower_base in ("pnpm", "pnpm.cmd", "yarn", "yarn.cmd") and args and args[0] == "dlx":
        return _extract_npx_label(args[1:]) or _sanitize_target_label(base) or "upstream"
    if lower_base in ("npm", "npm.cmd") and args and args[0] == "exec":
        return _extract_npx_label(args[1:]) or _sanitize_target_label(base) or "upstream"

    # 2. uvx
    if lower_base in ("uvx", "uvx.exe"):
        label = _extract_uvx_label(args)
        return label or _sanitize_target_label(base) or "upstream"

    # 3. uv run <subcommand ...>
    if lower_base in ("uv", "uv.exe"):
        idx = 0
        uv_global_val_flags = {"--directory", "--project", "-C", "--config-file", "--cache-dir"}
        while idx < len(args) and args[idx].startswith("-"):
            tok = args[idx]
            idx += 2 if tok in uv_global_val_flags else 1
        if idx < len(args) and args[idx] == "run":
            run_args = args[idx + 1 :]
            r_idx = 0
            uv_run_val_flags = {
                "--with",
                "--with-editable",
                "--with-requirements",
                "--directory",
                "--project",
                "-C",
                "--python",
                "-p",
                "--package",
                "--extra",
                "--group",
                "--only-group",
                "--env-file",
                "--index-url",
                "--extra-index-url",
            }
            while r_idx < len(run_args):
                tok = run_args[r_idx]
                if tok == "--":
                    r_idx += 1
                    break
                if tok in uv_run_val_flags:
                    r_idx += 2
                    continue
                if tok.startswith("-"):
                    r_idx += 1
                    continue
                break
            if r_idx < len(run_args):
                return _extract_readable_target_label(run_args[r_idx:], _depth=_depth + 1)

    # 4. python / python3 / python3.x
    if _PYTHON_EXE_RE.match(lower_base):
        idx = 0
        py_val_flags = {"-X", "-W"}
        while idx < len(args):
            tok = args[idx]
            if tok == "--":
                idx += 1
                break
            if tok == "-m" and idx + 1 < len(args):
                mod = args[idx + 1].strip()
                if mod.endswith(".__main__"):
                    mod = mod[: -len(".__main__")]
                return _sanitize_target_label(mod) or _sanitize_target_label(base) or "upstream"
            if tok == "-c":
                return _sanitize_target_label(base) or "upstream"
            if tok in py_val_flags:
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            return _extract_script_label(tok) or _sanitize_target_label(base) or "upstream"
        return _sanitize_target_label(base) or "upstream"

    # 5. node / bun / deno
    if lower_base in ("node", "node.exe", "bun", "bun.exe", "deno", "deno.exe"):
        idx = 0
        if lower_base.startswith(("deno", "bun")) and idx < len(args) and args[idx] == "run":
            idx += 1
        js_val_flags = {
            "-r",
            "--require",
            "--loader",
            "--import",
            "--env-file",
            "--conditions",
            "-C",
            "-c",
            "--config",
            "--import-map",
            "--lock",
            "--cwd",
        }
        while idx < len(args):
            tok = args[idx]
            if tok == "--":
                idx += 1
                break
            if tok in ("-e", "--eval", "-p", "--print"):
                return _sanitize_target_label(base) or "upstream"
            if tok in js_val_flags:
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            return _extract_script_label(tok) or _sanitize_target_label(base) or "upstream"
        return _sanitize_target_label(base) or "upstream"

    # 6. docker / podman run ... <image>
    if lower_base in ("docker", "podman") and args and args[0] == "run":
        idx = 1
        docker_val_flags = {
            "-e",
            "--env",
            "--env-file",
            "-v",
            "--volume",
            "--mount",
            "-p",
            "--publish",
            "--network",
            "--name",
            "-w",
            "--workdir",
            "-u",
            "--user",
            "--platform",
            "--entrypoint",
            "-l",
            "--label",
            "--pull",
        }
        while idx < len(args):
            tok = args[idx]
            if tok == "--":
                idx += 1
                break
            if tok in docker_val_flags:
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            image_no_digest = tok.split("@", 1)[0]
            tail = image_no_digest.rsplit("/", 1)[-1]
            image_name = tail.split(":", 1)[0]
            return _sanitize_target_label(image_name) or _sanitize_target_label(base) or "upstream"

    return _sanitize_target_label(base) or "upstream"


def _extract_npx_label(args: Sequence[str]) -> str:
    val_flags = {"-p", "--package", "-c", "--call", "--prefix", "--cache"}
    pkg_from_flag: str | None = None
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            idx += 1
            if idx < len(args):
                return _extract_npm_package_label(args[idx])
            break
        if tok in ("-p", "--package") and idx + 1 < len(args):
            if pkg_from_flag is None:
                pkg_from_flag = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("--package="):
            if pkg_from_flag is None:
                pkg_from_flag = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in val_flags:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return _extract_npm_package_label(tok)
    if pkg_from_flag:
        return _extract_npm_package_label(pkg_from_flag)
    return ""


def _extract_uvx_label(args: Sequence[str]) -> str:
    val_flags = {
        "--from",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--index-url",
        "--extra-index-url",
        "--directory",
        "--project",
        "-C",
    }
    from_pkg: str | None = None
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            idx += 1
            if idx < len(args):
                return _extract_uv_package_label(args[idx])
            break
        if tok == "--from" and idx + 1 < len(args):
            from_pkg = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("--from="):
            from_pkg = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in val_flags:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return _extract_uv_package_label(tok)
    if from_pkg:
        return _extract_uv_package_label(from_pkg)
    return ""


def _default_target_from_cmd(upstream_cmd: Sequence[str]) -> str:
    """Derive a readable, deterministic target identifier from the full upstream command."""
    if not upstream_cmd:
        return "mcp:upstream"
    label = _extract_readable_target_label(upstream_cmd)
    cmd_digest = hashlib.sha256(
        json.dumps(list(upstream_cmd), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()[:8]
    return f"mcp:{label}:{cmd_digest}"


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


def _kill_upstream_tree(proc: subprocess.Popen[bytes], pgid: int | None) -> bool:
    """Terminate an upstream process and its entire POSIX process group.

    Returns ``True`` only when the process tree is confirmed dead (so callers
    can safely release in-flight duplicate-dispatch guards).
    """
    if os.name == "posix" and pgid is not None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=0.3)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                # macOS returns EPERM transiently while a process group still holds unreaped
                # zombies before transitioning to ESRCH; keep polling until the deadline.
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=1.0)
    return proc.poll() is not None


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
        inject_instructions: bool = True,
        restart_on_timeout: bool = False,
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
        # Precedence: explicit arg > SIDEFX_SESSION_ID > CLAUDE_CODE_SESSION_ID > config >
        # persisted `<journal_dir>/current-session` (stable across restarts) > ephemeral.
        resolved_session, _ = resolve_session_id(
            explicit=session_id,
            config=self._config,
            db_path=self._journal.db_path,
            create=True,
            fallback_prefix="mcp",
        )
        assert resolved_session is not None
        self._session_id = resolved_session
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
        self._inject_instructions = bool(inject_instructions)
        self._restart_on_timeout = bool(restart_on_timeout)
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
        self._pending_passthrough: dict[tuple[type, Any], tuple[Any, int]] = {}
        self._timed_out_upstream_ids: OrderedDict[str, _TimedOutMutation] = OrderedDict()
        self._pending_init_ids: set[tuple[type, Any]] = set()
        self._internal_waiters: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._confirmed_cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
        self._confirmed_cache_bytes = 0
        self._active_finalizers = 0
        self._upstream_generation = 0
        self._upstream_closed = False
        self._needs_health_check = False
        self._reader_fatal_error = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_pgid: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._saw_initialize = False
        self._cached_init_params: dict[str, Any] = {}
        self._saw_initialized_notif = False
        self._cached_initialized_msg: dict[str, Any] | None = None

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

    def _cache_confirmed_result(self, key: tuple[str, str, str], result: Any) -> None:
        if not isinstance(result, dict):
            return
        try:
            serialized = _serialize_json_value(result)
        except (TypeError, ValueError, RecursionError):
            return
        size = len(serialized)
        if size > _MAX_SINGLE_CACHE_BYTES:
            return
        with self._state_lock:
            old = self._confirmed_cache.pop(key, None)
            if old is not None:
                self._confirmed_cache_bytes -= len(old)
            while self._confirmed_cache and (
                len(self._confirmed_cache) >= _MAX_CONFIRMED_CACHE_ENTRIES
                or self._confirmed_cache_bytes + size > _MAX_CONFIRMED_CACHE_BYTES
            ):
                _, evicted = self._confirmed_cache.popitem(last=False)
                self._confirmed_cache_bytes -= len(evicted)
            self._confirmed_cache[key] = serialized
            self._confirmed_cache_bytes += size

    def _get_cached_result_bytes(self, key: tuple[str, str, str]) -> bytes | None:
        with self._state_lock:
            cached = self._confirmed_cache.get(key)
            if cached is not None:
                self._confirmed_cache.move_to_end(key)
            return cached

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

    def _record_timed_out_entry_locked(self, entry: _InFlightMutation) -> None:
        self._timed_out_upstream_ids[entry.upstream_id] = _TimedOutMutation(
            upstream_id=entry.upstream_id,
            client_id=entry.client_id,
            has_client_id=entry.has_client_id,
            action_id=entry.action_id,
            marker=entry.marker,
            tool_name=entry.tool_name,
            attempt=entry.attempt,
        )
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
        db_path = self._journal_db_path()
        message = outcome_unknown_message(
            action_id=action_id,
            marker=marker,
            tool_name=tool_name,
            detail=detail,
            db_path=db_path,
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
                    **outcome_unknown_data(action_id, db_path=db_path),
                },
            )
        )

    def _journal_db_path(self) -> Path | None:
        """Absolute journal path, so recovery commands work from any working directory."""
        raw = self._journal.db_path
        if raw is None:
            return None
        try:
            return raw.resolve()
        except OSError:
            return raw

    def _on_mutation_timeout(self, upstream_id: str) -> None:
        with self._pending_cond:
            entry = self._pending_mutations.get(upstream_id)
            if entry is None or entry.settled:
                return
            entry.settled = True
            self._pending_mutations.pop(upstream_id, None)
            self._record_timed_out_entry_locked(entry)
            if self._restart_on_timeout:
                self._needs_health_check = True
            self._active_finalizers += 1

        try:
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
        finally:
            with self._pending_cond:
                self._active_finalizers -= 1
                self._pending_cond.notify_all()

    def _finalize_late_timed_out_response(
        self,
        timed_out: _TimedOutMutation,
        msg: dict[str, Any],
    ) -> None:
        """Reconcile a late upstream reply in SQLite while dropping it from client stdout."""
        if "error" in msg and msg["error"] is not None:
            err_obj = msg["error"]
            if isinstance(err_obj, dict):
                err_text = _sanitize_text(
                    f"JSON-RPC error {err_obj.get('code')}: "
                    f"{err_obj.get('message', 'upstream JSON-RPC error')}",
                    max_len=_MAX_SUMMARY_LENGTH,
                )
            else:
                err_text = _sanitize_text(str(err_obj), max_len=_MAX_SUMMARY_LENGTH)
            if not _is_ambiguous_error_text(err_text):
                with contextlib.suppress(
                    StaleAttemptError, InvalidStateTransitionError, JournalError
                ):
                    self._journal.mark_failed(
                        self._actor,
                        self._target,
                        timed_out.action_id,
                        attempt=timed_out.attempt,
                        error=err_text,
                    )
            return

        result = msg.get("result")
        if not isinstance(result, dict):
            return

        if result.get("isError") is True:
            err_text = _extract_tool_error_text(result)
            if not _is_ambiguous_error_text(err_text):
                with contextlib.suppress(
                    StaleAttemptError, InvalidStateTransitionError, JournalError
                ):
                    self._journal.mark_failed(
                        self._actor,
                        self._target,
                        timed_out.action_id,
                        attempt=timed_out.attempt,
                        error=err_text,
                    )
            return

        result_ref, summary = _extract_result_ref_and_summary(result)
        try:
            self._journal.mark_reconciled(
                self._actor,
                self._target,
                timed_out.action_id,
                attempt=timed_out.attempt,
                result_ref=result_ref,
                summary=summary,
            )
        except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
            self._log_err(
                f"failed to reconcile late upstream reply for {timed_out.action_id}: {exc}"
            )
            return
        self._cache_confirmed_result((self._actor, self._target, timed_out.action_id), result)

    def _handle_upstream_response_for_mutation(
        self,
        upstream_id: str,
        msg: dict[str, Any],
    ) -> bool:
        """Handle an upstream JSON-RPC response if it matches an intercepted mutation.

        Returns True if the message belonged to an intercepted mutation (handled or dropped
        as a late response), False if it should be passed through verbatim to the client.
        """
        late_entry: _TimedOutMutation | None = None
        with self._pending_cond:
            entry = self._pending_mutations.get(upstream_id)
            if entry is not None:
                if entry.settled:
                    return True
                entry.settled = True
                self._pending_mutations.pop(upstream_id, None)
                timer = entry.timer
                self._active_finalizers += 1
            else:
                late_entry = self._timed_out_upstream_ids.pop(upstream_id, None)
                if late_entry is not None:
                    self._active_finalizers += 1
                else:
                    return False

        if late_entry is not None:
            try:
                self._log_err(
                    f"reconciling and dropping late upstream response for timed-out action "
                    f"{late_entry.action_id}"
                )
                self._finalize_late_timed_out_response(late_entry, msg)
            finally:
                with self._pending_cond:
                    self._active_finalizers -= 1
                    self._pending_cond.notify_all()
            return True

        assert entry is not None
        try:
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

                if _is_ambiguous_error_text(err_text):
                    try:
                        self._journal.mark_unknown(
                            self._actor,
                            self._target,
                            entry.action_id,
                            attempt=entry.attempt,
                            error=err_text,
                        )
                    except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                        self._log_err(f"failed to mark action {entry.action_id} unknown: {exc}")
                    self._send_outcome_unknown(
                        has_client_id=entry.has_client_id,
                        client_id=entry.client_id,
                        action_id=entry.action_id,
                        marker=entry.marker,
                        tool_name=entry.tool_name,
                        status=ActionStatus.UNKNOWN,
                        detail=err_text or "ambiguous upstream error",
                    )
                    return True

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
            elif "result" not in msg or not isinstance(msg["result"], dict):
                # Missing or non-object result in tools/call response -> outcome is ambiguous!
                detail = "upstream MCP response missing valid 'result' object"
                try:
                    self._journal.mark_unknown(
                        self._actor,
                        self._target,
                        entry.action_id,
                        attempt=entry.attempt,
                        error=detail,
                    )
                except (StaleAttemptError, InvalidStateTransitionError, JournalError) as exc:
                    self._log_err(
                        f"failed to mark malformed reply {entry.action_id} unknown: {exc}"
                    )
                self._send_outcome_unknown(
                    has_client_id=entry.has_client_id,
                    client_id=entry.client_id,
                    action_id=entry.action_id,
                    marker=entry.marker,
                    tool_name=entry.tool_name,
                    status=ActionStatus.UNKNOWN,
                    detail=detail,
                )
                return True
            else:
                result = msg["result"]
                is_tool_error = result.get("isError") is True
                if is_tool_error:
                    err_text = _extract_tool_error_text(result)
                    if _is_ambiguous_error_text(err_text):
                        try:
                            self._journal.mark_unknown(
                                self._actor,
                                self._target,
                                entry.action_id,
                                attempt=entry.attempt,
                                error=err_text,
                            )
                        except (
                            StaleAttemptError,
                            InvalidStateTransitionError,
                            JournalError,
                        ) as exc:
                            self._log_err(f"failed to mark action {entry.action_id} unknown: {exc}")
                        self._send_outcome_unknown(
                            has_client_id=entry.has_client_id,
                            client_id=entry.client_id,
                            action_id=entry.action_id,
                            marker=entry.marker,
                            tool_name=entry.tool_name,
                            status=ActionStatus.UNKNOWN,
                            detail=err_text,
                        )
                        return True
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
                    self._cache_confirmed_result(
                        (self._actor, self._target, entry.action_id), result
                    )

            if entry.has_client_id:
                outbound = dict(msg)
                outbound["id"] = entry.client_id
                self._send_client_message(outbound)
            return True
        finally:
            with self._pending_cond:
                self._active_finalizers -= 1
                self._pending_cond.notify_all()

    def _is_internal_upstream_id(self, msg_id: Any) -> bool:
        return isinstance(msg_id, str) and msg_id.startswith(f"__sidefx_{self._proxy_nonce}_")

    def _dispatch_internal_waiter(self, msg_id: Any, msg: dict[str, Any], generation: int) -> bool:
        if not isinstance(msg_id, str):
            return False
        with self._state_lock:
            if generation != self._upstream_generation:
                return False
            waiter = self._internal_waiters.pop(msg_id, None)
        if waiter is None:
            return False
        event, box = waiter
        box.append(msg)
        event.set()
        return True

    def _maybe_inject_init_instructions(self, msg_id: Any, msg: dict[str, Any]) -> dict[str, Any]:
        key = _rpc_id_key(msg_id)
        with self._state_lock:
            if key not in self._pending_init_ids:
                return msg
            self._pending_init_ids.discard(key)

        if not self._inject_instructions:
            return msg
        result = msg.get("result")
        if not isinstance(result, dict):
            return msg

        existing = result.get("instructions")
        if existing is not None and not isinstance(existing, str):
            return msg

        guidance = mcp_initialize_instructions(deterministic_ids=self._deterministic_ids)
        updated_result = dict(result)
        if isinstance(existing, str) and existing.strip():
            updated_result["instructions"] = f"{existing.rstrip()}\n\n{guidance}"
        else:
            updated_result["instructions"] = guidance
        updated_msg = dict(msg)
        updated_msg["result"] = updated_result
        return updated_msg

    def _fail_orphaned_passthrough(self, orphaned_ids: list[Any], *, reason: str) -> None:
        for cid in orphaned_ids:
            self._send_client_message(
                _jsonrpc_error_response(
                    cid,
                    ERR_UPSTREAM_UNAVAILABLE,
                    f"Upstream MCP server unavailable: {reason}",
                    data={"kind": "upstream_unavailable"},
                )
            )

    def _fail_all_inflight_on_upstream_eof(self, generation: int) -> None:
        with self._pending_cond:
            is_current = generation == self._upstream_generation
            if is_current:
                self._upstream_closed = True
                for event, _box in self._internal_waiters.values():
                    event.set()
                self._internal_waiters.clear()
                self._pending_init_ids.clear()
            orphaned: list[_InFlightMutation] = []
            for uid, entry in list(self._pending_mutations.items()):
                if entry.upstream_gen == generation and not entry.settled:
                    entry.settled = True
                    orphaned.append(entry)
                    self._pending_mutations.pop(uid, None)
            orphaned_passthrough: list[Any] = []
            for pkey, (cid, pgen) in list(self._pending_passthrough.items()):
                if pgen == generation:
                    orphaned_passthrough.append(cid)
                    self._pending_passthrough.pop(pkey, None)
            if is_current:
                self._timed_out_upstream_ids.clear()
            self._active_finalizers += 1

        try:
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
                    self._log_err(
                        f"failed to mark orphaned action {entry.action_id} unknown: {exc}"
                    )
                self._send_outcome_unknown(
                    has_client_id=entry.has_client_id,
                    client_id=entry.client_id,
                    action_id=entry.action_id,
                    marker=entry.marker,
                    tool_name=entry.tool_name,
                    status=ActionStatus.UNKNOWN,
                    detail=detail,
                )
            if orphaned_passthrough:
                self._fail_orphaned_passthrough(orphaned_passthrough, reason=detail)
        finally:
            with self._pending_cond:
                self._active_finalizers -= 1
                self._pending_cond.notify_all()

    def _upstream_reader_loop(self, upstream_stdout: BinaryIO, generation: int) -> None:
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
                    msg = _loads_strict_json(decoded)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    self._log_err(f"ignored non-JSON line from upstream stdout: {exc}")
                    continue

                with self._state_lock:
                    if generation != self._upstream_generation:
                        continue

                try:
                    if isinstance(msg, dict) and "method" not in msg and "id" in msg:
                        msg_id = msg["id"]
                        if self._dispatch_internal_waiter(msg_id, msg, generation):
                            continue
                        if isinstance(msg_id, str) and self._handle_upstream_response_for_mutation(
                            msg_id, msg
                        ):
                            continue
                        if self._is_internal_upstream_id(msg_id):
                            # Never leak unmatched proxy-internal JSON-RPC IDs to the client.
                            self._log_err(
                                f"dropped unmatched internal upstream response id={msg_id!r}"
                            )
                            continue
                        with self._state_lock:
                            if generation != self._upstream_generation:
                                continue
                            self._pending_passthrough.pop(_rpc_id_key(msg_id), None)
                        msg = self._maybe_inject_init_instructions(msg_id, msg)

                    # Passthrough response, notification, or server-initiated request.
                    if isinstance(msg, dict):
                        with self._state_lock:
                            if generation != self._upstream_generation:
                                continue
                        self._send_client_message(msg)
                    else:
                        self._log_err("ignored non-object JSON message from upstream stdout")
                except Exception as exc:
                    self._log_err(f"error processing upstream message: {exc}")
        except Exception as exc:
            with self._state_lock:
                is_current = generation == self._upstream_generation
                if is_current:
                    self._reader_fatal_error = True
                proc = self._proc if is_current else None
            if is_current:
                self._log_err(f"fatal error in upstream reader thread: {exc}")
                if proc is not None and proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.terminate()
        finally:
            self._fail_all_inflight_on_upstream_eof(generation)

    def _stderr_pump_loop(self, upstream_stderr: BinaryIO) -> None:
        write_failed = False
        try:
            while True:
                chunk = upstream_stderr.read(4096)
                if not chunk:
                    break
                if self._err_stream is not None and not write_failed:
                    try:
                        self._err_stream.write(chunk)
                        self._err_stream.flush()
                    except OSError:
                        # Keep draining upstream stderr so the child pipe never deadlocks.
                        write_failed = True
        except OSError:
            pass

    def _send_replay_for_record(
        self,
        *,
        has_client_id: bool,
        client_id: Any,
        record: ActionRecord,
        explicit_result: dict[str, Any] | None = None,
        announce: bool = False,
    ) -> None:
        """Answer a duplicate call from the journal instead of dispatching it upstream.

        ``announce`` appends a visible notice. It is set when sidefx derived the action ID
        itself: the caller never asked for deduplication, so a silently repeated result
        would read as a second successful write.
        """
        if not has_client_id:
            return
        if explicit_result is not None:
            self._send_client_message(_jsonrpc_result_response(client_id, explicit_result))
            return
        cached_bytes = self._get_cached_result_bytes(
            (record.actor, record.target, record.action_id)
        )
        if announce:
            self._send_client_message(
                _jsonrpc_result_response(
                    client_id, _result_with_replay_notice(record, cached_bytes)
                )
            )
            return
        if cached_bytes is not None:
            self._write_client_bytes(_build_cached_response_frame(client_id, cached_bytes))
            return
        self._send_client_message(_jsonrpc_result_response(client_id, _build_replay_result(record)))

    def _is_action_active_on_upstream(self, action_id: str) -> tuple[bool, bool]:
        """Return ``(in_pending, in_timed_out_while_alive)`` for ``action_id``."""
        with self._state_lock:
            in_pending = any(
                not entry.settled and entry.action_id == action_id
                for entry in self._pending_mutations.values()
            )
            in_timed_out = (not self._upstream_closed) and any(
                entry.action_id == action_id for entry in self._timed_out_upstream_ids.values()
            )
            return in_pending, in_timed_out

    def _try_reconcile_unknown(
        self,
        record: ActionRecord,
        *,
        has_client_id: bool,
        client_id: Any,
    ) -> str:
        """Attempt recovery via ``reconciler_hook``.

        Returns:
          - ``"handled"`` if a response was already sent to the client.
          - ``"retry"`` if reconciler proved ``not_found`` and it is safe to re-dispatch.
          - ``"block"`` if retry must be blocked with ``OUTCOME UNKNOWN``.
        """
        in_pending, in_timed_out_alive = self._is_action_active_on_upstream(record.action_id)
        if self._reconciler_hook is None or in_pending:
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
                self._cache_confirmed_result(
                    (record.actor, record.target, record.action_id), hook_res.cached_result
                )
            self._send_replay_for_record(
                has_client_id=has_client_id,
                client_id=client_id,
                record=reconciled,
                explicit_result=hook_res.cached_result,
            )
            return "handled"

        # ReconcileState.NOT_FOUND: NEVER re-dispatch while the earlier timed-out attempt
        # may still be executing inside a live upstream process, nor on a `pending` row.
        if in_timed_out_alive or record.status != ActionStatus.UNKNOWN:
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

    def _translate_cancelled_notification(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Rewrite `notifications/cancelled` requestId to the upstream ID when intercepted."""
        params = msg.get("params")
        if not isinstance(params, dict) or "requestId" not in params:
            return msg
        target_key = _rpc_id_key(params["requestId"])
        mapped_upstream_id: str | None = None
        with self._state_lock:
            self._pending_passthrough.pop(target_key, None)
            for entry in self._pending_mutations.values():
                if entry.has_client_id and _rpc_id_key(entry.client_id) == target_key:
                    mapped_upstream_id = entry.upstream_id
                    break
            if mapped_upstream_id is None:
                for timed_out in self._timed_out_upstream_ids.values():
                    if timed_out.has_client_id and _rpc_id_key(timed_out.client_id) == target_key:
                        mapped_upstream_id = timed_out.upstream_id
                        break
        if mapped_upstream_id is None:
            return msg
        rewritten = dict(msg)
        rewritten_params = dict(params)
        rewritten_params["requestId"] = mapped_upstream_id
        rewritten["params"] = rewritten_params
        return rewritten

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

        # Strip private `_sidefx_*` keys for preflight evaluation & upstream forwarding.
        forwarded_args = {k: v for k, v in tool_args.items() if not k.startswith("_sidefx_")}

        # Always evaluate deterministic preflight rules for ALL tools (reads, mutations, denied)
        # so proxy enforcement stays 100% consistent with `evaluate_preflight(...)`.
        preflight_res = evaluate_preflight(tool_name, forwarded_args, self._config)
        if not preflight_res.allowed:
            if has_client_id:
                summary = "; ".join(preflight_res.reasons)
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        ERR_PREFLIGHT_DENIED,
                        preflight_denied_message(tool_name, summary),
                        data={
                            "kind": "preflight_denied",
                            "tool": tool_name,
                            "tool_kind": preflight_res.tool_kind.value,
                            "violations": [v.to_dict() for v in preflight_res.violations],
                        },
                    )
                )
            return

        # Read-only calls pass straight through without journaling after preflight passes
        # (stripping any private `_sidefx_*` keys so strict upstream schemas accept them).
        if preflight_res.tool_kind == ToolKind.READ:
            if len(forwarded_args) != len(tool_args):
                read_msg = dict(msg)
                read_params = dict(params)
                read_params["arguments"] = forwarded_args
                read_msg["params"] = read_params
                self._forward_passthrough(
                    read_msg, has_client_id=has_client_id, client_id=client_id
                )
            else:
                self._forward_passthrough(msg, has_client_id=has_client_id, client_id=client_id)
            return

        # Extract explicit action ID from `_sidefx_action_id` or `idempotency_key`.
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
        elif "idempotency_key" in tool_args and tool_args["idempotency_key"] is not None:
            raw_ik = tool_args["idempotency_key"]
            if not isinstance(raw_ik, str) or not raw_ik.strip() or "\x00" in raw_ik:
                if has_client_id:
                    self._send_client_message(
                        _jsonrpc_error_response(
                            client_id,
                            JSONRPC_INVALID_PARAMS,
                            "Invalid params: 'idempotency_key' must be a non-empty string",
                        )
                    )
                return
            explicit_action_id = raw_ik.strip()

        # Compute canonical payload hash AND pre-serialize outbound frame BEFORE `begin_action`
        # so any serialization or nesting error fails cleanly without creating a pending row.
        canonical_payload = {"tool": tool_name, "arguments": forwarded_args}
        try:
            payload_hash = compute_payload_hash(canonical_payload)
        except (TypeError, ValueError, RecursionError) as exc:
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Invalid tool arguments for canonical hashing: {exc}",
                    )
                )
            return

        upstream_id = self._next_upstream_id()
        forwarded_params = dict(params)
        if raw_arguments is not _MISSING or forwarded_args:
            forwarded_params["arguments"] = forwarded_args
        forwarded_msg = dict(msg)
        forwarded_msg["id"] = upstream_id
        forwarded_msg["params"] = forwarded_params
        try:
            forwarded_bytes = _serialize_message(forwarded_msg)
        except (TypeError, ValueError, RecursionError) as exc:
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Failed to serialize tools/call request: {exc}",
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
            auto_derived_id=explicit_action_id is None,
        )
        if begin_res is None:
            return

        record = begin_res.record

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
                entry.upstream_gen = self._upstream_generation
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
        sent = self._write_upstream_bytes(forwarded_bytes)
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
        auto_derived_id: bool = False,
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
                            payload_mismatch_message(
                                exc, id_source="`_sidefx_action_id` (or `idempotency_key`)"
                            ),
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
                    announce=auto_derived_id,
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
        try:
            serialized = _serialize_message(msg)
        except (TypeError, ValueError, RecursionError) as exc:
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Failed to serialize JSON-RPC message: {exc}",
                    )
                )
            return

        tracked_key: tuple[type, Any] | None = None
        with self._state_lock:
            dead = self._upstream_closed
            if not dead and has_client_id and isinstance(msg.get("method"), str):
                tracked_key = _rpc_id_key(client_id)
                self._pending_passthrough[tracked_key] = (client_id, self._upstream_generation)
        if dead or not self._write_upstream_bytes(serialized):
            if tracked_key is not None:
                with self._state_lock:
                    self._pending_passthrough.pop(tracked_key, None)
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        ERR_UPSTREAM_UNAVAILABLE,
                        "Upstream MCP server is unavailable",
                        data={"kind": "upstream_unavailable"},
                    )
                )

    def _spawn_upstream(self) -> bool:
        """Spawn the upstream MCP subprocess in its own process group and start reader threads."""
        stderr_pipe = subprocess.PIPE if self._err_stream is not None else None
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": stderr_pipe,
            "env": self._upstream_env,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(self._upstream_cmd, **popen_kwargs)
        except OSError as exc:
            self._log_err(f"failed to launch upstream command {self._upstream_cmd[0]!r}: {exc}")
            with self._pending_cond:
                self._upstream_closed = True
                self._proc = None
                self._proc_pgid = None
                self._pending_cond.notify_all()
            return False

        assert proc.stdin is not None
        assert proc.stdout is not None

        pgid: int | None = None
        if os.name == "posix":
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = proc.pid

        with self._pending_cond:
            self._upstream_generation += 1
            gen = self._upstream_generation
            self._proc = proc
            self._proc_pgid = pgid
            self._upstream_closed = False

        reader_thread = threading.Thread(
            target=self._upstream_reader_loop,
            args=(proc.stdout, gen),
            name=f"sidefx-mcp-upstream-reader-{gen}",
            daemon=True,
        )
        reader_thread.start()

        stderr_thread: threading.Thread | None = None
        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=self._stderr_pump_loop,
                args=(proc.stderr,),
                name=f"sidefx-mcp-upstream-stderr-{gen}",
                daemon=True,
            )
            stderr_thread.start()

        with self._state_lock:
            self._reader_thread = reader_thread
            self._stderr_thread = stderr_thread
        return True

    def _send_internal_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        """Send an internal JSON-RPC request to upstream and wait for its reply."""
        internal_id = self._next_upstream_id()
        event = threading.Event()
        box: list[dict[str, Any]] = []
        with self._state_lock:
            if self._upstream_closed:
                return None
            self._internal_waiters[internal_id] = (event, box)

        req: dict[str, Any] = {"jsonrpc": "2.0", "id": internal_id, "method": method}
        if params is not None:
            req["params"] = params
        if not self._write_upstream_bytes(_serialize_message(req)):
            with self._state_lock:
                self._internal_waiters.pop(internal_id, None)
            return None

        event.wait(timeout=timeout)
        with self._state_lock:
            self._internal_waiters.pop(internal_id, None)
        return box[0] if box else None

    def _restart_upstream(self) -> None:
        """Terminate a wedged upstream process tree, spawn a fresh one, and replay handshake."""
        with self._pending_cond:
            old_proc = self._proc
            old_pgid = self._proc_pgid
            old_gen = self._upstream_generation
            old_reader = self._reader_thread
            old_stderr = self._stderr_thread
            old_timed_out_keys = list(self._timed_out_upstream_ids.keys())
            # Bump generation BEFORE killing old_proc so its reader's EOF handler ignores it.
            self._upstream_generation += 1
            self._proc = None
            self._proc_pgid = None
            self._pending_init_ids.clear()
            for event, _box in self._internal_waiters.values():
                event.set()
            self._internal_waiters.clear()
            orphaned: list[_InFlightMutation] = []
            for uid, entry in list(self._pending_mutations.items()):
                if entry.upstream_gen == old_gen and not entry.settled:
                    entry.settled = True
                    orphaned.append(entry)
                    self._pending_mutations.pop(uid, None)
            orphaned_passthrough: list[Any] = []
            for pkey, (cid, pgen) in list(self._pending_passthrough.items()):
                if pgen == old_gen:
                    orphaned_passthrough.append(cid)
                    self._pending_passthrough.pop(pkey, None)
            self._active_finalizers += 1

        try:
            detail = "upstream MCP server was restarted after becoming unresponsive"
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
                    self._log_err(
                        f"failed to mark restarted orphaned action {entry.action_id} unknown: {exc}"
                    )
                self._send_outcome_unknown(
                    has_client_id=entry.has_client_id,
                    client_id=entry.client_id,
                    action_id=entry.action_id,
                    marker=entry.marker,
                    tool_name=entry.tool_name,
                    status=ActionStatus.UNKNOWN,
                    detail=detail,
                )
            if orphaned_passthrough:
                self._fail_orphaned_passthrough(orphaned_passthrough, reason=detail)
        finally:
            with self._pending_cond:
                self._active_finalizers -= 1
                self._pending_cond.notify_all()

        tree_dead = True
        if old_proc is not None:
            with self._upstream_write_lock:
                if old_proc.stdin is not None:
                    with contextlib.suppress(OSError):
                        old_proc.stdin.close()
            tree_dead = _kill_upstream_tree(old_proc, old_pgid)
            if old_reader is not None:
                old_reader.join(timeout=0.5)
            if old_stderr is not None:
                old_stderr.join(timeout=0.5)
            # Never close a pipe while a reader thread is still blocked on readline/read.
            if old_proc.stdout is not None and (old_reader is None or not old_reader.is_alive()):
                with contextlib.suppress(OSError):
                    old_proc.stdout.close()
            if old_proc.stderr is not None and (old_stderr is None or not old_stderr.is_alive()):
                with contextlib.suppress(OSError):
                    old_proc.stderr.close()

        # Release the in-flight timed-out duplicate guard ONLY once the old tree is confirmed dead.
        if tree_dead:
            with self._state_lock:
                for uid in old_timed_out_keys:
                    self._timed_out_upstream_ids.pop(uid, None)
        else:
            self._log_err(
                "could not confirm previous upstream process tree exited; keeping duplicate guard"
            )

        if not self._spawn_upstream():
            return

        # Transparently replay the MCP initialize + notifications/initialized handshake.
        if self._saw_initialize:
            reinit_timeout = max(5.0, min(30.0, self._timeout_seconds))
            reinit_resp = self._send_internal_request(
                "initialize", self._cached_init_params, timeout=reinit_timeout
            )
            if reinit_resp is None or (
                isinstance(reinit_resp, dict) and reinit_resp.get("error") is not None
            ):
                self._log_err(
                    "restarted upstream MCP server failed initialize handshake; shutting it down"
                )
                with self._pending_cond:
                    failed_proc = self._proc
                    failed_pgid = self._proc_pgid
                    failed_reader = self._reader_thread
                    failed_stderr = self._stderr_thread
                    self._upstream_generation += 1
                    self._upstream_closed = True
                    self._proc = None
                    self._proc_pgid = None
                    self._pending_cond.notify_all()
                if failed_proc is not None:
                    with self._upstream_write_lock:
                        if failed_proc.stdin is not None:
                            with contextlib.suppress(OSError):
                                failed_proc.stdin.close()
                    _kill_upstream_tree(failed_proc, failed_pgid)
                    if failed_reader is not None:
                        failed_reader.join(timeout=0.5)
                    if failed_stderr is not None:
                        failed_stderr.join(timeout=0.5)
                    if failed_proc.stdout is not None and (
                        failed_reader is None or not failed_reader.is_alive()
                    ):
                        with contextlib.suppress(OSError):
                            failed_proc.stdout.close()
                    if failed_proc.stderr is not None and (
                        failed_stderr is None or not failed_stderr.is_alive()
                    ):
                        with contextlib.suppress(OSError):
                            failed_proc.stderr.close()
                return
            if self._saw_initialized_notif:
                notif = self._cached_initialized_msg or {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
                self._write_upstream_bytes(_serialize_message(notif))

    def _ensure_upstream_healthy(self) -> None:
        """Probe upstream after a timeout and restart it if it is wedged."""
        if not self._restart_on_timeout:
            return
        with self._state_lock:
            if not self._needs_health_check or self._upstream_closed:
                return
            if not self._timed_out_upstream_ids:
                # Late reply already arrived, proving upstream is responsive.
                self._needs_health_check = False
                return

        probe_timeout = min(1.0, max(0.1, self._timeout_seconds))
        probe_resp = self._send_internal_request("ping", {}, timeout=probe_timeout)
        if probe_resp is not None:
            with self._state_lock:
                self._needs_health_check = False
            return

        self._log_err(
            "upstream MCP server is unresponsive after timeout; restarting to prevent poisoning "
            "subsequent calls"
        )
        self._restart_upstream()
        with self._state_lock:
            self._needs_health_check = False

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
            msg = _loads_strict_json(decoded)
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

        try:
            if method != "notifications/cancelled":
                self._ensure_upstream_healthy()

            if method == "tools/call":
                self._handle_tools_call(msg, has_client_id=has_client_id, client_id=client_id)
                return

            if method == "initialize":
                raw_params = msg.get("params")
                self._saw_initialize = True
                self._cached_init_params = dict(raw_params) if isinstance(raw_params, dict) else {}
                if has_client_id:
                    with self._state_lock:
                        self._pending_init_ids.add(_rpc_id_key(client_id))
            elif method == "notifications/initialized":
                self._saw_initialized_notif = True
                self._cached_initialized_msg = dict(msg)
            elif method == "notifications/cancelled":
                msg = self._translate_cancelled_notification(msg)

            # Pass through protocol methods, notifications, and client replies to server requests.
            is_request_or_notification = isinstance(method, str) and bool(method)
            is_response = (
                "method" not in msg and has_client_id and ("result" in msg or "error" in msg)
            )
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
        except Exception as exc:
            self._log_err(f"internal error handling client message: {exc}")
            if has_client_id:
                self._send_client_message(
                    _jsonrpc_error_response(
                        client_id,
                        JSONRPC_INTERNAL_ERROR,
                        f"Internal proxy error: {exc}",
                    )
                )

    def run(self) -> int:
        """Spawn the upstream MCP server and proxy stdio JSON-RPC messages until EOF."""
        if not self._spawn_upstream():
            return 1

        try:
            while True:
                raw_line, overflowed = self._read_framed_line(self._client_in)
                if raw_line is None:
                    break
                self._handle_client_line(raw_line, overflowed)
        finally:
            self._shutdown()

        with self._state_lock:
            return 1 if self._reader_fatal_error else 0

    def _shutdown(self) -> None:
        # Wait for in-flight intercepted mutations AND active timer/response finalizers
        # to finish writing SQLite and client responses before closing upstream stdin.
        with self._pending_cond:
            while (
                self._pending_mutations and not self._upstream_closed
            ) or self._active_finalizers > 0:
                self._pending_cond.wait(timeout=0.5)
            proc = self._proc
            pgid = self._proc_pgid
            reader_thread = self._reader_thread
            stderr_thread = self._stderr_thread

        if proc is not None:
            # Signal clean EOF to upstream stdin.
            with self._upstream_write_lock:
                if proc.stdin is not None:
                    with contextlib.suppress(OSError):
                        proc.stdin.close()

            # Allow upstream to finish sending any queued passthrough responses and exit cleanly.
            if reader_thread is not None:
                reader_thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)

            if proc.poll() is None:
                try:
                    proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _kill_upstream_tree(proc, pgid)

            if reader_thread is not None:
                reader_thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)
            if stderr_thread is not None:
                stderr_thread.join(timeout=1.0)

        # Cancel any leftover timers and wait for any racing finalizer before returning.
        with self._pending_cond:
            for entry in self._pending_mutations.values():
                if entry.timer is not None:
                    entry.timer.cancel()
            self._pending_mutations.clear()
            while self._active_finalizers > 0:
                self._pending_cond.wait(timeout=0.5)

        if proc is not None:
            if proc.stdout is not None and (reader_thread is None or not reader_thread.is_alive()):
                with contextlib.suppress(OSError):
                    proc.stdout.close()
            if proc.stderr is not None and (stderr_thread is None or not stderr_thread.is_alive()):
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
    parser.add_argument(
        "--no-inject-instructions",
        dest="inject_instructions",
        action="store_false",
        default=True,
        help=(
            "Pass through upstream MCP initialize result.instructions verbatim without "
            "appending sidefx protocol guidance."
        ),
    )
    parser.add_argument(
        "--restart-on-timeout",
        dest="restart_on_timeout",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Restart a wedged upstream MCP server after a call times out and replay the "
            "initialize handshake transparently (opt-in; default: disabled so slow-but-healthy "
            "serial servers can finish and reconcile late replies)."
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
            inject_instructions=parsed.inject_instructions,
            restart_on_timeout=parsed.restart_on_timeout,
        )
        return proxy.run()
