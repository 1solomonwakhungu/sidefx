"""Reconciler registry, orchestration helpers, and ``sidefx reconcile`` CLI handler."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sidefx.config import SidefxConfig, load_config
from sidefx.guidance import resolve_command
from sidefx.journal import (
    ActionRecord,
    ActionStatus,
    InvalidStateTransitionError,
    Journal,
    StaleAttemptError,
    resolve_db_path,
)
from sidefx.reconcilers.base import (
    Reconciler,
    ReconcileResult,
    ReconcileState,
    format_summary_with_meta,
    parse_summary_meta,
    sanitize_text,
    validate_marker_token,
)
from sidefx.reconcilers.git_push import GitPushReconciler, GitPushSpec, inspect_git_push
from sidefx.reconcilers.github import (
    DEFAULT_GITHUB_API_URL,
    ENV_GITHUB_API_URL,
    GitHubReconciler,
    body_has_marker,
    extract_markers,
    format_marker_comment,
    inject_marker_into_body,
    inject_marker_into_gh_args,
    inject_marker_into_json_body,
)

__all__ = [
    "DEFAULT_GITHUB_API_URL",
    "ENV_GITHUB_API_URL",
    "GitHubReconciler",
    "GitPushReconciler",
    "GitPushSpec",
    "ReconcileOutcome",
    "ReconcileResult",
    "ReconcileState",
    "Reconciler",
    "body_has_marker",
    "default_reconcilers",
    "extract_markers",
    "format_marker_comment",
    "format_summary_with_meta",
    "handle_reconcile_cli",
    "has_reconciler",
    "inject_marker_into_body",
    "inject_marker_into_gh_args",
    "inject_marker_into_json_body",
    "inspect_git_push",
    "parse_summary_meta",
    "reconcile_action",
    "validate_marker_token",
]


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """Full result of attempting to reconcile a single journaled action."""

    record: ActionRecord
    result: ReconcileResult
    reconciler_name: str | None
    updated_record: ActionRecord | None = None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.record.action_id,
            "actor": self.record.actor,
            "target": self.record.target,
            "tool_name": self.record.tool_name,
            "previous_status": self.record.status.value,
            "current_status": (
                self.updated_record.status.value
                if self.updated_record is not None
                else self.record.status.value
            ),
            "reconciler": self.reconciler_name,
            "applied": self.applied,
            "result": self.result.to_dict(),
        }


def default_reconcilers(
    *,
    github_api_url: str | None = None,
    github_token: str | None = None,
    timeout_seconds: float = 15.0,
) -> list[Reconciler]:
    """Construct the default ordered list of built-in read-only reconcilers."""
    return [
        GitHubReconciler(
            api_base_url=github_api_url,
            token=github_token,
            timeout_seconds=timeout_seconds,
        ),
        GitPushReconciler(timeout_seconds=timeout_seconds),
    ]


def has_reconciler(record: ActionRecord, reconcilers: Sequence[Reconciler] | None = None) -> bool:
    """Return True when a built-in reconciler can verify ``record`` against its remote."""
    adapters = list(reconcilers) if reconcilers is not None else default_reconcilers()
    for adapter in adapters:
        try:
            if adapter.can_reconcile(record):
                return True
        except Exception:
            continue
    return False


def _record_age_seconds(record: ActionRecord, *, now: datetime | None = None) -> float | None:
    """Return elapsed seconds since ``record.updated_at`` (or ``created_at``), or None."""
    raw = record.updated_at or record.created_at
    if not raw:
        return None
    try:
        ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    ref = now if now is not None else datetime.now(UTC)
    return max(0.0, (ref - ts).total_seconds())


def reconcile_action(
    journal: Journal,
    record: ActionRecord,
    *,
    reconcilers: Sequence[Reconciler] | None = None,
    dry_run: bool = False,
    mark_not_found_failed: bool = True,
    mark_pending_not_found_failed: bool = False,
) -> ReconcileOutcome:
    """Reconcile one ``pending`` or ``unknown`` action against external systems.

    Safety invariants:
      - Uses attempt-fenced journal transitions so a concurrent worker or newer attempt
        cannot be clobbered.
      - Never marks an action ``failed`` unless the adapter explicitly returned
        ``ReconcileState.NOT_FOUND`` AND ``mark_not_found_failed`` is True.
      - Never marks a ``pending`` action ``failed`` on ``NOT_FOUND`` unless the caller
        explicitly opts in via ``mark_pending_not_found_failed=True`` (because a live
        worker may still be in flight and about to commit the side effect!).
    """
    if record.status not in (ActionStatus.PENDING, ActionStatus.UNKNOWN):
        return ReconcileOutcome(
            record=record,
            result=ReconcileResult(
                state=ReconcileState.FOUND
                if record.status in (ActionStatus.CONFIRMED, ActionStatus.RECONCILED)
                else ReconcileState.NOT_FOUND,
                result_ref=record.result_ref,
                details=f"Action already in terminal state {record.status.value!r}",
            ),
            reconciler_name=None,
            updated_record=record,
            applied=False,
        )

    adapters = list(reconcilers) if reconcilers is not None else default_reconcilers()
    chosen: Reconciler | None = None
    for adapter in adapters:
        try:
            if adapter.can_reconcile(record):
                chosen = adapter
                break
        except Exception:
            continue

    if chosen is None:
        return ReconcileOutcome(
            record=record,
            result=ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=f"No reconciler available for tool {record.tool_name!r}",
            ),
            reconciler_name=None,
            updated_record=None,
            applied=False,
        )

    try:
        result = chosen.reconcile(record)
    except Exception as exc:
        result = ReconcileResult(
            state=ReconcileState.UNKNOWN,
            details=f"Reconciler {chosen.name!r} raised unexpected error: {exc}",
        )

    if dry_run:
        return ReconcileOutcome(
            record=record,
            result=result,
            reconciler_name=chosen.name,
            updated_record=None,
            applied=False,
        )

    if result.state == ReconcileState.FOUND:
        try:
            updated = journal.mark_reconciled(
                record.actor,
                record.target,
                record.action_id,
                attempt=record.attempt,
                result_ref=result.result_ref,
            )
            return ReconcileOutcome(
                record=record,
                result=result,
                reconciler_name=chosen.name,
                updated_record=updated,
                applied=True,
            )
        except (StaleAttemptError, InvalidStateTransitionError):
            latest = journal.get_action(record.actor, record.target, record.action_id)
            return ReconcileOutcome(
                record=record,
                result=result,
                reconciler_name=chosen.name,
                updated_record=latest,
                applied=False,
            )

    if result.state == ReconcileState.NOT_FOUND:
        can_mark_failed = mark_not_found_failed and (
            record.status == ActionStatus.UNKNOWN or mark_pending_not_found_failed
        )
        if not can_mark_failed:
            safe_result = ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=(
                    "Action is still pending (possibly in-flight); left pending despite "
                    "remote not_found (pass --stale-pending-after <secs> or --force-pending "
                    "once the worker has terminated)"
                ),
            )
            return ReconcileOutcome(
                record=record,
                result=safe_result,
                reconciler_name=chosen.name,
                updated_record=None,
                applied=False,
            )
        try:
            updated = journal.mark_failed(
                record.actor,
                record.target,
                record.action_id,
                attempt=record.attempt,
                error=result.details or "Reconciler confirmed action did not land",
            )
            return ReconcileOutcome(
                record=record,
                result=result,
                reconciler_name=chosen.name,
                updated_record=updated,
                applied=True,
            )
        except (StaleAttemptError, InvalidStateTransitionError):
            latest = journal.get_action(record.actor, record.target, record.action_id)
            return ReconcileOutcome(
                record=record,
                result=result,
                reconciler_name=chosen.name,
                updated_record=latest,
                applied=False,
            )

    return ReconcileOutcome(
        record=record,
        result=result,
        reconciler_name=chosen.name,
        updated_record=None,
        applied=False,
    )


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {raw!r}") from exc
    if value <= 0 or value != value:  # reject <= 0 and NaN
        raise argparse.ArgumentTypeError("value must be > 0")
    return value


def _non_negative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {raw!r}") from exc
    if value < 0 or value != value:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return value


def _build_reconcile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx reconcile",
        description="Reconcile pending or unknown journaled actions against external systems.",
    )
    parser.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    parser.add_argument("--session-id", dest="session_id", default=None, help="Filter by session.")
    parser.add_argument("--action-id", dest="action_id", default=None, help="Specific action_id.")
    parser.add_argument("--actor", dest="actor", default=None, help="Filter by actor.")
    parser.add_argument("--target", dest="target", default=None, help="Filter by target.")
    parser.add_argument(
        "--all",
        dest="reconcile_all",
        action="store_true",
        help="Reconcile all pending and unknown actions across all sessions.",
    )
    parser.add_argument(
        "--stale-pending-after",
        dest="stale_pending_after",
        type=_non_negative_float,
        default=None,
        help=(
            "Allow marking a PENDING row failed on NOT_FOUND only if it has been "
            "pending for at least this many seconds."
        ),
    )
    parser.add_argument(
        "--force-pending",
        dest="force_pending",
        action="store_true",
        help=(
            "Allow marking PENDING rows failed on NOT_FOUND immediately "
            "(only use once you are certain no worker is still in flight)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Query external systems without modifying the journal.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--github-api-url",
        dest="github_api_url",
        default=None,
        help="Override GitHub REST API base URL (or set SIDEFX_GITHUB_API_URL).",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout_seconds",
        type=_positive_float,
        default=15.0,
        help="Timeout in seconds for external reconciliation requests (default: 15).",
    )
    return parser


def handle_reconcile_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx reconcile``."""
    parser = _build_reconcile_parser()
    sub_args = parser.parse_args(list(getattr(args, "args", [])))

    effective_config = load_config(sub_args.config_path) if sub_args.config_path else config
    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    session_id = (
        None
        if sub_args.reconcile_all
        else (sub_args.session_id or getattr(args, "session_id", None))
    )
    db_path = resolve_db_path(db_raw, config=effective_config)

    if not db_path.exists():
        if sub_args.action_id:
            print(
                f"error: journal database not found: {sanitize_text(str(db_path))}", file=sys.stderr
            )
            return 1
        if sub_args.as_json:
            print(
                json.dumps(
                    {"db_path": str(db_path), "journal_exists": False, "outcomes": []},
                    indent=2,
                )
            )
        else:
            print("No journal database found (0 unresolved actions).")
        return 0

    reconcilers = default_reconcilers(
        github_api_url=sub_args.github_api_url,
        timeout_seconds=sub_args.timeout_seconds,
    )

    journal_ctx = Journal.open_readonly(db_path) if sub_args.dry_run else Journal(db_path)
    now_utc = datetime.now(UTC)
    with journal_ctx as journal:
        candidates = journal.list_actions(
            session_id=session_id,
            actor=sub_args.actor,
            target=sub_args.target,
            status=(ActionStatus.PENDING, ActionStatus.UNKNOWN) if not sub_args.action_id else None,
            descending=False,
        )
        if sub_args.action_id is not None:
            candidates = [rec for rec in candidates if rec.action_id == sub_args.action_id]
            if not candidates:
                print(
                    f"error: action_id not found: {sanitize_text(sub_args.action_id)}",
                    file=sys.stderr,
                )
                return 1

        outcomes: list[ReconcileOutcome] = []
        for rec in candidates:
            allow_pending_fail = False
            if rec.status == ActionStatus.PENDING:
                if sub_args.force_pending:
                    allow_pending_fail = True
                elif sub_args.stale_pending_after is not None:
                    age = _record_age_seconds(rec, now=now_utc)
                    if age is not None and age >= sub_args.stale_pending_after:
                        allow_pending_fail = True

            outcomes.append(
                reconcile_action(
                    journal,
                    rec,
                    reconcilers=reconcilers,
                    dry_run=sub_args.dry_run,
                    mark_not_found_failed=True,
                    mark_pending_not_found_failed=allow_pending_fail,
                )
            )

    has_unknown = any(o.result.state == ReconcileState.UNKNOWN for o in outcomes)

    if sub_args.as_json:
        payload = {
            "db_path": str(db_path),
            "journal_exists": True,
            "dry_run": sub_args.dry_run,
            "total": len(outcomes),
            "outcomes": [o.to_dict() for o in outcomes],
        }
        print(json.dumps(payload, indent=2))
        return 1 if has_unknown else 0

    if not outcomes:
        print("No unresolved actions to reconcile.")
        return 0

    for outcome in outcomes:
        rec = outcome.record
        res = outcome.result
        adapter = sanitize_text(outcome.reconciler_name or "none")
        mode = " (dry-run)" if sub_args.dry_run else ""
        ref_part = f" -> {sanitize_text(res.result_ref)}" if res.result_ref else ""
        detail_part = f" | {sanitize_text(res.details)}" if res.details else ""
        print(
            f"[{res.state.value:<9}] {sanitize_text(rec.action_id)} "
            f"({sanitize_text(rec.tool_name)} target={sanitize_text(rec.target)}) "
            f"via {adapter}{mode}{ref_part}{detail_part}"
        )

    unchecked = [
        o.record.action_id
        for o in outcomes
        if o.reconciler_name is None and not o.applied and o.result.state == ReconcileState.UNKNOWN
    ]
    if unchecked:
        example = unchecked[0]
        print(
            "\nNo reconciler can check "
            f"{len(unchecked)} action(s). Verify each by hand, then record the outcome, e.g.:\n"
            f"  {resolve_command(example, landed=True, db_path=db_raw)} --ref <url-or-id>\n"
            f"  {resolve_command(example, landed=False, db_path=db_raw)}"
        )

    return 1 if has_unknown else 0
