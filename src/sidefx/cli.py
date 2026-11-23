"""Command-line interface for sidefx."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
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
    ENV_DB_PATH,
    ActionRecord,
    ActionStatus,
    Journal,
    JournalError,
    Surface,
    resolve_db_path,
)

DEFAULT_STATUS_LIMIT = 20


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


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
        help=f"Path to {CONFIG_FILENAME} configuration file.",
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Session identifier filter or override.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # status
    status_parser = subparsers.add_parser(
        "status",
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
        "--session-id",
        dest="status_session_id",
        default=None,
        help="Filter actions to a specific session ID.",
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
    status_parser.set_defaults(handler=_cmd_status)

    # init
    init_parser = subparsers.add_parser(
        "init",
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
    init_parser.set_defaults(handler=_cmd_init)

    # Modular dispatch stubs for upcoming milestones (PR 2 & PR 3)
    report_parser = subparsers.add_parser(
        "report",
        help="Render a ground-truth session side-effects receipt.",
    )
    report_parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    report_parser.set_defaults(
        handler=lambda ns, cfg: _dispatch_optional(
            "sidefx.receipt", "handle_report_cli", ns, cfg, "report"
        )
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Reconcile pending or unknown actions against external systems.",
    )
    reconcile_parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    reconcile_parser.set_defaults(
        handler=lambda ns, cfg: _dispatch_optional(
            "sidefx.reconcilers", "handle_reconcile_cli", ns, cfg, "reconcile"
        )
    )

    wrap_parser = subparsers.add_parser(
        "wrap",
        help="Wrap a mutating CLI command with write-ahead journaling.",
    )
    wrap_parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    wrap_parser.set_defaults(
        handler=lambda ns, cfg: _dispatch_optional(
            "sidefx.wrapper", "handle_wrap_cli", ns, cfg, "wrap"
        )
    )

    mcp_proxy_parser = subparsers.add_parser(
        "mcp-proxy",
        help="Run transparent stdio JSON-RPC 2.0 MCP proxy in front of an MCP server.",
    )
    mcp_proxy_parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    mcp_proxy_parser.set_defaults(
        handler=lambda ns, cfg: _dispatch_optional(
            "sidefx.mcp_proxy", "handle_mcp_proxy_cli", ns, cfg, "mcp-proxy"
        )
    )

    return parser


def _resolve_effective_db_path(args: argparse.Namespace, config: SidefxConfig) -> Path:
    if args.db_path is not None:
        return resolve_db_path(args.db_path)
    if os.environ.get(ENV_DB_PATH, "").strip():
        return resolve_db_path()
    if config.journal_path is not None:
        configured = Path(config.journal_path).expanduser()
        if not configured.is_absolute() and config.source_path is not None:
            return (config.source_path.parent / configured).resolve()
        return configured
    return resolve_db_path()


def _format_status_text(
    db_path: Path,
    counts: dict[ActionStatus, int],
    actions: list[ActionRecord],
    *,
    journal_exists: bool,
) -> str:
    total = sum(counts.values())
    lines = [f"sidefx journal: {db_path}"]
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
        suffix = f" | {detail}" if detail else ""
        lines.append(
            f"[{rec.status.value:<10}] {rec.surface.value:<4} {rec.tool_name} "
            f"target={rec.target} id={rec.action_id}{suffix}"
        )
    return "\n".join(lines)


def _cmd_status(args: argparse.Namespace, config: SidefxConfig) -> int:
    db_path = _resolve_effective_db_path(args, config)
    session_id = args.status_session_id or args.session_id
    statuses = [ActionStatus(s) for s in args.statuses] if args.statuses else None
    surface = Surface(args.surface) if args.surface else None

    if not db_path.exists():
        counts = {s: 0 for s in ActionStatus}
        actions: list[ActionRecord] = []
        journal_exists = False
    else:
        journal_exists = True
        with Journal(db_path) as journal:
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
    if dest.exists() and not args.force:
        print(
            f"error: {dest} already exists (pass --force to overwrite)",
            file=sys.stderr,
        )
        return 1
    if dest.exists() and dest.is_dir():
        print(f"error: {dest} is a directory", file=sys.stderr)
        return 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(default_config_dict(), indent=2) + "\n"
    dest.write_text(content, encoding="utf-8")
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

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
    except (ConfigError, JournalError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
