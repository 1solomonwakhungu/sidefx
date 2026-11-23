"""Environment and configuration diagnostics (``sidefx doctor``)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sidefx import __version__
from sidefx.config import (
    CONFIG_FILENAME,
    ENV_CONFIG_PATH,
    ConfigError,
    SidefxConfig,
    discover_config_path,
    load_config,
)
from sidefx.journal import ActionStatus, Journal, JournalError, resolve_db_path
from sidefx.receipt import DEFAULT_CLAUDE_SETTINGS_PATH, DEFAULT_STOP_HOOK_COMMAND
from sidefx.session import read_current_session, resolve_session_id, session_file_path


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """Single diagnostic check emitted by ``sidefx doctor``."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


def _check_python() -> DoctorCheck:
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    ok = (ver.major, ver.minor) >= (3, 11)
    return DoctorCheck(
        name="python",
        status="ok" if ok else "fail",
        summary=(
            f"Python {ver_str} (SQLite {sqlite3.sqlite_version})"
            if ok
            else f"Python {ver_str} is unsupported; sidefx requires Python >= 3.11"
        ),
        details={
            "version": ver_str,
            "executable": sys.executable,
            "sqlite_version": sqlite3.sqlite_version,
        },
    )


def _check_path() -> DoctorCheck:
    found = shutil.which("sidefx")
    if found:
        return DoctorCheck(
            name="path",
            status="ok",
            summary=f"`sidefx` found on PATH at {found}",
            details={"executable": found},
        )
    return DoctorCheck(
        name="path",
        status="warn",
        summary=(
            "`sidefx` is not on PATH (install with `uv tool install sidefx` or "
            "`pip install sidefx`)"
        ),
        details={"executable": None},
    )


def _check_config(
    config_path: str | Path | None,
    *,
    cwd: Path,
) -> tuple[DoctorCheck, SidefxConfig]:
    explicit = config_path is not None
    env_raw = os.environ.get(ENV_CONFIG_PATH, "").strip()
    target_path: Path | None = None

    if explicit:
        target_path = Path(str(config_path)).expanduser()
    elif env_raw:
        target_path = Path(env_raw).expanduser()
    else:
        target_path = discover_config_path(cwd=cwd)

    if target_path is None:
        return (
            DoctorCheck(
                name="config",
                status="warn",
                summary=f"No {CONFIG_FILENAME} found; using built-in defaults (run `sidefx init`)",
                details={"path": None, "found": False},
            ),
            SidefxConfig(),
        )

    try:
        cfg = load_config(config_path, cwd=cwd)
    except ConfigError as exc:
        return (
            DoctorCheck(
                name="config",
                status="fail",
                summary=f"Invalid config at {target_path}: {exc}",
                details={
                    "path": str(target_path),
                    "found": target_path.exists(),
                    "error": str(exc),
                },
            ),
            SidefxConfig(),
        )

    return (
        DoctorCheck(
            name="config",
            status="ok",
            summary=(
                f"Valid {CONFIG_FILENAME} at {cfg.source_path} "
                f"({len(cfg.preflight.rules)} preflight rule(s), "
                f"timeout={cfg.default_timeout_seconds:g}s)"
            ),
            details={
                "path": str(cfg.source_path),
                "found": True,
                "rules_count": len(cfg.preflight.rules),
                "default_actor": cfg.default_actor,
                "default_timeout_seconds": cfg.default_timeout_seconds,
            },
        ),
        cfg,
    )


def _nearest_existing_dir(path: Path) -> Path:
    cur = path
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def _check_journal(
    db_path_override: str | Path | None,
    *,
    config: SidefxConfig,
    cwd: Path,
) -> DoctorCheck:
    try:
        db_path = resolve_db_path(db_path_override, config=config, cwd=cwd)
    except (ValueError, OSError) as exc:
        return DoctorCheck(
            name="journal",
            status="fail",
            summary=f"Invalid journal path: {exc}",
            details={"error": str(exc)},
        )

    journal_dir = db_path.parent
    sess_file = session_file_path(db_path)
    active_session, session_source = resolve_session_id(
        config=config, db_path=db_path, create=False
    )
    gitignore_exists = (journal_dir / ".gitignore").is_file()

    if db_path.exists():
        try:
            with Journal.open_readonly(db_path) as journal:
                counts = journal.count_by_status()
        except (JournalError, sqlite3.Error, OSError) as exc:
            return DoctorCheck(
                name="journal",
                status="fail",
                summary=f"Journal at {db_path} failed validation: {exc}",
                details={"db_path": str(db_path), "exists": True, "error": str(exc)},
            )

        total = sum(counts.values())
        unresolved = counts[ActionStatus.PENDING] + counts[ActionStatus.UNKNOWN]
        writable = os.access(journal_dir, os.W_OK | os.X_OK) and os.access(db_path, os.W_OK)
        status = "ok" if writable else "warn"
        summary = (
            f"Journal OK at {db_path} ({total} action(s), {unresolved} unresolved"
            + (f", session={active_session}" if active_session else "")
            + ("" if writable else ", read-only")
            + ")"
        )
        return DoctorCheck(
            name="journal",
            status=status,
            summary=summary,
            details={
                "db_path": str(db_path),
                "exists": True,
                "writable": writable,
                "gitignore_present": gitignore_exists,
                "session_file": str(sess_file),
                "persisted_session": read_current_session(db_path),
                "active_session": active_session,
                "active_session_source": session_source,
                "total_actions": total,
                "unresolved_actions": unresolved,
            },
        )

    ancestor = _nearest_existing_dir(journal_dir)
    writable = ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
    return DoctorCheck(
        name="journal",
        status="ok" if writable else "fail",
        summary=(
            f"Journal not yet created ({db_path}); directory {ancestor} is writable"
            if writable
            else f"Journal directory ancestor {ancestor} is not writable for {db_path}"
        ),
        details={
            "db_path": str(db_path),
            "exists": False,
            "writable": writable,
            "checked_ancestor": str(ancestor),
            "session_file": str(sess_file),
            "active_session": active_session,
            "active_session_source": session_source,
        },
    )


def _extract_stop_commands(parsed: dict[str, Any]) -> list[str]:
    hooks = parsed.get("hooks")
    if not isinstance(hooks, dict):
        return []
    stop_entries = hooks.get("Stop")
    if not isinstance(stop_entries, list):
        return []
    commands: list[str] = []
    for entry in stop_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "command" and isinstance(entry.get("command"), str):
            commands.append(entry["command"])
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for hook in nested:
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and isinstance(hook.get("command"), str)
                ):
                    commands.append(hook["command"])
    return commands


def _check_claude_code_hook(
    settings_path_override: str | Path | None,
    *,
    cwd: Path,
) -> DoctorCheck:
    candidates: list[Path] = []
    if settings_path_override is not None:
        candidates.append(Path(str(settings_path_override)).expanduser())
    else:
        for rel in (
            DEFAULT_CLAUDE_SETTINGS_PATH,
            Path(".claude") / "settings.local.json",
            Path("hooks") / "hooks.json",
        ):
            candidates.append(cwd / rel)
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
        if plugin_root:
            plugin_hooks = Path(plugin_root) / "hooks" / "hooks.json"
            if plugin_hooks not in candidates:
                candidates.append(plugin_hooks)
        try:
            home = Path.home()
            for rel in (
                DEFAULT_CLAUDE_SETTINGS_PATH,
                Path(".claude") / "settings.local.json",
            ):
                home_settings = home / rel
                if home_settings not in candidates:
                    candidates.append(home_settings)
        except RuntimeError:
            pass

    checked_paths: list[str] = []
    legacy_found: tuple[Path, str] | None = None

    for cand in candidates:
        checked_paths.append(str(cand))
        if not cand.exists():
            continue
        if cand.is_dir():
            return DoctorCheck(
                name="claude_code_hook",
                status="fail",
                summary=f"Claude Code settings path is a directory: {cand}",
                details={"checked": checked_paths, "path": str(cand)},
            )
        try:
            raw_text = cand.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return DoctorCheck(
                name="claude_code_hook",
                status="fail",
                summary=f"Failed to read Claude Code settings at {cand}: {exc}",
                details={"checked": checked_paths, "path": str(cand), "error": str(exc)},
            )
        if not raw_text.strip():
            continue
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return DoctorCheck(
                name="claude_code_hook",
                status="fail",
                summary=f"Malformed JSON in {cand} at line {exc.lineno}: {exc.msg}",
                details={"checked": checked_paths, "path": str(cand), "error": str(exc)},
            )
        if not isinstance(parsed, dict):
            return DoctorCheck(
                name="claude_code_hook",
                status="fail",
                summary=f"Expected JSON object in {cand}",
                details={"checked": checked_paths, "path": str(cand)},
            )

        commands = _extract_stop_commands(parsed)
        for cmd in commands:
            if DEFAULT_STOP_HOOK_COMMAND in cmd or "hook claude-code" in cmd:
                return DoctorCheck(
                    name="claude_code_hook",
                    status="ok",
                    summary=f"Claude Code Stop hook configured in {cand}",
                    details={
                        "checked": checked_paths,
                        "path": str(cand),
                        "command": cmd,
                    },
                )
            if "sidefx" in cmd and "report" in cmd and legacy_found is None:
                legacy_found = (cand, cmd)

    if legacy_found is not None:
        legacy_path, legacy_cmd = legacy_found
        return DoctorCheck(
            name="claude_code_hook",
            status="warn",
            summary=(
                f"Legacy Stop hook found in {legacy_path} ({legacy_cmd!r}); "
                "update to `sidefx hook claude-code` via `sidefx init --claude-code --write`"
            ),
            details={
                "checked": checked_paths,
                "path": str(legacy_path),
                "command": legacy_cmd,
                "legacy": True,
            },
        )

    return DoctorCheck(
        name="claude_code_hook",
        status="warn",
        summary=(
            "Claude Code Stop hook not configured "
            "(run `sidefx init --claude-code --write` to enable session receipts)"
        ),
        details={"checked": checked_paths, "configured": False},
    )


def run_doctor(
    *,
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
    settings_path: str | Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run all sidefx environment and setup checks strictly read-only."""
    effective_cwd = cwd if cwd is not None else Path.cwd()
    py_check = _check_python()
    path_check = _check_path()
    cfg_check, cfg = _check_config(config_path, cwd=effective_cwd)
    journal_check = _check_journal(db_path, config=cfg, cwd=effective_cwd)
    hook_check = _check_claude_code_hook(settings_path, cwd=effective_cwd)

    checks = [py_check, path_check, cfg_check, journal_check, hook_check]
    overall_ok = all(c.status != "fail" for c in checks)
    return {
        "ok": overall_ok,
        "version": __version__,
        "checks": [c.to_dict() for c in checks],
    }


def _build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx doctor",
        description=(
            "Check sidefx installation, Python/SQLite runtime, .sidefx.json config, "
            "journal directory, and Claude Code Stop hook setup."
        ),
    )
    parser.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    parser.add_argument(
        "--settings",
        dest="settings_path",
        default=None,
        help="Path to Claude Code settings.json to inspect.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    return parser


def handle_doctor_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx doctor``."""
    del config  # Loaded and validated directly by doctor so invalid configs report cleanly.
    parser = _build_doctor_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))

    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    cfg_raw = sub_args.config_path or getattr(args, "config_path", None)
    report = run_doctor(
        db_path=db_raw,
        config_path=cfg_raw,
        settings_path=sub_args.settings_path,
    )

    if sub_args.as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    print(f"sidefx doctor (v{report['version']})")
    tag_map = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    for check in report["checks"]:
        tag = tag_map.get(check["status"], check["status"].upper())
        print(f"  [{tag:<4}] {check['name']}: {check['summary']}")

    return 0 if report["ok"] else 1
