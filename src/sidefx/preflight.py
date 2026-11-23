"""Deterministic tool classification and preflight guardrail evaluator."""

from __future__ import annotations

import fnmatch
import ipaddress
import math
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import getaddresses
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
_HEX_LITERAL_RE = re.compile(r"^0x[0-9a-f]*$")
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x1f\x7f\s]")
_DISALLOWED_EMAIL_CHARS = frozenset(",;\\()\"'")
_DISALLOWED_EMAIL_LOCAL_CHARS = frozenset("/?#:")
_NAMESPACE_SEPARATORS = ("__", ".", ":", "/")
_COMPOUND_CONNECTORS = ("and_", "or_", "then_")
_COMPOUND_DIRECT_MUTATE_VERBS = ("write", "replace", "modify")
_EXTRA_COMPOUND_MUTATE_VERBS = frozenset(
    {"replace", "modify", "edit", "upsert", "insert", "drop", "destroy", "publish"}
)
_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[/\\])\.\.(?:$|[/\\])")
_LEADING_SLASHES_RE = re.compile(r"^/+")


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


def _unqualified_tool_suffix(tool_name: str) -> str | None:
    """Extract the action suffix from namespaced tool names like `mcp__gh__delete_repo`."""
    best_idx = -1
    best_sep_len = 0
    for sep in _NAMESPACE_SEPARATORS:
        idx = tool_name.rfind(sep)
        if idx > best_idx:
            best_idx = idx
            best_sep_len = len(sep)
    if best_idx == -1:
        return None
    suffix = tool_name[best_idx + best_sep_len :]
    return suffix if suffix else None


def _matches_verb_token(text: str, verbs: frozenset[str]) -> bool:
    return any(text == verb or text.startswith(f"{verb}_") for verb in verbs)


def _classify_by_prefixes(
    lower_name: str,
    tools_cfg: ToolClassificationConfig,
) -> ToolKind | None:
    mutate_prefixes_lower = tuple(p.lower() for p in tools_cfg.mutate_prefixes if p)
    read_prefixes_lower = tuple(p.lower() for p in tools_cfg.read_prefixes if p)

    best_mutate_len = max(
        (len(p) for p in mutate_prefixes_lower if lower_name.startswith(p)),
        default=0,
    )
    matched_read_prefixes = [p for p in read_prefixes_lower if lower_name.startswith(p)]
    best_read_len = max((len(p) for p in matched_read_prefixes), default=0)

    if best_mutate_len > 0 or best_read_len > 0:
        if best_mutate_len >= best_read_len:
            return ToolKind.MUTATE
        # Guard against compound names like `fetch_and_send_email`, `get_or_create_user`,
        # `find_and_replace`, `fetch_then_delete`, `read_modify_write`, `read_write_file`.
        rest = lower_name[best_read_len:]
        mutate_verbs = (
            frozenset(p.rstrip("_") for p in mutate_prefixes_lower if p.rstrip("_"))
            | _EXTRA_COMPOUND_MUTATE_VERBS
        )
        if _matches_verb_token(rest, frozenset(_COMPOUND_DIRECT_MUTATE_VERBS)):
            return ToolKind.MUTATE
        for conn in _COMPOUND_CONNECTORS:
            if rest.startswith(conn) and _matches_verb_token(rest[len(conn) :], mutate_verbs):
                return ToolKind.MUTATE
        return ToolKind.READ

    return None


def classify_tool(
    tool_name: str,
    config: SidefxConfig | ToolClassificationConfig | None = None,
) -> ToolKind:
    """Classify a tool as ``read``, ``mutate``, or ``deny``.

    Precedence (explicit lists override prefix heuristics; ``mutate`` wins any conflict
    at the same precedence level):
      1. Exact matches in ``mutate_tools`` / ``read_tools`` (mutate wins if in both).
      2. Glob/fnmatch matches in ``mutate_tools`` / ``read_tools`` (mutate wins if in both).
      3. Prefix rules on the tool name (or its namespace suffix after ``__``, ``.``, ``:``,
         or ``/``), using longest prefix match with compound read+mutate protection
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

    # 3. Prefix rules on both full name and namespace suffix (e.g. `search_api.delete_index`);
    #    mutate wins across both so a read-prefixed namespace cannot mask a mutating action.
    lower_name = tool_name.lower()
    full_kind = _classify_by_prefixes(lower_name, tools_cfg)
    suffix = _unqualified_tool_suffix(tool_name)
    suffix_kind = _classify_by_prefixes(suffix.lower(), tools_cfg) if suffix is not None else None
    if full_kind == ToolKind.MUTATE or suffix_kind == ToolKind.MUTATE:
        return ToolKind.MUTATE
    if full_kind == ToolKind.READ or suffix_kind == ToolKind.READ:
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


@dataclass(frozen=True, slots=True)
class _ResolvedEntry:
    path: str
    value: Any
    intermediate_type_error: str | None = None
    missing: bool = False


def _resolve_arg_values(
    arguments: Mapping[str, Any],
    arg_path: str,
    *,
    report_missing_in_collections: bool = False,
) -> list[_ResolvedEntry]:
    """Resolve top-level or dotted nested argument values.

    Checks both literal key match and dotted nested traversal so neither form can
    bypass a rule. Also traverses into lists/tuples of mappings and reports an
    ``intermediate_type_error`` when an intermediate container segment is present
    with a non-object value (fail-closed against JSON-in-string bypasses).
    """
    results: list[_ResolvedEntry] = []
    if "." not in arg_path:
        if arg_path in arguments:
            results.append(_ResolvedEntry(path=arg_path, value=arguments[arg_path]))
        return results

    # Also check exact literal key containing dots in case an upstream uses flat dotted keys.
    if arg_path in arguments:
        results.append(_ResolvedEntry(path=arg_path, value=arguments[arg_path]))

    segments = arg_path.split(".")
    if any(not seg for seg in segments):
        return results

    def _walk(current: Any, idx: int, prefix: str, in_collection: bool) -> None:
        if isinstance(current, (list, tuple)):
            if not current and report_missing_in_collections:
                results.append(
                    _ResolvedEntry(
                        path=f"{prefix}.{'.'.join(segments[idx:])}" if prefix else arg_path,
                        value=None,
                        missing=True,
                    )
                )
                return
            for item_idx, item in enumerate(current):
                _walk(item, idx, f"{prefix}[{item_idx}]", True)
            return
        if not isinstance(current, Mapping):
            # An intermediate key was present, but its value is not a mapping/list.
            if idx > 0:
                results.append(
                    _ResolvedEntry(
                        path=prefix or arg_path,
                        value=current,
                        intermediate_type_error=(
                            f"Argument {prefix!r} must be an object to evaluate {arg_path!r}, "
                            f"got {type(current).__name__}"
                        ),
                    )
                )
            return
        seg = segments[idx]
        if seg not in current:
            if in_collection and report_missing_in_collections:
                missing_tail = ".".join(segments[idx:])
                missing_path = f"{prefix}.{missing_tail}" if prefix else missing_tail
                results.append(_ResolvedEntry(path=missing_path, value=None, missing=True))
            return
        next_path = f"{prefix}.{seg}" if prefix else seg
        val = current[seg]
        if idx == len(segments) - 1:
            results.append(_ResolvedEntry(path=next_path, value=val))
        else:
            _walk(val, idx + 1, next_path, in_collection)

    _walk(arguments, 0, "", False)
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
    """Normalize and validate a hostname, domain, or canonical IP literal.

    Unmaps IPv4-mapped IPv6 literals (e.g. ``::ffff:127.0.0.1`` -> ``127.0.0.1``)
    and rejects alternate numeric/hex/octal IP encodings (e.g. ``2130706433``,
    ``0x7f.0.0.1``, ``127.1``) that could bypass IP/domain denylists.
    """
    if not host or _CONTROL_OR_WHITESPACE_RE.search(host) is not None:
        return None
    raw = host.lower()
    # Reject multiple trailing dots (e.g. "example.com..") while normalizing single FQDN dot.
    if raw.endswith("."):
        raw = raw[:-1]
        if not raw or raw.endswith("."):
            return None

    # Handle bracketed or bare IP literals (e.g. ::1, ::ffff:127.0.0.1, 127.0.0.1).
    ip_candidate = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    try:
        ip_obj = ipaddress.ip_address(ip_candidate)
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            return str(ip_obj.ipv4_mapped)
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

    # Reject non-canonical numeric/hex/octal IPv4 shorthands (RFC 1123 / WHATWG URL):
    # A valid DNS TLD (last label) cannot be purely numeric or a hex literal.
    last_label = labels[-1]
    if last_label.isdigit() or _HEX_LITERAL_RE.match(last_label):
        return None

    return ascii_host


def _extract_email_domain(trimmed: str) -> str | None:
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
    # Reject URL delimiters inside the email local part to prevent URL-vs-email bypasses
    # like `https:/evil.com/@good.com`, `https:evil.com/@good.com`, or `evil.com/@good.com`.
    if any(ch in _DISALLOWED_EMAIL_LOCAL_CHARS for ch in local):
        return None
    # Reject port/path suffixes attached to email domain.
    if ":" in domain or "/" in domain or "?" in domain or "#" in domain:
        return None
    return _normalize_host(domain)


def extract_candidate_domains(raw: str) -> tuple[str, ...] | None:
    """Extract all plausible normalized domains from an email, URL, or bare host.

    Returns ``None`` on malformed, ambiguous, or obfuscated inputs (fail-closed):
      - Rejects NUL bytes, control characters, and embedded newlines.
      - Rejects scheme-less protocol-relative ``//...`` strings (eliminates URL/email
        ambiguity like ``//good.com/@evil.com`` unless unambiguously parsed as email).
      - Rejects ``<`` or ``>`` inside URL-shaped values (blocks ``https://good.com/<a@evil.com>``).
      - When ``@`` appears outside a URL's authority, evaluates BOTH the URL authority
        host AND any email-parsed address domains so every candidate must satisfy
        allowlists and any candidate triggers denylists.
    """
    if not isinstance(raw, str):
        return None
    if "\x00" in raw or "\r" in raw or "\n" in raw or "\t" in raw:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None

    # Case 1: Explicit scheme URL (scheme://...)
    if "://" in trimmed:
        if _CONTROL_OR_WHITESPACE_RE.search(trimmed) is not None:
            return None
        if "<" in trimmed or ">" in trimmed:
            return None
        try:
            parsed = urlsplit(trimmed)
        except ValueError:
            return None
        if not parsed.scheme or not parsed.netloc or "\\" in parsed.netloc:
            return None
        # Reject ambiguous multiple '@' inside authority (e.g. user@good.com@evil.com)
        if parsed.netloc.count("@") > 1:
            return None
        try:
            _ = parsed.port
        except ValueError:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        url_host = _normalize_host(hostname)
        if url_host is None:
            return None

        candidates: list[str] = [url_host]
        # If '@' appears outside authority (path/query/fragment), also check email-like domain
        # readings (multi-label domain or IP) while allowing plain `@handle` path segments
        # such as `https://medium.com/@user/post` or `https://registry.npmjs.org/@scope/pkg`.
        after_authority = f"{parsed.path}?{parsed.query}#{parsed.fragment}"
        if "@" in after_authority:
            for _display, parsed_addr in getaddresses([trimmed]):
                if "@" in parsed_addr:
                    email_part = parsed_addr.rsplit("@", 1)[1]
                    email_host_raw = re.split(r"[/?#]", email_part, maxsplit=1)[0]
                    if "." in email_host_raw or ":" in email_host_raw:
                        email_host = _normalize_host(email_host_raw)
                        if email_host is not None and email_host not in candidates:
                            candidates.append(email_host)
        return tuple(candidates)

    # Reject all scheme-less protocol-relative URLs (e.g. //cdn.example.com or //evil.com/@good.com)
    if trimmed.startswith("//"):
        return None

    # Case 2: Email address (plain local@domain or RFC 5322 "Display Name <local@domain>")
    if "@" in trimmed:
        email_domain = _extract_email_domain(trimmed)
        return (email_domain,) if email_domain is not None else None

    # Case 3: Bare hostname / domain (with optional :port)
    if _CONTROL_OR_WHITESPACE_RE.search(trimmed) is not None or any(
        ch in trimmed for ch in ("/", "?", "#", "\\", ",", ";", "<", ">")
    ):
        return None
    host_part = trimmed
    if trimmed.startswith("["):
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
            host = _normalize_host(trimmed)
            return (host,) if host is not None else None
        host_part, port_str = trimmed.split(":", 1)
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            return None
    host = _normalize_host(host_part)
    return (host,) if host is not None else None


def extract_domain(raw: str) -> str | None:
    """Extract a single unambiguous normalized domain, or ``None`` if invalid/ambiguous."""
    candidates = extract_candidate_domains(raw)
    if candidates is None or len(candidates) != 1:
        return None
    return candidates[0]


def _is_valid_domain_pattern(pattern: str) -> bool:
    raw_pat = pattern.strip().lower()
    if not raw_pat:
        return False
    if raw_pat == "*":
        return True
    if raw_pat.startswith("*."):
        return _normalize_host(raw_pat[2:]) is not None
    if raw_pat.startswith("."):
        return _normalize_host(raw_pat[1:]) is not None
    return _normalize_host(raw_pat) is not None


def _domain_matches(domain: str, pattern: str, *, match_subdomains_by_default: bool) -> bool:
    """Match a normalized domain against a validated domain rule pattern."""
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


def _string_matches_pattern(value: str, pattern: str, *, case_insensitive: bool = False) -> bool:
    val = value.lower() if case_insensitive else value
    pat = pattern.lower() if case_insensitive else pattern
    if _has_glob(pat):
        return fnmatch.fnmatchcase(val, pat)
    return val == pat


def _has_path_separator(pattern: str) -> bool:
    return "/" in pattern or "\\" in pattern


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
                norm_posix = posixpath.normpath(stripped.replace("\\", "/"))
                collapsed_posix = _LEADING_SLASHES_RE.sub("/", norm_posix)
                deny_candidates = [value]
                if stripped != value:
                    deny_candidates.append(stripped)
                if "/" in stripped or "\\" in stripped:
                    deny_candidates.append(norm_posix)
                    if collapsed_posix != norm_posix:
                        deny_candidates.append(collapsed_posix)
                if any(
                    _string_matches_pattern(cand, pat, case_insensitive=True)
                    for pat in rule.deny
                    for cand in deny_candidates
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
            if rule.allow:
                path_rule = any(_has_path_separator(pat) for pat in rule.allow)
                has_traversal = path_rule and _PATH_TRAVERSAL_RE.search(value) is not None
                if has_traversal or not any(
                    _string_matches_pattern(value, pat, case_insensitive=False)
                    for pat in rule.allow
                ):
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
        # Fail closed on malformed rule patterns so a misconfigured rule never fails open.
        invalid_deny = [p for p in rule.deny_domains if not _is_valid_domain_pattern(p)]
        invalid_allow = [p for p in rule.allow_domains if not _is_valid_domain_pattern(p)]
        if invalid_deny or invalid_allow:
            bad = ", ".join(repr(p) for p in (*invalid_deny, *invalid_allow))
            violations.append(
                PreflightViolation(
                    tool=tool_name,
                    arg=resolved_path,
                    constraint="invalid_rule",
                    value=value,
                    rule_reason=rule.reason,
                    message=_format_violation_message(
                        f"Preflight rule for {resolved_path!r} contains invalid domain "
                        f"pattern(s): {bad}",
                        rule.reason,
                    ),
                )
            )
            return

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
            candidates = extract_candidate_domains(value)
            if not candidates:
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
                if rule.deny_domains:
                    for domain in candidates:
                        if any(
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
                                        f"Argument {resolved_path!r} domain {domain!r} is "
                                        "denied by policy",
                                        rule.reason,
                                    ),
                                )
                            )
                if rule.allow_domains:
                    for domain in candidates:
                        if not any(
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
                                        f"Argument {resolved_path!r} domain {domain!r} is not "
                                        "in allowed domains",
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
            try:
                resolved_entries = _resolve_arg_values(
                    args_mapping,
                    rule.arg,
                    report_missing_in_collections=rule.required,
                )
            except RecursionError:
                violations.append(
                    PreflightViolation(
                        tool=tool_name,
                        arg=rule.arg,
                        constraint="invalid_type",
                        message=f"Argument {rule.arg!r} exceeded maximum nesting depth",
                        rule_reason=rule.reason,
                    )
                )
                continue
            if not resolved_entries:
                if rule.required:
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=rule.arg,
                            constraint="required",
                            value=None,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Required argument {rule.arg!r} is missing",
                                rule.reason,
                            ),
                        )
                    )
                continue
            for entry in resolved_entries:
                if entry.missing:
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=entry.path,
                            constraint="required",
                            value=None,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Required argument {entry.path!r} is missing",
                                rule.reason,
                            ),
                        )
                    )
                    continue
                if entry.intermediate_type_error is not None:
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=entry.path,
                            constraint="invalid_type",
                            value=entry.value,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                entry.intermediate_type_error,
                                rule.reason,
                            ),
                        )
                    )
                    continue
                try:
                    scalar_items = _iter_scalar_values(entry.path, entry.value)
                except RecursionError:
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=entry.path,
                            constraint="invalid_type",
                            message=f"Argument {entry.path!r} exceeded maximum nesting depth",
                            rule_reason=rule.reason,
                        )
                    )
                    continue
                if not scalar_items and rule.required:
                    violations.append(
                        PreflightViolation(
                            tool=tool_name,
                            arg=entry.path,
                            constraint="required",
                            value=entry.value,
                            rule_reason=rule.reason,
                            message=_format_violation_message(
                                f"Required argument {entry.path!r} must not be empty",
                                rule.reason,
                            ),
                        )
                    )
                    continue
                for scalar_path, scalar_val in scalar_items:
                    if rule.required and scalar_val is None:
                        violations.append(
                            PreflightViolation(
                                tool=tool_name,
                                arg=scalar_path,
                                constraint="required",
                                value=None,
                                rule_reason=rule.reason,
                                message=_format_violation_message(
                                    f"Required argument {scalar_path!r} must not be null",
                                    rule.reason,
                                ),
                            )
                        )
                        continue
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
