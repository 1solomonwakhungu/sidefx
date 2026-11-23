"""Deterministic tool classification and preflight guardrail evaluator."""

from __future__ import annotations

import fnmatch
import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from sidefx.config import (
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    ToolClassificationConfig,
    UnknownToolPolicy,
)

_GLOB_CHARS = frozenset("*?[")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x1f\x7f\s]")
_DISALLOWED_EMAIL_CHARS = frozenset(",;\\()\"'")


class ToolKind(StrEnum):
    """Classification of an MCP or CLI tool."""

    READ = "read"
    MUTATE = "mutate"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PreflightViolation:
    """Single deterministic preflight policy violation."""

    tool: str
    arg: str
    constraint: str
    message: str
    value: Any = None
    rule_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this violation."""
        data: dict[str, Any] = {
            "tool": self.tool,
            "arg": self.arg,
            "constraint": self.constraint,
            "message": self.message,
        }
        if self.rule_reason is not None:
            data["reason"] = self.rule_reason
        return data


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Aggregate result of classifying a tool and evaluating preflight rules."""

    allowed: bool
    tool_kind: ToolKind
    violations: tuple[PreflightViolation, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        """Human-readable violation messages."""
        return tuple(v.message for v in self.violations)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of this preflight evaluation."""
        return {
            "allowed": self.allowed,
            "tool_kind": self.tool_kind.value,
            "violations": [v.to_dict() for v in self.violations],
        }


def _has_glob(pattern: str) -> bool:
    return any(ch in _GLOB_CHARS for ch in pattern)


def _extract_tools_config(
    config: SidefxConfig | ToolClassificationConfig | None,
) -> ToolClassificationConfig:
    if config is None:
        return ToolClassificationConfig()
    if isinstance(config, SidefxConfig):
        return config.tools
    return config


def _extract_preflight_config(
    config: SidefxConfig | PreflightConfig | None,
) -> PreflightConfig:
    if config is None:
        return PreflightConfig()
    if isinstance(config, SidefxConfig):
        return config.preflight
    return config


def classify_tool(
    tool_name: str,
    config: SidefxConfig | ToolClassificationConfig | None = None,
) -> ToolKind:
    """Classify a tool as ``read``, ``mutate``, or ``deny``.

    Precedence (fail-safe: ``mutate``/``deny`` wins any conflict):
      1. Exact matches in ``mutate_tools`` / ``read_tools`` (mutate wins if in both).
      2. Glob/fnmatch matches in ``mutate_tools`` / ``read_tools`` (mutate wins if in both).
      3. Longest matching prefix in ``mutate_prefixes`` / ``read_prefixes``
         (case-insensitive; mutate wins on equal-length tie).
      4. Fallback to ``unknown_tool_policy``.
    """
    if (
        not isinstance(tool_name, str)
        or not tool_name
        or tool_name != tool_name.strip()
        or _CONTROL_OR_WHITESPACE_RE.search(tool_name) is not None
    ):
        return ToolKind.DENY

    tools_cfg = _extract_tools_config(config)

    # 1. Explicit exact names (mutate takes precedence over read on conflict).
    exact_mutate = {p for p in tools_cfg.mutate_tools if not _has_glob(p)}
    exact_read = {p for p in tools_cfg.read_tools if not _has_glob(p)}
    if tool_name in exact_mutate:
        return ToolKind.MUTATE
    if tool_name in exact_read:
        return ToolKind.READ

    # 2. Explicit glob/fnmatch patterns (mutate takes precedence over read on conflict).
    glob_mutate = [p for p in tools_cfg.mutate_tools if _has_glob(p)]
    glob_read = [p for p in tools_cfg.read_tools if _has_glob(p)]
    if any(fnmatch.fnmatchcase(tool_name, pat) for pat in glob_mutate):
        return ToolKind.MUTATE
    if any(fnmatch.fnmatchcase(tool_name, pat) for pat in glob_read):
        return ToolKind.READ

    # 3. Prefix rules (case-insensitive, longest prefix wins, mutate wins ties).
    lower_name = tool_name.lower()
    best_mutate_len = max(
        (len(p) for p in tools_cfg.mutate_prefixes if p and lower_name.startswith(p.lower())),
        default=0,
    )
    best_read_len = max(
        (len(p) for p in tools_cfg.read_prefixes if p and lower_name.startswith(p.lower())),
        default=0,
    )
    if best_mutate_len > 0 or best_read_len > 0:
        if best_mutate_len >= best_read_len:
            return ToolKind.MUTATE
        return ToolKind.READ

    # 4. Unknown tool policy fallback.
    if tools_cfg.unknown_tool_policy == UnknownToolPolicy.READ:
        return ToolKind.READ
    if tools_cfg.unknown_tool_policy == UnknownToolPolicy.DENY:
        return ToolKind.DENY
    return ToolKind.MUTATE


def _tool_matches_rule(tool_name: str, rule_tool: str) -> bool:
    if rule_tool == "*":
        return True
    if _has_glob(rule_tool):
        return fnmatch.fnmatchcase(tool_name, rule_tool)
    return tool_name == rule_tool


def _resolve_arg_values(arguments: Mapping[str, Any], arg_path: str) -> list[tuple[str, Any]]:
    """Resolve top-level or dotted nested argument values.

    Returns ``(resolved_path, value)`` pairs. Checks both literal key match and
    dotted nested traversal so neither form can bypass a rule. Also traverses into
    lists/tuples of mappings when intermediate segments encounter sequences.
    """
    results: list[tuple[str, Any]] = []
    if "." not in arg_path:
        if arg_path in arguments:
            results.append((arg_path, arguments[arg_path]))
        return results

    # Also check exact literal key containing dots in case an upstream uses flat dotted keys.
    if arg_path in arguments:
        results.append((arg_path, arguments[arg_path]))

    segments = arg_path.split(".")
    if any(not seg for seg in segments):
        return results

    def _walk(current: Any, idx: int, prefix: str) -> None:
        if isinstance(current, (list, tuple)):
            for item_idx, item in enumerate(current):
                _walk(item, idx, f"{prefix}[{item_idx}]")
            return
        if not isinstance(current, Mapping):
            return
        seg = segments[idx]
        if seg not in current:
            return
        next_path = f"{prefix}.{seg}" if prefix else seg
        val = current[seg]
        if idx == len(segments) - 1:
            results.append((next_path, val))
        else:
            _walk(val, idx + 1, next_path)

    _walk(arguments, 0, "")
    return results


def _iter_scalar_values(path: str, value: Any) -> list[tuple[str, Any]]:
    """Flatten list/tuple values so every item in a collection is validated."""
    if isinstance(value, (list, tuple)):
        items: list[tuple[str, Any]] = []
        for idx, elem in enumerate(value):
            items.extend(_iter_scalar_values(f"{path}[{idx}]", elem))
        return items
    return [(path, value)]


def _normalize_host(host: str) -> str | None:
    """Normalize and validate a hostname, domain, or IP literal."""
    if not host or _CONTROL_OR_WHITESPACE_RE.search(host) is not None:
        return None
    raw = host.lower()
    # Reject multiple trailing dots (e.g. "example.com..") while normalizing single FQDN dot.
    if raw.endswith("."):
        raw = raw[:-1]
        if not raw or raw.endswith("."):
            return None

    # Handle bracketed or bare IP literals (e.g. ::1 or 127.0.0.1).
    ip_candidate = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    try:
        ip_obj = ipaddress.ip_address(ip_candidate)
        return str(ip_obj)
    except ValueError:
        pass

    try:
        ascii_host = raw.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None

    if not ascii_host or len(ascii_host) > 253:
        return None
    labels = ascii_host.split(".")
    if any(not _DNS_LABEL_RE.match(label) for label in labels):
        return None
    return ascii_host


def extract_domain(raw: str) -> str | None:
    """Extract and normalize a lowercase domain/host from an email, URL, or bare host.

    Returns ``None`` on malformed, ambiguous, or obfuscated inputs (fail-closed):
      - Rejects NUL bytes, control characters, and embedded newlines.
      - Rejects multi-address email strings (``a@good.com, b@evil.com``).
      - Rejects multiple ``@`` symbols in emails (``user@evil.com@good.com``).
      - Rejects backslashes in URL authority/netloc (``https://good.com\\@evil.com``).
      - Extracts the true authority hostname from URLs with userinfo
        (``https://good.com@evil.com/`` -> ``evil.com``) and strips ports.
    """
    if not isinstance(raw, str):
        return None
    if "\x00" in raw or "\r" in raw or "\n" in raw or "\t" in raw:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None

    # Case 1: URL (scheme://... or protocol-relative //...)
    if "://" in trimmed or trimmed.startswith("//"):
        if _CONTROL_OR_WHITESPACE_RE.search(trimmed) is not None:
            return None
        try:
            parsed = urlsplit(trimmed)
        except ValueError:
            return None
        if not parsed.netloc or "\\" in parsed.netloc:
            return None
        # Reject ambiguous multiple '@' inside authority (e.g. user@good.com@evil.com)
        if parsed.netloc.count("@") > 1:
            return None
        # Validate port if present (urlsplit raises ValueError on invalid port access)
        try:
            _ = parsed.port
        except ValueError:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        return _normalize_host(hostname)

    # Case 2: Email address (plain local@domain or RFC 5322 "Display Name <local@domain>")
    if "@" in trimmed:
        if "," in trimmed or ";" in trimmed or "\\" in trimmed:
            return None
        addr = trimmed
        if "<" in trimmed or ">" in trimmed:
            if (
                trimmed.count("<") != 1
                or trimmed.count(">") != 1
                or not trimmed.endswith(">")
                or trimmed.index("<") >= trimmed.index(">") - 1
            ):
                return None
            display_part, bracketed = trimmed.split("<", 1)
            # Disallow '@' in display name to defeat display-name spoofing.
            if "@" in display_part:
                return None
            addr = bracketed[:-1].strip()
        if not addr or _CONTROL_OR_WHITESPACE_RE.search(addr) is not None:
            return None
        if any(ch in _DISALLOWED_EMAIL_CHARS for ch in addr):
            return None
        if addr.count("@") != 1:
            return None
        local, domain = addr.split("@", 1)
        if not local or not domain:
            return None
        if local.startswith(".") or local.endswith(".") or ".." in local:
            return None
        # Reject port/path suffixes attached to email domain.
        if ":" in domain or "/" in domain or "?" in domain or "#" in domain:
            return None
        return _normalize_host(domain)

    # Case 3: Bare hostname / domain (with optional :port)
    if _CONTROL_OR_WHITESPACE_RE.search(trimmed) is not None or any(
        ch in trimmed for ch in ("/", "?", "#", "\\", ",", ";", "<", ">")
    ):
        return None
    host_part = trimmed
    if trimmed.startswith("["):
        # Bracketed IPv6 literal with optional :port
        end_bracket = trimmed.find("]")
        if end_bracket == -1:
            return None
        host_part = trimmed[: end_bracket + 1]
        rest = trimmed[end_bracket + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
            port_num = int(rest[1:])
            if not (1 <= port_num <= 65535):
                return None
    elif ":" in trimmed:
        if trimmed.count(":") > 1:
            # Possibly bare IPv6 literal
            return _normalize_host(trimmed)
        host_part, port_str = trimmed.split(":", 1)
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            return None
    return _normalize_host(host_part)


def _domain_matches(domain: str, pattern: str, *, match_subdomains_by_default: bool) -> bool:
    """Match a normalized domain against a domain rule pattern."""
    raw_pat = pattern.strip().lower()
    if not raw_pat:
        return False
    if raw_pat == "*":
        return True

    if raw_pat.startswith("*."):
        suffix = _normalize_host(raw_pat[2:])
        if suffix is None:
            return False
        return domain.endswith(f".{suffix}")

    if raw_pat.startswith("."):
        suffix = _normalize_host(raw_pat[1:])
        if suffix is None:
            return False
        return domain.endswith(f".{suffix}")

    normalized_pat = _normalize_host(raw_pat)
    if normalized_pat is None:
        return False
    if domain == normalized_pat:
        return True
    if match_subdomains_by_default:
        return domain.endswith(f".{normalized_pat}")
    return False


def _string_matches_pattern(value: str, pattern: str) -> bool:
    if _has_glob(pattern):
        return fnmatch.fnmatchcase(value, pattern)
    return value == pattern


def _format_violation_message(base: str, rule_reason: str | None) -> str:
    if rule_reason:
        return f"{base} ({rule_reason})"
    return base


def _evaluate_rule_value(
    *,
    tool_name: str,
    rule: PreflightRule,
    resolved_path: str,
    value: Any,
    violations: list[PreflightViolation],
) -> None:
    # Numeric constraints (min / max)
    if rule.min_value is not None or rule.max_value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            violations.append(
                PreflightViolation(
                    tool=tool_name,
                    arg=resolved_path,
                    constraint="invalid_type",
                    value=value,
                    rule_reason=rule.reason,
                    message=_format_violation_message(
                        f"Argument {resolved_path!r} must be a finite number, "
                        f"got {type(value).__name__}",
                        rule.reason,
                    ),
                )
            )
        elif isinstance(value, float) and not math.isfinite(value):
            violations.append(
                PreflightViolation(
                    tool=tool_name,
                    arg=resolved_path,
                    constraint="invalid_type",
                    value=value,
                    rule_reason=rule.reason,
                    message=_format_violation_message(
                        f"Argument {resolved_path!r} must be a finite number, got {value!r}",
                        rule.reason,
                    ),
                )
            )
        else:
            if rule.min_value is not None and value < rule.min_value:
                violations.append(
                    PreflightViolation(
                        tool=tool_name,
                        arg=resolved_path,
                        constraint="min",
                        value=value,
                        rule_reason=rule.reason,
                        message=_format_violation_message(
                            f"Argument {resolved_path!r} value {value!r} is below "
                            f"minimum {rule.min_value!r}",
                            rule.reason,
                        ),
                    )
                )
            if rule.max_value is not None and value > rule.max_value:
                violations.append(
                    PreflightViolation(
                        tool=tool_name,
                        arg=resolved_path,
                        constraint="max",
                        value=value,
                        rule_reason=rule.reason,
                        message=_format_violation_message(
                            f"Argument {resolved_path!r} value {value!r} exceeds "
                            f"maximum {rule.max_value!r}",
                            rule.reason,
                        ),
                    )
                )

    # String allowlist / denylist constraints
    if rule.allow or rule.deny:
        if not isinstance(value, str):
            violations.append(
                PreflightViolation(
                    tool=tool_name,
                    arg=resolved_path,
                    constraint="invalid_type",
                    value=value,
                    rule_reason=rule.reason,
                    message=_format_violation_message(
                        f"Argument {resolved_path!r} must be a string, got {type(value).__name__}",
                        rule.reason,
                    ),
                )
            )
        else:
            if rule.deny:
                stripped = value.strip()
                if any(
                    _string_matches_pattern(value, pat)
                    or (stripped != value and _string_matches_pattern(stripped, pat))
                    for pat in rule.deny
                ):
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=resolved_path,
                            constraint="deny",
                            value=value,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Argument {resolved_path!r} value {value!r} is denied by policy",
                                rule.reason,
                            ),
                        )
                    )
            if rule.allow and not any(_string_matches_pattern(value, pat) for pat in rule.allow):
                violations.append(
                    PreflightViolation(
                        tool=tool_name,
                        arg=resolved_path,
                        constraint="allow",
                        value=value,
                        rule_reason=rule.reason,
                        message=_format_violation_message(
                            f"Argument {resolved_path!r} value {value!r} is not in allowlist",
                            rule.reason,
                        ),
                    )
                )

    # Domain allowlist / denylist constraints
    if rule.allow_domains or rule.deny_domains:
        if not isinstance(value, str):
            violations.append(
                PreflightViolation(
                    tool=tool_name,
                    arg=resolved_path,
                    constraint="invalid_type",
                    value=value,
                    rule_reason=rule.reason,
                    message=_format_violation_message(
                        f"Argument {resolved_path!r} must be a string for domain check, "
                        f"got {type(value).__name__}",
                        rule.reason,
                    ),
                )
            )
        else:
            domain = extract_domain(value)
            if domain is None:
                violations.append(
                    PreflightViolation(
                        tool=tool_name,
                        arg=resolved_path,
                        constraint="invalid_domain",
                        value=value,
                        rule_reason=rule.reason,
                        message=_format_violation_message(
                            f"Argument {resolved_path!r} value {value!r} is not a valid "
                            "email address, URL, or domain",
                            rule.reason,
                        ),
                    )
                )
            else:
                if rule.deny_domains and any(
                    _domain_matches(domain, pat, match_subdomains_by_default=True)
                    for pat in rule.deny_domains
                ):
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=resolved_path,
                            constraint="deny_domains",
                            value=domain,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Argument {resolved_path!r} domain {domain!r} is denied by policy",
                                rule.reason,
                            ),
                        )
                    )
                if rule.allow_domains and not any(
                    _domain_matches(domain, pat, match_subdomains_by_default=False)
                    for pat in rule.allow_domains
                ):
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=resolved_path,
                            constraint="allow_domains",
                            value=domain,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Argument {resolved_path!r} domain {domain!r} is not in "
                                "allowed domains",
                                rule.reason,
                            ),
                        )
                    )


def evaluate_preflight(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    config: SidefxConfig | None = None,
) -> PreflightResult:
    """Evaluate tool classification and all matching deterministic preflight rules.

    Always evaluates every matching rule and every item in list/tuple argument values
    so all violations are reported in a single response.
    """
    cfg = config if config is not None else SidefxConfig()
    tool_kind = classify_tool(tool_name, cfg)
    violations: list[PreflightViolation] = []

    if tool_kind == ToolKind.DENY:
        violations.append(
            PreflightViolation(
                tool=str(tool_name),
                arg="*",
                constraint="tool_denied",
                value=tool_name,
                message=f"Tool {tool_name!r} is denied by tool classification policy",
            )
        )

    if arguments is None:
        args_mapping: Mapping[str, Any] = {}
    elif isinstance(arguments, Mapping):
        args_mapping = arguments
    else:
        violations.append(
            PreflightViolation(
                tool=str(tool_name),
                arg="*",
                constraint="invalid_type",
                value=arguments,
                message=(
                    f"Tool arguments must be an object/mapping, got {type(arguments).__name__}"
                ),
            )
        )
        return PreflightResult(
            allowed=False,
            tool_kind=tool_kind,
            violations=tuple(violations),
        )

    preflight_cfg = _extract_preflight_config(cfg)
    if isinstance(tool_name, str) and tool_name:
        for rule in preflight_cfg.rules:
            if not _tool_matches_rule(tool_name, rule.tool):
                continue
            resolved_entries = _resolve_arg_values(args_mapping, rule.arg)
            for resolved_path, raw_val in resolved_entries:
                for scalar_path, scalar_val in _iter_scalar_values(resolved_path, raw_val):
                    _evaluate_rule_value(
                        tool_name=tool_name,
                        rule=rule,
                        resolved_path=scalar_path,
                        value=scalar_val,
                        violations=violations,
                    )

    return PreflightResult(
        allowed=len(violations) == 0,
        tool_kind=tool_kind,
        violations=tuple(violations),
    )
