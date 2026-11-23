"""Claude Code hook handlers (``sidefx hook claude-code``)."""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, TextIO

from sidefx.config import ConfigError, SidefxConfig, load_config
from sidefx.journal import ActionRecord, ActionStatus, Journal, JournalError, resolve_db_path
from sidefx.receipt import ReceiptData, load_receipt_data, render_receipt_markdown
from sidefx.reconcilers.base import sanitize_text
from sidefx.session import (
    ENV_CLAUDE_CODE_SESSION_ID,
    ENV_SESSION_ID,
    resolve_session_id,
)

_MAX_HOOK_STDIN_BYTES = 1024 * 1024  # 1 MiB safety cap on hook stdin payload
_WRITER_AUTHORITATIVE_SOURCES = frozenset(
    {
        "cli",
        f"env:{ENV_SESSION_ID}",
        "config",
        f"env:{ENV_CLAUDE_CODE_SESSION_ID}",
    }
)


def _parse_hook_stdin(stream: TextIO | None) -> tuple[dict[str, Any] | None, bool]:
    """Read and parse Claude Code Stop hook JSON from ``stream``.

    Returns ``(payload_dict_or_none, ok)``:
      - Empty input -> ``({}, True)``
      - Valid JSON object -> ``(dict, True)``
      - Malformed JSON, non-object JSON, oversized input, closed stream, or I/O error
        -> ``(None, False)``
    """
    if stream is None:
        return None, False
    try:
        raw = stream.read(_MAX_HOOK_STDIN_BYTES + 1)
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return None, False

    if not isinstance(raw, str) or len(raw) > _MAX_HOOK_STDIN_BYTES:
        return None, False

    trimmed = raw.strip()
    if not trimmed:
        return {}, True

    try:
        parsed = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None, False

    if not isinstance(parsed, dict):
        return None, False
    return parsed, True


def _load_all_unresolved(db_path: Path) -> tuple[ActionRecord, ...]:
    """Load all ``pending`` and ``unknown`` actions across every session strictly read-only."""
    if not db_path.exists():
        return ()
    with Journal.open_readonly(db_path) as journal:
        return tuple(
            journal.list_actions(
                status=(ActionStatus.PENDING, ActionStatus.UNKNOWN),
                descending=False,
            )
        )


def format_claude_code_stop_message(
    data: ReceiptData | None,
    *,
    journal_unresolved: tuple[ActionRecord, ...] = (),
) -> str:
    """Format the ``systemMessage`` text for a Claude Code ``Stop`` hook.

    Highlights any ``pending`` or ``unknown`` actions loudly at the top — including
    unresolved actions recorded in other sessions (e.g. a long-lived MCP proxy whose
    session ID diverged after ``/clear`` or ``--continue``).
    """
    scoped_unresolved = data.unknown if data is not None else ()
    scoped_keys = {(r.actor, r.target, r.action_id) for r in scoped_unresolved}
    other_unresolved = tuple(
        r for r in journal_unresolved if (r.actor, r.target, r.action_id) not in scoped_keys
    )
    all_unresolved = scoped_unresolved + other_unresolved

    receipt_md = render_receipt_markdown(data) if data is not None and data.total > 0 else ""

    if not all_unresolved:
        return receipt_md

    pending_count = sum(1 for r in all_unresolved if r.status == ActionStatus.PENDING)
    unknown_count = sum(1 for r in all_unresolved if r.status == ActionStatus.UNKNOWN)
    ids = ", ".join(f"`{sanitize_text(rec.action_id)}`" for rec in all_unresolved[:8])
    if len(all_unresolved) > 8:
        ids += f", ... (+{len(all_unresolved) - 8} more)"

    warning_lines = [
        f"WARNING: {len(all_unresolved)} UNRESOLVED sidefx side effect(s) "
        f"({pending_count} pending, {unknown_count} unknown) — outcome uncertain!",
        f"Unresolved action(s): {ids}",
        "Do NOT retry blindly. Run `sidefx reconcile` (or "
        "`sidefx resolve --action-id <id> --landed|--not-landed`) before retrying.",
    ]
    if other_unresolved:
        warning_lines.append("")
        warning_lines.append(f"### Unresolved in Other Session(s) ({len(other_unresolved)})")
        for rec in other_unresolved:
            err_suffix = f" — error: `{sanitize_text(rec.error)}`" if rec.error else ""
            warning_lines.append(
                f"- `{sanitize_text(rec.action_id)}` · [{rec.status.value}] · "
                f"session `{sanitize_text(rec.session_id)}` · `{rec.surface.value}` · "
                f"`{sanitize_text(rec.tool_name)}` · target `{sanitize_text(rec.target)}`"
                f"{err_suffix} (marker: `{sanitize_text(rec.marker)}`)"
            )
    if receipt_md:
        warning_lines.append("")
        warning_lines.append(receipt_md)
    return "\n".join(warning_lines)


def run_claude_code_stop_hook(
    *,
    stdin: TextIO | None = None,
    db_path_override: str | Path | None = None,
    config_path_override: str | Path | None = None,
    session_id_override: str | None = None,
    cwd_override: Path | None = None,
) -> dict[str, str] | None:
    """Evaluate the Claude Code ``Stop`` hook and return hook JSON dict or ``None`` if silent.

    Always fails open cleanly: malformed stdin, closed stdin, missing DB, unreadable DB,
    broken config, or zero recorded side effects (and zero journal-wide unresolved actions)
    all return ``None`` (stay silent, never block Stop).
    """
    stream = stdin if stdin is not None else sys.stdin
    payload, ok = _parse_hook_stdin(stream)
    if not ok or payload is None:
        return None

    effective_cwd = cwd_override if cwd_override is not None else Path.cwd()
    raw_cwd = payload.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip() and "\x00" not in raw_cwd:
        candidate_cwd = Path(raw_cwd.strip()).expanduser()
        try:
            if candidate_cwd.is_dir():
                effective_cwd = candidate_cwd
        except OSError:
            pass

    try:
        effective_config: SidefxConfig = (
            load_config(config_path_override, cwd=effective_cwd)
            if config_path_override is not None
            else load_config(cwd=effective_cwd)
        )
    except (ConfigError, OSError):
        effective_config = SidefxConfig()

    try:
        db_path = resolve_db_path(
            db_path_override,
            config=effective_config,
            cwd=effective_cwd,
        )
    except (ValueError, OSError):
        return None

    if not db_path.exists():
        return None

    payload_session = payload.get("session_id")
    payload_session_clean = (
        payload_session.strip()
        if isinstance(payload_session, str)
        and payload_session.strip()
        and "\x00" not in payload_session
        else None
    )

    # Resolve session with writer-matching precedence so SIDEFX_SESSION_ID, config
    # default_session_id, and persisted current-session align with what writers recorded.
    writer_session, writer_source = resolve_session_id(
        explicit=session_id_override,
        config=effective_config,
        db_path=db_path,
        create=False,
    )

    try:
        journal_unresolved = _load_all_unresolved(db_path)
    except (JournalError, sqlite3.Error, ValueError, OSError):
        return None

    data: ReceiptData | None = None
    try:
        if writer_source in _WRITER_AUTHORITATIVE_SOURCES and writer_session is not None:
            data = load_receipt_data(db_path, session_id=writer_session)
        elif payload_session_clean is not None:
            payload_data = load_receipt_data(db_path, session_id=payload_session_clean)
            if payload_data.total > 0:
                data = payload_data
            elif writer_session is not None:
                data = load_receipt_data(db_path, session_id=writer_session)
            else:
                data = payload_data
        elif writer_session is not None:
            data = load_receipt_data(db_path, session_id=writer_session)
    except (JournalError, sqlite3.Error, ValueError, OSError):
        return None

    scoped_total = data.total if data is not None else 0
    if scoped_total == 0 and not journal_unresolved:
        return None

    return {
        "systemMessage": format_claude_code_stop_message(
            data,
            journal_unresolved=journal_unresolved,
        )
    }


def _build_hook_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx hook",
        description="Run agent harness lifecycle hooks (e.g. Claude Code Stop hook).",
    )
    subparsers = parser.add_subparsers(dest="hook_target", metavar="<harness>")

    cc = subparsers.add_parser(
        "claude-code",
        help=(
            "Handle a Claude Code Stop hook payload on stdin; stay silent when 0 side effects "
            "occurred, else emit structured hook JSON."
        ),
    )
    cc.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    cc.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    cc.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Override session ID instead of reading from Stop hook stdin payload.",
    )
    return parser


def handle_hook_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx hook <harness>`` (strictly fail-open: always exits 0)."""
    del config  # Loaded relative to hook payload `cwd` or explicit `--config`.
    parser = _build_hook_parser()
    raw_args = list(getattr(args, "args", []))
    try:
        # Suppress argparse stderr noise on unexpected hook flags so Stop never fails/blocks.
        err_sink = io.StringIO()
        with redirect_stderr(err_sink):
            sub_args = parser.parse_args(raw_args)
    except SystemExit:
        return 0
    except Exception:
        return 0

    if not sub_args.hook_target:
        parser.print_help()
        return 0

    if sub_args.hook_target == "claude-code":
        try:
            db_override = sub_args.db_path or getattr(args, "db_path", None)
            cfg_override = sub_args.config_path or getattr(args, "config_path", None)
            sess_override = sub_args.session_id or getattr(args, "session_id", None)
            result = run_claude_code_stop_hook(
                stdin=sys.stdin,
                db_path_override=db_override,
                config_path_override=cfg_override,
                session_id_override=sess_override,
            )
            if result is not None:
                print(json.dumps(result))
        except Exception:
            return 0
        return 0

    return 0
