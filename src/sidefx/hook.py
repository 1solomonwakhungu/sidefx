"""Claude Code hook handlers (``sidefx hook claude-code``)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, TextIO

from sidefx.config import ConfigError, SidefxConfig, load_config
from sidefx.journal import ActionStatus, JournalError, resolve_db_path
from sidefx.receipt import ReceiptData, load_receipt_data, render_receipt_markdown
from sidefx.reconcilers.base import sanitize_text
from sidefx.session import resolve_session_id

_MAX_HOOK_STDIN_BYTES = 1024 * 1024  # 1 MiB safety cap on hook stdin payload


def _parse_hook_stdin(stream: TextIO) -> tuple[dict[str, Any] | None, bool]:
    """Read and parse Claude Code Stop hook JSON from ``stream``.

    Returns ``(payload_dict_or_none, ok)``:
      - Empty input -> ``({}, True)``
      - Valid JSON object -> ``(dict, True)``
      - Malformed JSON, non-object JSON, oversized input, or I/O error -> ``(None, False)``
    """
    try:
        raw = stream.read(_MAX_HOOK_STDIN_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None, False

    if len(raw) > _MAX_HOOK_STDIN_BYTES:
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


def format_claude_code_stop_message(data: ReceiptData) -> str:
    """Format the ``systemMessage`` text for a Claude Code ``Stop`` hook.

    Highlights any ``pending`` or ``unknown`` actions loudly at the top so the user
    and agent immediately notice unresolved side effects.
    """
    receipt_md = render_receipt_markdown(data)
    unresolved = data.unknown
    if not unresolved:
        return receipt_md

    pending_count = data.counts[ActionStatus.PENDING]
    unknown_count = data.counts[ActionStatus.UNKNOWN]
    ids = ", ".join(f"`{sanitize_text(rec.action_id)}`" for rec in unresolved[:8])
    if len(unresolved) > 8:
        ids += f", ... (+{len(unresolved) - 8} more)"

    warning_lines = [
        f"WARNING: {len(unresolved)} UNRESOLVED sidefx side effect(s) "
        f"({pending_count} pending, {unknown_count} unknown) — outcome uncertain!",
        f"Unresolved action(s): {ids}",
        "Do NOT retry blindly. Run `sidefx reconcile` (or "
        "`sidefx resolve --action-id <id> --landed|--not-landed`) before retrying.",
        "",
        receipt_md,
    ]
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

    Always fails open cleanly: malformed stdin, missing DB, unreadable DB, broken config,
    or zero recorded side effects all return ``None`` (stay silent, never block Stop).
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

    explicit_session = session_id_override or payload_session_clean
    session_id, _ = resolve_session_id(
        explicit=explicit_session,
        config=effective_config,
        db_path=db_path,
        create=False,
    )
    if not session_id:
        return None

    try:
        data = load_receipt_data(db_path, session_id=session_id)
    except (JournalError, sqlite3.Error, ValueError, OSError):
        return None

    if data.total == 0:
        return None

    return {"systemMessage": format_claude_code_stop_message(data)}


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
    """CLI handler for ``sidefx hook <harness>``."""
    del config  # Loaded relative to hook payload `cwd` or explicit `--config`.
    parser = _build_hook_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))
    if not sub_args.hook_target:
        parser.print_help()
        return 0

    if sub_args.hook_target == "claude-code":
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
        return 0

    parser.print_help()
    return 0
