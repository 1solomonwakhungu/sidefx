"""Ground-truth session side-effects receipt renderer and Claude Code Stop hook setup."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidefx.config import CONFIG_FILENAME, ConfigError, SidefxConfig, load_config
from sidefx.journal import ActionRecord, ActionStatus, Journal, resolve_db_path
from sidefx.reconcilers.base import parse_summary_meta

DEFAULT_CLAUDE_SETTINGS_PATH = Path(".claude") / "settings.json"
DEFAULT_STOP_HOOK_COMMAND = "sidefx report --format markdown"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BIDI_OVERRIDE_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def sanitize_text(text: str) -> str:
    """Strip ANSI escape sequences, control chars, and bidi overrides for safe display."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    stripped = _BIDI_OVERRIDE_RE.sub("", stripped)
    return _CONTROL_CHARS_RE.sub(" ", stripped).strip()


def _md_code(text: str) -> str:
    """Wrap sanitized text in a Markdown inline code span safely even if it contains backticks."""
    safe = sanitize_text(text)
    if "`" not in safe:
        return f"`{safe}`"
    # Choose a fence longer than any backtick run in `safe`.
    longest = max(len(m.group(0)) for m in re.finditer(r"`+", safe))
    fence = "`" * (longest + 1)
    if safe.startswith("`") or safe.endswith("`"):
        return f"{fence} {safe} {fence}"
    return f"{fence}{safe}{fence}"


def _clean_record_summary(record: ActionRecord) -> str:
    display, _ = parse_summary_meta(record.summary)
    return sanitize_text(display) if display else ""


@dataclass(frozen=True, slots=True)
class ReceiptData:
    """Immutable snapshot of journaled side effects loaded strictly read-only."""

    db_path: Path
    journal_exists: bool
    session_id: str | None
    actions: tuple[ActionRecord, ...]
    counts: dict[ActionStatus, int]

    @property
    def total(self) -> int:
        return len(self.actions)

    @property
    def confirmed(self) -> tuple[ActionRecord, ...]:
        return tuple(a for a in self.actions if a.status == ActionStatus.CONFIRMED)

    @property
    def reconciled(self) -> tuple[ActionRecord, ...]:
        return tuple(a for a in self.actions if a.status == ActionStatus.RECONCILED)

    @property
    def failed(self) -> tuple[ActionRecord, ...]:
        return tuple(a for a in self.actions if a.status == ActionStatus.FAILED)

    @property
    def unknown(self) -> tuple[ActionRecord, ...]:
        """Return both ``unknown`` and ``pending`` unresolved actions."""
        return tuple(
            a for a in self.actions if a.status in (ActionStatus.UNKNOWN, ActionStatus.PENDING)
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize receipt to a machine-readable JSON dictionary."""
        return {
            "db_path": str(self.db_path),
            "journal_exists": self.journal_exists,
            "session_id": self.session_id,
            "total": self.total,
            "counts": {s.value: self.counts[s] for s in ActionStatus},
            "unresolved_count": (
                self.counts[ActionStatus.UNKNOWN] + self.counts[ActionStatus.PENDING]
            ),
            "groups": {
                "confirmed": [a.to_dict() for a in self.confirmed],
                "reconciled": [a.to_dict() for a in self.reconciled],
                "failed": [a.to_dict() for a in self.failed],
                "unknown": [a.to_dict() for a in self.unknown],
            },
            "actions": [a.to_dict() for a in self.actions],
        }


def load_receipt_data(
    db_path: Path,
    *,
    session_id: str | None = None,
) -> ReceiptData:
    """Load side-effects receipt rows strictly read-only without creating missing DBs."""
    if not db_path.exists():
        return ReceiptData(
            db_path=db_path,
            journal_exists=False,
            session_id=session_id,
            actions=(),
            counts={s: 0 for s in ActionStatus},
        )

    with Journal.open_readonly(db_path) as journal:
        actions = tuple(
            journal.list_actions(
                session_id=session_id,
                descending=False,
            )
        )
        counts = journal.count_by_status(session_id=session_id)

    return ReceiptData(
        db_path=db_path,
        journal_exists=True,
        session_id=session_id,
        actions=actions,
        counts=counts,
    )


def render_receipt_markdown(data: ReceiptData) -> str:
    """Render a compact, human-scannable Markdown receipt strictly from journal rows."""
    session_label = _md_code(data.session_id) if data.session_id else "`all`"
    lines = [
        "## sidefx Session Receipt",
        f"- **Session:** {session_label}",
        f"- **Journal:** {_md_code(str(data.db_path))}",
    ]

    if not data.journal_exists:
        lines.append("- **Totals:** 0 actions (no journal database found).")
        return "\n".join(lines)

    unresolved = len(data.unknown)
    pending_count = data.counts[ActionStatus.PENDING]
    unknown_label = (
        f"{unresolved} unknown ({pending_count} pending)"
        if pending_count > 0
        else f"{unresolved} unknown"
    )
    lines.append(
        f"- **Totals:** {data.total} total ("
        f"{len(data.confirmed)} confirmed, "
        f"{len(data.reconciled)} reconciled, "
        f"{len(data.failed)} failed, "
        f"{unknown_label})"
    )

    if data.total == 0:
        lines.append("")
        lines.append("_No side effects recorded._")
        return "\n".join(lines)

    sections: tuple[tuple[str, tuple[ActionRecord, ...], bool], ...] = (
        ("Confirmed", data.confirmed, False),
        ("Reconciled", data.reconciled, False),
        ("Failed", data.failed, False),
        ("Unknown / Unresolved", data.unknown, True),
    )

    for heading, records, is_unresolved in sections:
        if not records:
            continue
        lines.append("")
        lines.append(f"### {heading} ({len(records)})")
        for rec in records:
            parts = [
                _md_code(rec.action_id),
                _md_code(rec.surface.value),
                _md_code(rec.tool_name),
                f"target {_md_code(rec.target)}",
            ]
            if is_unresolved:
                parts.insert(1, f"[{rec.status.value}]")

            line = "- " + " · ".join(parts)
            if rec.result_ref:
                safe_ref = sanitize_text(rec.result_ref)
                if safe_ref.startswith("http://") or safe_ref.startswith("https://"):
                    line += f" -> {safe_ref}"
                else:
                    line += f" -> {_md_code(safe_ref)}"
            elif rec.error:
                line += f" — error: {sanitize_text(rec.error)}"
            else:
                clean_sum = _clean_record_summary(rec)
                if clean_sum:
                    line += f" — {clean_sum}"

            if is_unresolved:
                line += f" (marker: {_md_code(rec.marker)})"
            lines.append(line)

    return "\n".join(lines)


def render_receipt_text(data: ReceiptData) -> str:
    """Render a compact plain-text receipt strictly from journal rows."""
    session_label = sanitize_text(data.session_id) if data.session_id else "all"
    lines = [
        "sidefx Session Receipt",
        f"Session: {session_label}",
        f"Journal: {sanitize_text(str(data.db_path))}",
    ]

    if not data.journal_exists:
        lines.append("Totals: 0 actions (no journal database found).")
        return "\n".join(lines)

    unresolved = len(data.unknown)
    pending_count = data.counts[ActionStatus.PENDING]
    unknown_label = (
        f"{unresolved} unknown ({pending_count} pending)"
        if pending_count > 0
        else f"{unresolved} unknown"
    )
    lines.append(
        f"Totals: {data.total} total ("
        f"{len(data.confirmed)} confirmed, "
        f"{len(data.reconciled)} reconciled, "
        f"{len(data.failed)} failed, "
        f"{unknown_label})"
    )

    if data.total == 0:
        lines.append("No side effects recorded.")
        return "\n".join(lines)

    sections: tuple[tuple[str, tuple[ActionRecord, ...], bool], ...] = (
        ("Confirmed", data.confirmed, False),
        ("Reconciled", data.reconciled, False),
        ("Failed", data.failed, False),
        ("Unknown / Unresolved", data.unknown, True),
    )

    for heading, records, is_unresolved in sections:
        if not records:
            continue
        lines.append("")
        lines.append(f"{heading} ({len(records)}):")
        for rec in records:
            status_tag = f"[{rec.status.value}] " if is_unresolved else ""
            base = (
                f"  - {status_tag}{sanitize_text(rec.action_id)} "
                f"({rec.surface.value} {sanitize_text(rec.tool_name)} "
                f"target={sanitize_text(rec.target)})"
            )
            if rec.result_ref:
                base += f" -> {sanitize_text(rec.result_ref)}"
            elif rec.error:
                base += f" | error: {sanitize_text(rec.error)}"
            else:
                clean_sum = _clean_record_summary(rec)
                if clean_sum:
                    base += f" | {clean_sum}"
            if is_unresolved:
                base += f" [marker={sanitize_text(rec.marker)}]"
            lines.append(base)

    return "\n".join(lines)


def render_receipt_json(data: ReceiptData) -> str:
    """Render a machine-readable JSON receipt strictly from journal rows."""
    return json.dumps(data.to_dict(), indent=2)


def render_receipt(data: ReceiptData, *, fmt: str = "markdown") -> str:
    """Render ``data`` in ``markdown``, ``text``, or ``json`` format."""
    if fmt == "markdown":
        return render_receipt_markdown(data)
    if fmt == "text":
        return render_receipt_text(data)
    if fmt == "json":
        return render_receipt_json(data)
    raise ValueError(f"Unsupported receipt format: {fmt!r}")


def _build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx report",
        description="Render a ground-truth session side-effects receipt from the durable journal.",
    )
    parser.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Filter receipt to a single session ID.",
    )
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("markdown", "text", "json"),
        default=None,
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--markdown",
        dest="as_markdown",
        action="store_true",
        help="Shorthand for --format markdown.",
    )
    parser.add_argument(
        "--text",
        dest="as_text",
        action="store_true",
        help="Shorthand for --format text.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    return parser


def _resolve_report_format(sub_args: argparse.Namespace) -> str:
    chosen: list[str] = []
    if sub_args.fmt is not None:
        chosen.append(sub_args.fmt)
    if sub_args.as_markdown:
        chosen.append("markdown")
    if sub_args.as_text:
        chosen.append("text")
    if sub_args.as_json:
        chosen.append("json")
    unique = list(dict.fromkeys(chosen))
    if len(unique) > 1:
        raise ValueError(f"Conflicting output format flags: {', '.join(unique)}")
    return unique[0] if unique else "markdown"


def handle_report_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx report``."""
    parser = _build_report_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))
    fmt = _resolve_report_format(sub_args)

    effective_config = load_config(sub_args.config_path) if sub_args.config_path else config
    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    session_id = sub_args.session_id or getattr(args, "session_id", None)
    db_path = resolve_db_path(db_raw, config=effective_config)

    data = load_receipt_data(db_path, session_id=session_id)
    print(render_receipt(data, fmt=fmt))
    return 0


def claude_code_stop_hook_snippet(
    command: str = DEFAULT_STOP_HOOK_COMMAND,
) -> dict[str, Any]:
    """Return a Claude Code ``Stop`` hook settings snippet for printing the session receipt."""
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                        }
                    ]
                }
            ]
        }
    }


def _stop_hook_already_present(stop_entries: list[Any], command: str) -> bool:
    for entry in stop_entries:
        if not isinstance(entry, dict):
            continue
        # Legacy flat hook format: {"type": "command", "command": "..."}
        if entry.get("type") == "command" and entry.get("command") == command:
            return True
        # Current Claude Code matcher-group format: {"hooks": [{"type": "command", ...}]}
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for hook in nested:
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and hook.get("command") == command
                ):
                    return True
    return False


def merge_claude_code_settings(
    settings_path: Path,
    *,
    command: str = DEFAULT_STOP_HOOK_COMMAND,
) -> tuple[dict[str, Any], bool]:
    """Safely merge a ``sidefx report`` Stop hook into a Claude Code ``settings.json`` file.

    Preserves all existing settings, other hook events, and existing ``Stop`` hooks.
    Idempotent: if the exact hook command is already registered, does not duplicate it.
    Returns ``(merged_settings_dict, modified_bool)``.
    """
    if settings_path.is_dir():
        raise ConfigError(f"Settings path is a directory, expected a JSON file: {settings_path}")

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            raw_text = settings_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"Failed to read {settings_path}: {exc}") from exc
        if raw_text.strip():
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"Malformed JSON in {settings_path} at line {exc.lineno}, "
                    f"column {exc.colno}: {exc.msg}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ConfigError(f"Expected JSON object in {settings_path}")
            existing = parsed

    hooks = existing.get("hooks")
    if hooks is None:
        hooks = {}
        existing["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise ConfigError(f"Expected 'hooks' to be a JSON object in {settings_path}")

    stop_list = hooks.get("Stop")
    if stop_list is None:
        stop_list = []
        hooks["Stop"] = stop_list
    elif not isinstance(stop_list, list):
        raise ConfigError(f"Expected 'hooks.Stop' to be a JSON list in {settings_path}")

    if _stop_hook_already_present(stop_list, command):
        return existing, False

    stop_list.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ]
        }
    )

    content = json.dumps(existing, indent=2) + "\n"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=settings_path.parent,
            prefix=".sidefx-claude-settings-",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, settings_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return existing, True


def handle_init_claude_code_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx init --claude-code``."""
    del config  # Not required to generate hook snippet.
    cmd_parts = ["sidefx", "report", "--format", "markdown"]
    db_raw = getattr(args, "db_path", None)
    if db_raw:
        cmd_parts.extend(["--db", shlex.quote(str(db_raw))])
    session_id = getattr(args, "session_id", None)
    if session_id:
        cmd_parts.extend(["--session-id", shlex.quote(str(session_id))])
    command = " ".join(cmd_parts)

    explicit_settings = getattr(args, "settings_path", None)
    write_flag = bool(getattr(args, "write", False))
    init_path = getattr(args, "init_path", CONFIG_FILENAME)

    dest: Path | None = None
    if explicit_settings:
        dest = Path(str(explicit_settings)).expanduser()
    elif init_path and str(init_path) != CONFIG_FILENAME:
        dest = Path(str(init_path)).expanduser()
    elif write_flag:
        dest = DEFAULT_CLAUDE_SETTINGS_PATH

    if dest is not None:
        _, modified = merge_claude_code_settings(dest, command=command)
        if modified:
            print(f"Merged sidefx Stop hook into {dest}")
        else:
            print(f"sidefx Stop hook already present in {dest} (unchanged)")
        return 0

    print(json.dumps(claude_code_stop_hook_snippet(command), indent=2))
    return 0
