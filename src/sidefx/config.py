"""Configuration loader and schema validator for `.sidefx.json`."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

CONFIG_FILENAME = ".sidefx.json"
ENV_CONFIG_PATH = "SIDEFX_CONFIG"
SUPPORTED_CONFIG_VERSION = 1
MAX_TIMEOUT_SECONDS = 86_400.0

DEFAULT_READ_PREFIXES: tuple[str, ...] = (
    "get_",
    "list_",
    "read_",
    "search_",
    "describe_",
    "fetch_",
    "show_",
    "find_",
)

DEFAULT_MUTATE_PREFIXES: tuple[str, ...] = (
    "create_",
    "update_",
    "delete_",
    "remove_",
    "post_",
    "send_",
    "push_",
    "merge_",
    "refund_",
    "charge_",
    "write_",
    "put_",
    "patch_",
    "close_",
    "reopen_",
    "comment_",
)

_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "journal_path",
        "default_actor",
        "default_session_id",
        "default_timeout_seconds",
        "tools",
        "preflight",
    }
)
_ALLOWED_TOOLS_KEYS = frozenset(
    {"read", "mutate", "read_prefixes", "mutate_prefixes", "unknown_tool_policy"}
)
_ALLOWED_PREFLIGHT_KEYS = frozenset({"rules"})
_ALLOWED_RULE_KEYS = frozenset(
    {
        "tool",
        "arg",
        "required",
        "max",
        "min",
        "allow",
        "deny",
        "allow_domains",
        "deny_domains",
        "reason",
    }
)


class ConfigError(Exception):
    """Raised when a `.sidefx.json` file cannot be read, parsed, or validated."""


class UnknownToolPolicy(StrEnum):
    """Policy applied to tools not matched by explicit lists or prefixes."""

    MUTATE = "mutate"
    READ = "read"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolClassificationConfig:
    """Configuration for classifying tool calls as read-only vs mutating."""

    read_tools: tuple[str, ...] = ()
    mutate_tools: tuple[str, ...] = ()
    read_prefixes: tuple[str, ...] = DEFAULT_READ_PREFIXES
    mutate_prefixes: tuple[str, ...] = DEFAULT_MUTATE_PREFIXES
    unknown_tool_policy: UnknownToolPolicy = UnknownToolPolicy.MUTATE


@dataclass(frozen=True, slots=True)
class PreflightRule:
    """Deterministic preflight guardrail rule evaluated before dispatch."""

    tool: str
    arg: str
    required: bool = False
    max_value: int | float | None = None
    min_value: int | float | None = None
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    allow_domains: tuple[str, ...] = ()
    deny_domains: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize rule back to `.sidefx.json` dictionary shape."""
        data: dict[str, Any] = {"tool": self.tool, "arg": self.arg}
        if self.required:
            data["required"] = True
        if self.min_value is not None:
            data["min"] = self.min_value
        if self.max_value is not None:
            data["max"] = self.max_value
        if self.allow:
            data["allow"] = list(self.allow)
        if self.deny:
            data["deny"] = list(self.deny)
        if self.allow_domains:
            data["allow_domains"] = list(self.allow_domains)
        if self.deny_domains:
            data["deny_domains"] = list(self.deny_domains)
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True, slots=True)
class PreflightConfig:
    """Collection of deterministic preflight rules."""

    rules: tuple[PreflightRule, ...] = ()


@dataclass(frozen=True, slots=True)
class SidefxConfig:
    """Validated runtime configuration for sidefx."""

    version: int = SUPPORTED_CONFIG_VERSION
    journal_path: str | None = None
    default_actor: str = "agent"
    default_session_id: str | None = None
    default_timeout_seconds: float = 30.0
    tools: ToolClassificationConfig = field(default_factory=ToolClassificationConfig)
    preflight: PreflightConfig = field(default_factory=PreflightConfig)
    source_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to a `.sidefx.json`-compatible dict."""
        data: dict[str, Any] = {
            "version": self.version,
            "default_actor": self.default_actor,
            "default_timeout_seconds": self.default_timeout_seconds,
            "tools": {
                "read": list(self.tools.read_tools),
                "mutate": list(self.tools.mutate_tools),
                "read_prefixes": list(self.tools.read_prefixes),
                "mutate_prefixes": list(self.tools.mutate_prefixes),
                "unknown_tool_policy": self.tools.unknown_tool_policy.value,
            },
            "preflight": {
                "rules": [rule.to_dict() for rule in self.preflight.rules],
            },
        }
        if self.journal_path is not None:
            data["journal_path"] = self.journal_path
        if self.default_session_id is not None:
            data["default_session_id"] = self.default_session_id
        return data


def default_config_dict() -> dict[str, Any]:
    """Return a clean starter `.sidefx.json` dictionary for `sidefx init`."""
    return {
        "version": SUPPORTED_CONFIG_VERSION,
        "journal_path": ".sidefx/journal.db",
        "default_actor": "agent",
        "default_timeout_seconds": 30.0,
        "tools": {
            "read": [],
            "mutate": [],
            "read_prefixes": list(DEFAULT_READ_PREFIXES),
            "mutate_prefixes": list(DEFAULT_MUTATE_PREFIXES),
            "unknown_tool_policy": UnknownToolPolicy.MUTATE.value,
        },
        "preflight": {
            "rules": [],
        },
    }


def _is_owned_by_current_user(path: Path) -> bool:
    """Return True if path is owned by current uid on POSIX (always True if getuid absent)."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return True
    try:
        return path.stat().st_uid == getuid()
    except OSError:
        return False


def discover_config_path(cwd: Path | None = None) -> Path | None:
    """Search upward from ``cwd`` for ``.sidefx.json`` within safe trust boundaries.

    Stops walking upward after inspecting a git repository root (``.git`` present),
    after inspecting the user's home directory, or before entering an ancestor directory
    owned by a different user on POSIX (preventing untrusted configs planted in ``/tmp``
    or ``/`` from altering policy).
    """
    start_raw = cwd if cwd is not None else Path.cwd()
    start = Path(os.path.abspath(start_raw))
    current = start if start.is_dir() else start.parent
    try:
        home_dir = Path(os.path.abspath(Path.home()))
    except RuntimeError:
        home_dir = None

    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file() and _is_owned_by_current_user(candidate):
            return candidate

        # Stop after checking repository root or user home directory.
        if (current / ".git").exists():
            return None
        if home_dir is not None and current == home_dir:
            return None

        parent = current.parent
        if parent == current:
            return None

        # Stop before crossing into directories owned by another user (e.g. /tmp or /).
        if not _is_owned_by_current_user(parent):
            return None
        current = parent


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_config(
    path: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> SidefxConfig:
    """Load and validate a `.sidefx.json` configuration.

    Resolution precedence:
      1. Explicit ``path`` argument (must exist).
      2. ``SIDEFX_CONFIG`` environment variable (must exist).
      3. Nearest trusted ``.sidefx.json`` searching upward from ``cwd``.
      4. Default ``SidefxConfig()`` if no config file is found.

    ``source_path`` preserves the logical path (without resolving symlinks) so relative
    ``journal_path`` values stay anchored to the project directory rather than a shared
    symlink target directory.
    """
    resolved: Path | None = None
    explicit = False

    if path is not None:
        raw_path = str(path).strip()
        if not raw_path:
            raise ConfigError("Config path must not be empty")
        resolved = Path(raw_path).expanduser()
        explicit = True
    else:
        env_path = os.environ.get(ENV_CONFIG_PATH, "").strip()
        if env_path:
            resolved = Path(env_path).expanduser()
            explicit = True
        else:
            resolved = discover_config_path(cwd=cwd)

    if resolved is None:
        return SidefxConfig()

    if not resolved.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {resolved}")
        return SidefxConfig()

    if resolved.is_dir():
        raise ConfigError(f"Config path is a directory, expected a JSON file: {resolved}")

    try:
        raw_text = resolved.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Failed to read config file {resolved}: {exc}") from exc

    try:
        parsed = json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except ConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Malformed JSON in {resolved} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise ConfigError(f"Invalid JSON structure in {resolved}: {exc}") from exc

    logical_path = Path(os.path.abspath(resolved))
    return _parse_config_dict(parsed, source_path=logical_path)


def _reject_unknown_keys(
    mapping: dict[str, Any],
    allowed: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(set(mapping.keys()) - allowed)
    if unknown:
        keys = ", ".join(repr(k) for k in unknown)
        raise ConfigError(f"Unknown key(s) in {context}: {keys}")


def _expect_non_empty_str(
    value: Any,
    field_name: str,
    *,
    reject_surrounding_whitespace: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise ConfigError(f"{field_name} must not contain NUL bytes")
    if reject_surrounding_whitespace and value != value.strip():
        raise ConfigError(f"{field_name} must not have leading or trailing whitespace")
    return value


def _expect_str_tuple(
    value: Any,
    field_name: str,
    *,
    non_empty: bool = False,
    reject_surrounding_whitespace: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list of non-empty strings")
    if non_empty and not value:
        raise ConfigError(f"{field_name} must not be empty when specified")
    result: list[str] = []
    for idx, item in enumerate(value):
        result.append(
            _expect_non_empty_str(
                item,
                f"{field_name}[{idx}]",
                reject_surrounding_whitespace=reject_surrounding_whitespace,
            )
        )
    return tuple(result)


def _expect_finite_number(value: Any, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name} must be a finite number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{field_name} must be a finite number")
        return value
    # Reject arbitrarily large ints that overflow IEEE-754 floats
    try:
        if not math.isfinite(float(value)):
            raise ConfigError(f"{field_name} is out of finite numeric range")
    except OverflowError as exc:
        raise ConfigError(f"{field_name} is out of finite numeric range") from exc
    return value


def _parse_tools_config(raw: Any) -> ToolClassificationConfig:
    if not isinstance(raw, dict):
        raise ConfigError("'tools' must be a JSON object")
    _reject_unknown_keys(raw, _ALLOWED_TOOLS_KEYS, "'tools'")

    read_tools = (
        _expect_str_tuple(raw["read"], "tools.read", reject_surrounding_whitespace=True)
        if "read" in raw
        else ()
    )
    mutate_tools = (
        _expect_str_tuple(raw["mutate"], "tools.mutate", reject_surrounding_whitespace=True)
        if "mutate" in raw
        else ()
    )

    overlap = sorted(set(read_tools) & set(mutate_tools))
    if overlap:
        names = ", ".join(repr(n) for n in overlap)
        raise ConfigError(f"Tool(s) cannot appear in both tools.read and tools.mutate: {names}")

    read_prefixes = (
        _expect_str_tuple(
            raw["read_prefixes"], "tools.read_prefixes", reject_surrounding_whitespace=True
        )
        if "read_prefixes" in raw
        else DEFAULT_READ_PREFIXES
    )
    mutate_prefixes = (
        _expect_str_tuple(
            raw["mutate_prefixes"], "tools.mutate_prefixes", reject_surrounding_whitespace=True
        )
        if "mutate_prefixes" in raw
        else DEFAULT_MUTATE_PREFIXES
    )

    prefix_overlap = sorted(set(read_prefixes) & set(mutate_prefixes))
    if prefix_overlap:
        names = ", ".join(repr(n) for n in prefix_overlap)
        raise ConfigError(
            f"Prefix(es) cannot appear in both tools.read_prefixes and "
            f"tools.mutate_prefixes: {names}"
        )

    if "unknown_tool_policy" in raw:
        raw_policy = raw["unknown_tool_policy"]
        if not isinstance(raw_policy, str):
            raise ConfigError("tools.unknown_tool_policy must be a string")
        try:
            unknown_policy = UnknownToolPolicy(raw_policy)
        except ValueError as exc:
            valid = ", ".join(p.value for p in UnknownToolPolicy)
            raise ConfigError(
                f"Invalid tools.unknown_tool_policy {raw_policy!r}; expected one of: {valid}"
            ) from exc
    else:
        unknown_policy = UnknownToolPolicy.MUTATE

    return ToolClassificationConfig(
        read_tools=read_tools,
        mutate_tools=mutate_tools,
        read_prefixes=read_prefixes,
        mutate_prefixes=mutate_prefixes,
        unknown_tool_policy=unknown_policy,
    )


def _parse_preflight_rule(raw: Any, index: int) -> PreflightRule:
    ctx = f"preflight.rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx} must be a JSON object")
    _reject_unknown_keys(raw, _ALLOWED_RULE_KEYS, ctx)

    tool = (
        _expect_non_empty_str(raw["tool"], f"{ctx}.tool", reject_surrounding_whitespace=True)
        if "tool" in raw
        else "*"
    )
    if "arg" not in raw:
        raise ConfigError(f"{ctx}.arg is required")
    arg = _expect_non_empty_str(raw["arg"], f"{ctx}.arg", reject_surrounding_whitespace=True)

    required = False
    if "required" in raw:
        raw_required = raw["required"]
        if not isinstance(raw_required, bool):
            raise ConfigError(f"{ctx}.required must be a boolean")
        required = raw_required

    max_value = _expect_finite_number(raw["max"], f"{ctx}.max") if "max" in raw else None
    min_value = _expect_finite_number(raw["min"], f"{ctx}.min") if "min" in raw else None
    if min_value is not None and max_value is not None and min_value > max_value:
        raise ConfigError(f"{ctx}: min ({min_value}) cannot be greater than max ({max_value})")

    allow = (
        _expect_str_tuple(raw["allow"], f"{ctx}.allow", non_empty=True) if "allow" in raw else ()
    )
    deny = _expect_str_tuple(raw["deny"], f"{ctx}.deny", non_empty=True) if "deny" in raw else ()
    allow_domains = (
        _expect_str_tuple(
            raw["allow_domains"],
            f"{ctx}.allow_domains",
            non_empty=True,
            reject_surrounding_whitespace=True,
        )
        if "allow_domains" in raw
        else ()
    )
    deny_domains = (
        _expect_str_tuple(
            raw["deny_domains"],
            f"{ctx}.deny_domains",
            non_empty=True,
            reject_surrounding_whitespace=True,
        )
        if "deny_domains" in raw
        else ()
    )
    reason = (
        _expect_non_empty_str(raw["reason"], f"{ctx}.reason")
        if "reason" in raw and raw["reason"] is not None
        else None
    )

    has_constraint = any(
        (
            required,
            max_value is not None,
            min_value is not None,
            bool(allow),
            bool(deny),
            bool(allow_domains),
            bool(deny_domains),
        )
    )
    if not has_constraint:
        raise ConfigError(
            f"{ctx} must specify at least one constraint "
            "(required, min, max, allow, deny, allow_domains, or deny_domains)"
        )

    return PreflightRule(
        tool=tool,
        arg=arg,
        required=required,
        max_value=max_value,
        min_value=min_value,
        allow=allow,
        deny=deny,
        allow_domains=allow_domains,
        deny_domains=deny_domains,
        reason=reason,
    )


def _parse_preflight_config(raw: Any) -> PreflightConfig:
    if not isinstance(raw, dict):
        raise ConfigError("'preflight' must be a JSON object")
    _reject_unknown_keys(raw, _ALLOWED_PREFLIGHT_KEYS, "'preflight'")

    if "rules" not in raw:
        return PreflightConfig()

    raw_rules = raw["rules"]
    if not isinstance(raw_rules, list):
        raise ConfigError("preflight.rules must be a list of rule objects")

    rules = tuple(_parse_preflight_rule(item, idx) for idx, item in enumerate(raw_rules))
    return PreflightConfig(rules=rules)


def _parse_config_dict(raw: Any, *, source_path: Path | None) -> SidefxConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a JSON object")
    _reject_unknown_keys(raw, _ALLOWED_TOP_LEVEL_KEYS, "config root")

    version = SUPPORTED_CONFIG_VERSION
    if "version" in raw:
        raw_version = raw["version"]
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ConfigError("version must be an integer")
        if raw_version != SUPPORTED_CONFIG_VERSION:
            raise ConfigError(
                f"Unsupported config version {raw_version}; expected {SUPPORTED_CONFIG_VERSION}"
            )
        version = raw_version

    journal_path: str | None = None
    if "journal_path" in raw and raw["journal_path"] is not None:
        journal_path = _expect_non_empty_str(raw["journal_path"], "journal_path")

    default_actor = "agent"
    if "default_actor" in raw:
        default_actor = _expect_non_empty_str(
            raw["default_actor"], "default_actor", reject_surrounding_whitespace=True
        )

    default_session_id: str | None = None
    if "default_session_id" in raw and raw["default_session_id"] is not None:
        default_session_id = _expect_non_empty_str(
            raw["default_session_id"],
            "default_session_id",
            reject_surrounding_whitespace=True,
        )

    default_timeout_seconds = 30.0
    if "default_timeout_seconds" in raw:
        val = _expect_finite_number(raw["default_timeout_seconds"], "default_timeout_seconds")
        timeout_float = float(val)
        if timeout_float <= 0 or timeout_float > MAX_TIMEOUT_SECONDS:
            raise ConfigError(
                f"default_timeout_seconds must be > 0 and <= {int(MAX_TIMEOUT_SECONDS)}"
            )
        default_timeout_seconds = timeout_float

    tools = _parse_tools_config(raw["tools"]) if "tools" in raw else ToolClassificationConfig()
    preflight = (
        _parse_preflight_config(raw["preflight"]) if "preflight" in raw else PreflightConfig()
    )

    return SidefxConfig(
        version=version,
        journal_path=journal_path,
        default_actor=default_actor,
        default_session_id=default_session_id,
        default_timeout_seconds=default_timeout_seconds,
        tools=tools,
        preflight=preflight,
        source_path=source_path,
    )
