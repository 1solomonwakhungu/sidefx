"""Command-line interface for sidefx."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sidefx import __version__
from sidefx.config import (
    CONFIG_FILENAME,
    ConfigError,
    SidefxConfig,
    default_config_dict,
    load_config,
)
from sidefx.journal import (
    ActionRecord,
    ActionStatus,
    Journal,
    JournalError,
    Surface,
    resolve_db_path,
)

DEFAULT_STATUS_LIMIT = 20
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def _sanitize_terminal_text(text: str) -> str:
    """Strip ANSI escape sequences and control characters so journal text cannot forge lines."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub(" ", stripped)


def _add_shared_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    default_val: Any = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--db",
        dest="db_path",
        default=default_val,
        help="Path to SQLite journal database (overrides SIDEFX_DB and config).",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=default_val,
        help=f"Path to {CONFIG_FILENAME} configuration file.",
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default=default_val,
        help="Session identifier filter or override.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for the `sidefx` CLI."""
    parser = argparse.ArgumentParser(
        prog="sidefx",
        description=(
            "Outbound effects journal, transparent stdio MCP proxy, CLI mutation wrapper, "
            "crash reconciler, and ground-truth session receipts."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    _add_shared_options(parser, suppress_defaults=False)

    shared_sub_parent = argparse.ArgumentParser(add_help=False)
    _add_shared_options(shared_sub_parent, suppress_defaults=True)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # status
    status_parser = subparsers.add_parser(
        "status",
        parents=[shared_sub_parent],
        help="Show action counts by status and list recent journaled actions.",
    )
    status_parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=[s.value for s in ActionStatus],
        default=None,
        help="Filter by action status (repeatable).",
    )
    status_parser.add_argument(
        "--surface",
        dest="surface",
        choices=[s.value for s in Surface],
        default=None,
        help="Filter actions by execution surface.",
    )
    status_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_STATUS_LIMIT,
        help=f"Maximum number of actions to list (default: {DEFAULT_STATUS_LIMIT}).",
    )
    status_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    status_parser.set_defaults(handler=_cmd_status, passthrough=False)

    # init
    init_parser = subparsers.add_parser(
        "init",
        parents=[shared_sub_parent],
        help=f"Create a starter {CONFIG_FILENAME} configuration file.",
    )
    init_parser.add_argument(
        "--path",
        dest="init_path",
        default=CONFIG_FILENAME,
        help=f"Destination path for config file (default: {CONFIG_FILENAME}).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file.",
    )
    init_parser.add_argument(
        "--claude-code",
        dest="claude_code",
        action="store_true",
        help="Output or merge a Claude Code Stop hook snippet for session receipts.",
    )
    init_parser.set_defaults(handler=_cmd_init, passthrough=False)

    # Passthrough subcommand dispatchers for PR 2 & PR 3.
    # Using `add_help=False` + `parse_known_args` lets downstream subcommand handlers
    # receive all subcommand flags (including `--help` and `-- <cmd ...>`) verbatim
    # without conflicting edits in `cli.py`.
    _register_passthrough_subcommand(
        subparsers,
        name="report",
        help_text="Render a ground-truth session side-effects receipt.",
        module_name="sidefx.receipt",
        func_name="handle_report_cli",
    )
    _register_passthrough_subcommand(
        subparsers,
        name="reconcile",
        help_text="Reconcile pending or unknown actions against external systems.",
        module_name="sidefx.reconcilers",
        func_name="handle_reconcile_cli",
    )
    _register_passthrough_subcommand(
        subparsers,
        name="wrap",
        help_text="Wrap a mutating CLI command with write-ahead journaling.",
        module_name="sidefx.wrapper",
        func_name="handle_wrap_cli",
    )
    _register_passthrough_subcommand(
        subparsers,
        name="mcp-proxy",
        help_text="Run transparent stdio JSON-RPC 2.0 MCP proxy in front of an MCP server.",
        module_name="sidefx.mcp_proxy",
        func_name="handle_mcp_proxy_cli",
    )

    return parser


def _register_passthrough_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    name: str,
    help_text: str,
    module_name: str,
    func_name: str,
) -> None:
    sub = subparsers.add_parser(name, add_help=False, help=help_text)
    sub.set_defaults(
        handler=lambda ns, cfg, m=module_name, f=func_name, lbl=name: _dispatch_optional(
            m, f, ns, cfg, lbl
        ),
        passthrough=True,
    )


def _format_status_text(
    db_path: Path,
    counts: dict[ActionStatus, int],
    actions: list[ActionRecord],
    *,
    journal_exists: bool,
) -> str:
    total = sum(counts.values())
    lines = [f"sidefx journal: {_sanitize_terminal_text(str(db_path))}"]
    if not journal_exists:
        lines.append("No journal database found (0 actions).")
        return "\n".join(lines)

    summary_parts = [f"{s.value}={counts[s]}" for s in ActionStatus]
    lines.append(f"Totals: {total} ({', '.join(summary_parts)})")

    if not actions:
        lines.append("No matching actions.")
        return "\n".join(lines)

    lines.append("")
    for rec in actions:
        detail = rec.result_ref or rec.error or rec.summary or ""
        safe_tool = _sanitize_terminal_text(rec.tool_name)
        safe_target = _sanitize_terminal_text(rec.target)
        safe_id = _sanitize_terminal_text(rec.action_id)
        safe_detail = _sanitize_terminal_text(detail)
        suffix = f" | {safe_detail}" if safe_detail else ""
        lines.append(
            f"[{rec.status.value:<10}] {rec.surface.value:<4} {safe_tool} "
            f"target={safe_target} id={safe_id}{suffix}"
        )
    return "\n".join(lines)


def _cmd_status(args: argparse.Namespace, config: SidefxConfig) -> int:
    db_path = resolve_db_path(args.db_path, config=config)
    session_id = args.session_id
    statuses = [ActionStatus(s) for s in args.statuses] if args.statuses else None
    surface = Surface(args.surface) if args.surface else None

    if not db_path.exists():
        counts = {s: 0 for s in ActionStatus}
        actions: list[ActionRecord] = []
        journal_exists = False
    else:
        journal_exists = True
        with Journal.open_readonly(db_path) as journal:
            counts = journal.count_by_status(session_id=session_id, surface=surface)
            actions = journal.list_actions(
                session_id=session_id,
                status=statuses,
                surface=surface,
                limit=args.limit,
                descending=True,
            )

    if args.as_json:
        payload: dict[str, Any] = {
            "db_path": str(db_path),
            "journal_exists": journal_exists,
            "total": sum(counts.values()),
            "counts": {s.value: counts[s] for s in ActionStatus},
            "actions": [rec.to_dict() for rec in actions],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(_format_status_text(db_path, counts, actions, journal_exists=journal_exists))
    return 0


def _cmd_init(args: argparse.Namespace, config: SidefxConfig) -> int:
    if args.claude_code:
        return _dispatch_optional(
            "sidefx.receipt", "handle_init_claude_code_cli", args, config, "init --claude-code"
        )

    dest = Path(args.init_path).expanduser()
    if dest.is_dir():
        print(f"error: {dest} is a directory", file=sys.stderr)
        return 1

    content = json.dumps(default_config_dict(), indent=2) + "\n"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not args.force:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(dest, flags, 0o644)
        except FileExistsError:
            print(
                f"error: {dest} already exists (pass --force to overwrite)",
                file=sys.stderr,
            )
            return 1
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    else:
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=dest.parent,
                prefix=".sidefx-init-",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(content)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    print(f"Created {dest}")
    return 0


def _dispatch_optional(
    module_name: str,
    func_name: str,
    args: argparse.Namespace,
    config: SidefxConfig,
    command_label: str,
) -> int:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name or (exc.name and module_name.startswith(f"{exc.name}.")):
            print(
                f"sidefx {command_label}: not yet implemented in this version.",
                file=sys.stderr,
            )
            return 2
        raise

    handler = getattr(module, func_name, None)
    if handler is None:
        print(
            f"sidefx {command_label}: not yet implemented in this version.",
            file=sys.stderr,
        )
        return 2
    return int(handler(args, config))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning a process exit code."""
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else None
    args, extras = parser.parse_known_args(raw_argv)

    if not getattr(args, "command", None):
        if extras:
            parser.error(f"unrecognized arguments: {' '.join(extras)}")
        parser.print_help()
        return 0

    if getattr(args, "passthrough", False):
        args.args = extras
    elif extras:
        parser.error(f"unrecognized arguments: {' '.join(extras)}")

    skip_config_load = (
        args.command == "init"
        and not getattr(args, "claude_code", False)
        and args.config_path is None
    )
    if skip_config_load:
        config = SidefxConfig()
    else:
        try:
            config = load_config(args.config_path)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        return int(args.handler(args, config))
    except (ConfigError, JournalError, ValueError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
