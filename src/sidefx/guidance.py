"""Agent-facing next-step guidance attached to sidefx errors.

Every blocking error an agent can receive from ``sidefx mcp-proxy`` or ``sidefx wrap``
must answer "what do I do now?" in the error text itself, because most MCP clients show
the model only the JSON-RPC ``message`` and a shell shows only stderr. This module is the
single source for that wording so the proxy, the CLI wrapper, and the docs stay in step.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from sidefx.journal import PayloadMismatchError

OUTCOME_UNKNOWN_BANNER = "OUTCOME UNKNOWN — reconcile before retrying"

_HASH_PREVIEW_LENGTH = 12
_MAX_DETAIL_CHARS = 300
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _sanitize_detail(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text)
    cleaned = _UNSAFE_CHARS_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_DETAIL_CHARS:
        cleaned = f"{cleaned[: _MAX_DETAIL_CHARS - 1]}…"
    return cleaned


def _db_flag(db_path: str | Path | None) -> str:
    return f" --db {shlex.quote(str(db_path))}" if db_path is not None else ""


def reconcile_command(action_id: str, *, db_path: str | Path | None = None) -> str:
    """Shell command that reconciles one action against its external system."""
    return f"sidefx reconcile{_db_flag(db_path)} --action-id {shlex.quote(action_id)}"


def resolve_command(
    action_id: str,
    *,
    landed: bool,
    db_path: str | Path | None = None,
) -> str:
    """Shell command that records a manually verified outcome for one action."""
    verdict = "--landed" if landed else "--not-landed"
    return f"sidefx resolve{_db_flag(db_path)} --action-id {shlex.quote(action_id)} {verdict}"


def outcome_unknown_next_steps(
    action_id: str,
    *,
    db_path: str | Path | None = None,
    retry_noun: str = "call",
) -> list[str]:
    """Ordered steps that get an agent out of an ``OUTCOME UNKNOWN`` block."""
    return [
        f"Do not re-send or reword this {retry_noun}: the earlier attempt may have landed.",
        f"Run `{reconcile_command(action_id, db_path=db_path)}` "
        "(verifies git push and GitHub issue/PR/comment writes read-only).",
        "If it reports no reconciler, check for the write yourself with a read-only "
        f"{retry_noun}, then run `{resolve_command(action_id, landed=True, db_path=db_path)}` "
        "(add --ref <url-or-id>), or the same command with `--not-landed` if it is absent.",
        f"Repeat the original {retry_noun} unchanged: it returns the recorded result, "
        "or runs again if you recorded --not-landed.",
    ]


def outcome_unknown_data(
    action_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Structured recovery fields for the JSON-RPC ``error.data`` object."""
    return {
        "journal": str(db_path) if db_path is not None else None,
        "reconcile_command": reconcile_command(action_id, db_path=db_path),
        "resolve_commands": {
            "landed": resolve_command(action_id, landed=True, db_path=db_path),
            "not_landed": resolve_command(action_id, landed=False, db_path=db_path),
        },
        "next_steps": outcome_unknown_next_steps(action_id, db_path=db_path),
    }


def _join_sentence(prefix: str, sentence: str) -> str:
    return f"{prefix.rstrip().rstrip('.')}. {sentence}"


def outcome_unknown_message(
    *,
    action_id: str,
    marker: str,
    detail: str,
    tool_name: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Single-line ``OUTCOME UNKNOWN`` message that carries its own recovery steps."""
    tool_part = f", tool={tool_name}" if tool_name else ""
    safe_detail = _sanitize_detail(detail)
    head = (
        f"{OUTCOME_UNKNOWN_BANNER} (action_id={action_id}, marker={marker}{tool_part}): "
        f"{safe_detail}"
    )
    steps = outcome_unknown_next_steps(action_id, db_path=db_path)
    numbered = " ".join(f"{index}) {step}" for index, step in enumerate(steps, start=1))
    return _join_sentence(head, f"Next steps: {numbered}")


def outcome_unknown_cli_hint(
    action_id: str,
    *,
    db_path: str | Path | None = None,
    has_reconciler: bool,
) -> str:
    """Multi-line stderr hint printed under an ``OUTCOME UNKNOWN`` line from ``sidefx wrap``."""
    reconcile = reconcile_command(action_id, db_path=db_path)
    landed = resolve_command(action_id, landed=True, db_path=db_path)
    not_landed = resolve_command(action_id, landed=False, db_path=db_path)
    lines = ["sidefx: the write may have landed, so do not re-run it blindly. Next steps:"]
    if has_reconciler:
        lines.append(
            "  1. Re-run this exact command after a few seconds, or run "
            f"`{reconcile}`: sidefx checks the remote read-only and never writes twice."
        )
        lines.append(
            f"  2. If that stays unknown, verify by hand and record it: `{landed}` "
            f"(add --ref <url-or-id>) or `{not_landed}`."
        )
    else:
        lines.append(
            "  1. No reconciler covers this command. Check by hand whether the write landed."
        )
        lines.append(
            f"  2. Record what you found: `{landed}` (add --ref <url-or-id>) or `{not_landed}`."
        )
        lines.append(
            "  3. If it landed, stop (do not re-run). If you recorded --not-landed, "
            "re-run the exact original command."
        )
    return "\n".join(lines)


def payload_mismatch_message(
    exc: PayloadMismatchError,
    *,
    id_source: str,
    payload_noun: str = "payload",
) -> str:
    """Explain a reused action ID in terms of what the caller should send instead."""
    detail = f" ({exc.detail})" if exc.detail else ""
    expected = exc.expected_hash[:_HASH_PREVIEW_LENGTH]
    actual = exc.actual_hash[:_HASH_PREVIEW_LENGTH]
    return (
        f"Payload mismatch for action {exc.action_id!r} on target {exc.target!r}: this action ID "
        f"is already recorded for a different {payload_noun}{detail}. Nothing was sent. "
        f"For a new write, use a new {id_source}. To get the original result back, repeat the "
        f"original {payload_noun} unchanged. (recorded sha256 {expected}…, received {actual}…)"
    )


def preflight_denied_message(tool_name: str, summary: str) -> str:
    """Preflight denial text that tells the agent a retry cannot succeed."""
    return _join_sentence(
        f"Preflight check denied tool {tool_name!r}: {summary}",
        "This is a .sidefx.json policy rule, not a transient error: the call was not sent "
        "and an unchanged retry will be denied again. Change the argument to satisfy the "
        "rule, or ask the user to approve or adjust the policy.",
    )


def replay_notice(*, tool_name: str, action_id: str, status: str) -> str:
    """Text appended to a result that sidefx served from the journal instead of upstream."""
    return (
        f"[sidefx] Duplicate call not re-sent: an identical {tool_name} call already ran in "
        f"this session (action {action_id}, {status}), so this is the original result. "
        "To run it again on purpose, repeat the call with a unique `_sidefx_action_id` "
        "string argument."
    )


def mcp_initialize_instructions(*, deterministic_ids: bool = False) -> str:
    """Concise protocol guidance injected into MCP ``initialize`` ``result.instructions``."""
    dedupe_note = (
        "Deterministic session deduplication is ON: identical mutating calls are answered from "
        "the journal and include a `[sidefx] Duplicate call not re-sent` notice instead of "
        "writing twice. Pass a unique `_sidefx_action_id` string argument (stripped before "
        "reaching the server) to pin a retry key or to intentionally repeat an identical call."
        if deterministic_ids
        else "Pass an optional `_sidefx_action_id` string argument on mutating tool calls "
        "(stripped before reaching the server) for explicit idempotency across retries or "
        "intentional repeats. If a repeated call returns a `[sidefx] Duplicate call not re-sent` "
        "notice, the original result was replayed from the journal without writing again."
    )
    return (
        "[sidefx] Mutating tool calls on this MCP server are protected by sidefx's write-ahead "
        f"journal. {dedupe_note} "
        "On `OUTCOME UNKNOWN` (-32002), the write may have landed—do NOT re-send or reword the "
        "call; check remote state read-only and run `sidefx reconcile` (or "
        "`sidefx resolve --action-id <id> --landed` / `--not-landed`) before repeating the "
        "original call unchanged."
    )
