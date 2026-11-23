"""``sidefx resolve``: record a manually verified outcome for an unresolved action.

Built-in reconcilers cover ``git push`` and GitHub issue/PR/comment writes. Every other
tool (an arbitrary MCP server, a deploy script, a webhook) has no read-only check sidefx
can run by itself, so an ``unknown`` row would block that exact call forever. ``resolve``
is the audited way out: whoever verified the external system records what they saw, and
the journal keeps that attestation in the row so receipts show it was a manual call.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sidefx.config import SidefxConfig, load_config
from sidefx.journal import (
    ActionRecord,
    ActionStatus,
    InvalidStateTransitionError,
    Journal,
    StaleAttemptError,
    resolve_db_path,
)
from sidefx.reconcilers.base import sanitize_text

MANUAL_LANDED_PREFIX = "manually resolved as landed"
MANUAL_NOT_LANDED_PREFIX = "manually resolved as not landed"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

_MAX_NOTE_LENGTH = 500
_LANDED_STATUSES = (ActionStatus.CONFIRMED, ActionStatus.RECONCILED)


def _build_resolve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx resolve",
        description=(
            "Record a manually verified outcome for a pending or unknown action that no "
            "reconciler can check. Verify the external system first: --landed makes retries "
            "replay the recorded result, --not-landed lets the same call run again."
        ),
    )
    parser.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    parser.add_argument(
        "--action-id",
        dest="action_id",
        required=True,
        help="Action ID to resolve (shown in the OUTCOME UNKNOWN error and `sidefx status`).",
    )
    verdict = parser.add_mutually_exclusive_group(required=True)
    verdict.add_argument(
        "--landed",
        dest="landed",
        action="store_true",
        help="The write exists on the external system. Marks the action reconciled.",
    )
    verdict.add_argument(
        "--not-landed",
        dest="not_landed",
        action="store_true",
        help="The write does not exist. Marks the action failed so it can be retried.",
    )
    parser.add_argument(
        "--ref",
        dest="result_ref",
        default=None,
        help="With --landed: URL or ID of the created resource, replayed to later retries.",
    )
    parser.add_argument(
        "--note",
        dest="note",
        default=None,
        help="How the outcome was verified; stored in the journal and shown in receipts.",
    )
    parser.add_argument(
        "--actor",
        dest="actor",
        default=None,
        help="Disambiguate when the same action ID exists for several actors.",
    )
    parser.add_argument(
        "--target",
        dest="target",
        default=None,
        help="Disambiguate when the same action ID exists for several targets.",
    )
    parser.add_argument(
        "--force-pending",
        dest="force_pending",
        action="store_true",
        help=(
            "Also resolve an action that is still pending (only once you are certain no "
            "worker is still executing it)."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    return parser


def _attestation(prefix: str, note: str | None) -> str:
    cleaned = sanitize_text(note)[:_MAX_NOTE_LENGTH] if note else ""
    return f"{prefix}: {cleaned}" if cleaned else prefix


def _describe(record: ActionRecord) -> str:
    return (
        f"{sanitize_text(record.action_id)} ({sanitize_text(record.tool_name)} "
        f"target={sanitize_text(record.target)} actor={sanitize_text(record.actor)})"
    )


def _fail(message: str, *, code: int = EXIT_ERROR) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _emit(
    *,
    as_json: bool,
    db_path: str,
    previous: ActionRecord,
    current: ActionRecord,
    verdict: str,
    applied: bool,
    text: str,
) -> None:
    if as_json:
        payload: dict[str, Any] = {
            "db_path": db_path,
            "action_id": current.action_id,
            "actor": current.actor,
            "target": current.target,
            "tool_name": current.tool_name,
            "verdict": verdict,
            "previous_status": previous.status.value,
            "current_status": current.status.value,
            "applied": applied,
            "result_ref": current.result_ref,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def handle_resolve_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx resolve``."""
    parser = _build_resolve_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))

    if sub_args.result_ref is not None and not sub_args.landed:
        return _fail("--ref is only valid with --landed", code=EXIT_USAGE)

    effective_config = load_config(sub_args.config_path) if sub_args.config_path else config
    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    db_path = resolve_db_path(db_raw, config=effective_config)
    if not db_path.exists():
        return _fail(f"journal database not found: {sanitize_text(str(db_path))}")

    verdict = "landed" if sub_args.landed else "not_landed"

    with Journal(db_path) as journal:
        matches = [
            rec
            for rec in journal.list_actions(
                actor=sub_args.actor, target=sub_args.target, descending=False
            )
            if rec.action_id == sub_args.action_id
        ]
        if not matches:
            return _fail(
                f"action_id not found: {sanitize_text(sub_args.action_id)} "
                "(run `sidefx status --status unknown --status pending` to list unresolved IDs)"
            )
        if len(matches) > 1:
            listing = "; ".join(_describe(rec) for rec in matches)
            return _fail(
                f"action_id {sanitize_text(sub_args.action_id)} matches {len(matches)} actions; "
                f"pass --target and/or --actor to pick one: {listing}",
                code=EXIT_USAGE,
            )

        record = matches[0]

        if record.status in _LANDED_STATUSES or record.status == ActionStatus.FAILED:
            agrees = (record.status in _LANDED_STATUSES) == sub_args.landed
            if not agrees:
                return _fail(
                    f"{_describe(record)} is already {record.status.value}; "
                    f"it cannot be resolved as {verdict.replace('_', ' ')}"
                )
            _emit(
                as_json=sub_args.as_json,
                db_path=str(db_path),
                previous=record,
                current=record,
                verdict=verdict,
                applied=False,
                text=f"{_describe(record)} is already {record.status.value} (unchanged).",
            )
            return EXIT_OK

        if record.status == ActionStatus.PENDING and not sub_args.force_pending:
            return _fail(
                f"{_describe(record)} is still pending, so a worker may be executing it right "
                "now. Wait for it to finish, or pass --force-pending once you are certain no "
                "process is still running it."
            )

        try:
            if sub_args.landed:
                updated = journal.mark_reconciled(
                    record.actor,
                    record.target,
                    record.action_id,
                    attempt=record.attempt,
                    result_ref=sub_args.result_ref,
                    summary=_attestation(MANUAL_LANDED_PREFIX, sub_args.note),
                )
            else:
                updated = journal.mark_failed(
                    record.actor,
                    record.target,
                    record.action_id,
                    attempt=record.attempt,
                    error=_attestation(MANUAL_NOT_LANDED_PREFIX, sub_args.note),
                )
        except (StaleAttemptError, InvalidStateTransitionError) as exc:
            return _fail(
                f"{_describe(record)} changed while resolving ({exc}); "
                "re-run `sidefx status` and try again"
            )

    transition = f"{record.status.value} -> {updated.status.value}"
    if sub_args.landed:
        text = (
            f"Resolved {_describe(record)} as landed ({transition}). "
            "Repeating the original call now replays the recorded result without re-sending it."
        )
    else:
        text = (
            f"Resolved {_describe(record)} as not landed ({transition}). "
            "The original call can now be repeated and will be sent again."
        )
    _emit(
        as_json=sub_args.as_json,
        db_path=str(db_path),
        previous=record,
        current=updated,
        verdict=verdict,
        applied=True,
        text=text,
    )
    return EXIT_OK
