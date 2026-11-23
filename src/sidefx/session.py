"""Workspace/journal-scoped session persistence and ``sidefx session`` CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sidefx.config import load_config
from sidefx.journal import _write_journal_gitignore, resolve_db_path

if TYPE_CHECKING:
    from sidefx.config import SidefxConfig

ENV_SESSION_ID = "SIDEFX_SESSION_ID"
ENV_CLAUDE_CODE_SESSION_ID = "CLAUDE_CODE_SESSION_ID"
SESSION_FILENAME = "current-session"


def session_file_path(db_path: Path) -> Path:
    """Return the persisted active-session file path alongside ``db_path``."""
    return db_path.parent / SESSION_FILENAME


def _validate_session_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        return None
    return cleaned


def _generate_session_id(prefix: str = "sess") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def read_current_session(db_path: Path | None) -> str | None:
    """Read `<journal_dir>/current-session` without creating directories or files."""
    if db_path is None:
        return None
    path = session_file_path(db_path)
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    return _validate_session_id(first_line)


def _write_temp_session_file(journal_dir: Path, session_id: str) -> Path:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=journal_dir,
            prefix=".sidefx-session-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(f"{session_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return tmp_path
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _create_session_file_excl(target: Path, session_id: str) -> bool:
    """Create ``target`` atomically with ``O_CREAT | O_EXCL``; return True if created."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{session_id}\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def ensure_persisted_session(db_path: Path, *, prefix: str = "sess") -> str:
    """Return `<journal_dir>/current-session`, creating it atomically if absent.

    Uses atomic temp-file + hardlink (create-if-absent) with ``O_CREAT | O_EXCL``
    fallback so concurrent proxy/wrapper startups in a fresh workspace converge on
    the exact same session ID and readers never observe a partially written file.
    """
    existing = read_current_session(db_path)
    if existing is not None:
        return existing

    journal_dir = db_path.parent
    created_dir = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if created_dir:
        _write_journal_gitignore(journal_dir)

    target = session_file_path(db_path)
    candidate = _generate_session_id(prefix=prefix)
    tmp_path = _write_temp_session_file(journal_dir, candidate)
    try:
        try:
            os.link(tmp_path, target)
            return candidate
        except FileExistsError:
            winner = read_current_session(db_path)
            if winner is not None:
                return winner
            # Existing file is empty/corrupt: try atomic unlink + O_CREAT|O_EXCL retry.
            with suppress(OSError):
                target.unlink(missing_ok=True)
            if _create_session_file_excl(target, candidate):
                return candidate
            winner = read_current_session(db_path)
            if winner is not None:
                return winner
            os.replace(tmp_path, target)
            tmp_path = None  # type: ignore[assignment]
            return candidate
        except OSError:
            # Filesystem without hardlink support: use atomic O_CREAT|O_EXCL create-if-absent.
            if _create_session_file_excl(target, candidate):
                return candidate
            winner = read_current_session(db_path)
            if winner is not None:
                return winner
            os.replace(tmp_path, target)
            tmp_path = None  # type: ignore[assignment]
            return candidate
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def rotate_session(
    db_path: Path,
    *,
    session_id: str | None = None,
    prefix: str = "sess",
) -> tuple[str, str | None]:
    """Atomically write a new active session ID into `<journal_dir>/current-session`.

    Returns ``(new_session_id, previous_session_id)``.
    """
    if session_id is not None:
        validated = _validate_session_id(session_id)
        if validated is None:
            raise ValueError("session_id must be a non-empty single-line string without NUL bytes")
        new_id = validated
    else:
        new_id = _generate_session_id(prefix=prefix)

    previous = read_current_session(db_path)
    journal_dir = db_path.parent
    created_dir = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if created_dir:
        _write_journal_gitignore(journal_dir)

    target = session_file_path(db_path)
    tmp_path = _write_temp_session_file(journal_dir, new_id)
    try:
        os.replace(tmp_path, target)
        tmp_path = None  # type: ignore[assignment]
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    return new_id, previous


def resolve_session_id(
    *,
    explicit: str | None = None,
    config: SidefxConfig | None = None,
    db_path: Path | None = None,
    create: bool = False,
    fallback_prefix: str = "sess",
) -> tuple[str | None, str]:
    """Resolve the active session ID and its source label.

    Precedence:
      1. Explicit argument (``cli``)
      2. ``SIDEFX_SESSION_ID`` environment variable (``env:SIDEFX_SESSION_ID``)
      3. ``config.default_session_id`` (``config``)
      4. ``CLAUDE_CODE_SESSION_ID`` environment variable (``env:CLAUDE_CODE_SESSION_ID``)
      5. Persisted ``<journal_dir>/current-session`` (``file``)
      6. Ephemeral random ID when ``create=True`` and ``db_path is None`` (``ephemeral``),
         otherwise ``(None, "none")``.
    """
    explicit_clean = _validate_session_id(explicit)
    if explicit_clean is not None:
        return explicit_clean, "cli"

    env_sidefx = _validate_session_id(os.environ.get(ENV_SESSION_ID))
    if env_sidefx is not None:
        return env_sidefx, f"env:{ENV_SESSION_ID}"

    if config is not None:
        cfg_session = _validate_session_id(config.default_session_id)
        if cfg_session is not None:
            return cfg_session, "config"

    env_claude = _validate_session_id(os.environ.get(ENV_CLAUDE_CODE_SESSION_ID))
    if env_claude is not None:
        return env_claude, f"env:{ENV_CLAUDE_CODE_SESSION_ID}"

    if db_path is not None:
        if create:
            try:
                return ensure_persisted_session(db_path, prefix=fallback_prefix), "file"
            except OSError:
                return _generate_session_id(prefix=fallback_prefix), "ephemeral"
        persisted = read_current_session(db_path)
        if persisted is not None:
            return persisted, "file"

    if create:
        return _generate_session_id(prefix=fallback_prefix), "ephemeral"
    return None, "none"


def _build_session_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx session",
        description="Inspect or rotate the workspace-scoped active sidefx session ID.",
    )
    subparsers = parser.add_subparsers(dest="session_action", metavar="<subcommand>")

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
        p.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
        p.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit machine-readable JSON output.",
        )

    cur_p = subparsers.add_parser(
        "current",
        help="Show the active session ID and where it came from (read-only).",
    )
    _add_common(cur_p)

    new_p = subparsers.add_parser(
        "new",
        help="Generate (or set via --id) a new persisted session ID in current-session.",
    )
    _add_common(new_p)
    new_p.add_argument(
        "--id",
        dest="custom_id",
        default=None,
        help="Explicit session ID to persist instead of generating one.",
    )

    return parser


def handle_session_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx session <current|new>``."""
    parser = _build_session_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))
    if not sub_args.session_action:
        parser.print_help()
        return 0

    effective_config = load_config(sub_args.config_path) if sub_args.config_path else config
    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    db_path = resolve_db_path(db_raw, config=effective_config)
    sess_file = session_file_path(db_path)

    if sub_args.session_action == "current":
        explicit = getattr(args, "session_id", None)
        session_id, source = resolve_session_id(
            explicit=explicit,
            config=effective_config,
            db_path=db_path,
            create=False,
        )
        if sub_args.as_json:
            payload: dict[str, Any] = {
                "session_id": session_id,
                "source": source,
                "session_file": str(sess_file),
                "session_file_exists": sess_file.is_file(),
                "db_path": str(db_path),
            }
            print(json.dumps(payload, indent=2))
            return 0
        if session_id is None:
            print("(none)")
        else:
            print(f"{session_id} ({source})")
        return 0

    if sub_args.session_action == "new":
        new_id, previous = rotate_session(db_path, session_id=sub_args.custom_id)
        override_env: str | None = None
        if _validate_session_id(os.environ.get(ENV_SESSION_ID)):
            override_env = ENV_SESSION_ID
        elif _validate_session_id(os.environ.get(ENV_CLAUDE_CODE_SESSION_ID)):
            override_env = ENV_CLAUDE_CODE_SESSION_ID

        if sub_args.as_json:
            payload = {
                "session_id": new_id,
                "previous_session_id": previous,
                "session_file": str(sess_file),
                "db_path": str(db_path),
                "overridden_by_env": override_env,
            }
            print(json.dumps(payload, indent=2))
            return 0

        print(new_id)
        if override_env is not None:
            print(
                f"note: {override_env} is currently set in your environment and overrides "
                f"{sess_file}",
                file=sys.stderr,
            )
        return 0

    parser.print_help()
    return 0
