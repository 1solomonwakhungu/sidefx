"""Core reconciler protocol, result types, and action metadata helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sidefx.journal import ActionRecord

_META_SEPARATOR = " | sidefx_meta="
_SAFE_MARKER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_INVISIBLE_AND_BIDI_RE = re.compile(
    r"[\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)


def sanitize_text(text: str) -> str:
    """Strip ANSI escapes, control chars, line separators, bidi, and tag chars."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    stripped = _INVISIBLE_AND_BIDI_RE.sub("", stripped)
    return _CONTROL_CHARS_RE.sub(" ", stripped).strip()


class ReconcileState(StrEnum):
    """Outcome of querying an external system to verify whether an action landed."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Result returned by a reconciler adapter.

    Safety contract:
      - ``FOUND`` MUST only be returned when an exact external match is confirmed.
      - ``NOT_FOUND`` MUST only be returned when an exhaustive, un-truncated, error-free
        check proves the action did not land.
      - ``UNKNOWN`` MUST be returned on any network/HTTP/auth/rate-limit/parse error,
        truncated pagination, missing preconditions, or ambiguous matches.
    """

    state: ReconcileState
    result_ref: str | None = None
    summary: str | None = None
    details: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReconcileState):
            raise TypeError(f"state must be a ReconcileState, got {type(self.state).__name__}")
        for field_name in ("result_ref", "summary", "details"):
            val = getattr(self, field_name)
            if val is not None:
                if not isinstance(val, str):
                    raise TypeError(f"{field_name} must be a string or None")
                if "\x00" in val:
                    raise ValueError(f"{field_name} must not contain NUL bytes")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary representation."""
        return {
            "state": self.state.value,
            "result_ref": self.result_ref,
            "summary": self.summary,
            "details": self.details,
        }


@runtime_checkable
class Reconciler(Protocol):
    """Protocol implemented by read-only external-system reconciler adapters."""

    @property
    def name(self) -> str:
        """Short identifier for this reconciler adapter."""
        ...

    def can_reconcile(self, record: ActionRecord) -> bool:
        """Return True if this adapter knows how to reconcile ``record``."""
        ...

    def reconcile(self, record: ActionRecord) -> ReconcileResult:
        """Query the external system read-only and report FOUND, NOT_FOUND, or UNKNOWN."""
        ...


def validate_marker_token(marker: str) -> str:
    """Validate that a marker token is safe to embed in comments and queries."""
    if not isinstance(marker, str) or not _SAFE_MARKER_RE.match(marker):
        raise ValueError(
            "marker must be 1-128 ASCII characters matching [A-Za-z0-9._-] without whitespace"
        )
    return marker


def format_summary_with_meta(display: str, meta: dict[str, Any] | None = None) -> str:
    """Encode a human-readable summary plus compact structured reconciler metadata."""
    clean_display = display.replace("\x00", "").strip()
    if not meta:
        return clean_display
    encoded = json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if clean_display:
        return f"{clean_display}{_META_SEPARATOR}{encoded}"
    return f"{_META_SEPARATOR.lstrip()}{encoded}"


def parse_summary_meta(summary: str | None) -> tuple[str, dict[str, Any]]:
    """Split a journal ``summary`` into ``(display_text, metadata_dict)``.

    Supports both ``"<display> | sidefx_meta=<json>"`` and plain JSON object summaries.
    Never raises on malformed or arbitrary user summaries.
    """
    if not summary:
        return "", {}

    idx = summary.rfind(_META_SEPARATOR)
    if idx != -1:
        display = summary[:idx].strip()
        raw_json = summary[idx + len(_META_SEPARATOR) :].strip()
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return display, parsed
        except (ValueError, RecursionError):
            pass

    stripped = summary.strip()
    if stripped.startswith("sidefx_meta="):
        raw_json = stripped[len("sidefx_meta=") :].strip()
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return "", parsed
        except (ValueError, RecursionError):
            pass

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                display_val = parsed.get("display")
                display_str = display_val if isinstance(display_val, str) else ""
                return display_str, parsed
        except (ValueError, RecursionError):
            pass

    return stripped, {}
