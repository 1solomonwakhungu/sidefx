"""Crash-proof secret and credential redaction helpers shared across sidefx surfaces."""

from __future__ import annotations

import hashlib
import re
import urllib.parse

_TELEGRAM_BOT_SEG_RE = re.compile(r"^bot\d+:.+$")
_KNOWN_SECRET_PREFIX_RE = re.compile(
    r"^(?:xox[baprs]-|gh[pousr]_|github_pat_|sk[-_]|pk_|rk_|whsec_|AIza|AKIA|eyJ)"
)
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{24,}$")
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{20,}$")

# Match any RFC 3986 scheme://... URL, allowing single quotes inside credentials/paths
# while stripping trailing closing quotes/punctuation after matching.
# Negative lookbehind prevents quadratic backtracking across long alphanumeric runs.
_URL_SCHEME_IN_TEXT_RE = re.compile(
    r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s\"<>]+", re.IGNORECASE
)
_FALLBACK_USERINFO_RE = re.compile(r"(://)[^/\s]*@")
_FALLBACK_QUERY_FRAG_RE = re.compile(r"[?#].*$")

_AUTH_TOKEN_RE = re.compile(r"\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_FREE_SECRET_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_|github_pat_|xox[baprs]-|sk[-_]|AKIA|whsec_)[A-Za-z0-9_\-]{8,}"
)
_KV_SECRET_RE = re.compile(r"\b([A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD)=)\S+")


def _mask_segment(segment: str, *, prefix: str = "") -> str:
    digest = hashlib.sha256(segment.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{prefix}<redacted:{digest}>"


def _is_token_like_path_segment(segment: str) -> bool:
    """Return True when a URL path segment looks like an embedded webhook or API secret."""
    if not segment:
        return False
    if _KNOWN_SECRET_PREFIX_RE.match(segment):
        return True
    if _HEX_TOKEN_RE.match(segment):
        return True
    if _OPAQUE_TOKEN_RE.match(segment):
        has_lower = any("a" <= ch <= "z" for ch in segment)
        has_upper = any("A" <= ch <= "Z" for ch in segment)
        has_digit = any("0" <= ch <= "9" for ch in segment)
        # Mixed case + digits (Slack/Discord/Stripe webhook tokens) or long mixed-case base64url.
        if (has_lower and has_upper and has_digit) or (
            len(segment) >= 24 and has_lower and has_upper
        ):
            return True
    return False


def _redact_url_path(path: str) -> str:
    if not path or "/" not in path:
        return path
    out: list[str] = []
    for seg in path.split("/"):
        if _TELEGRAM_BOT_SEG_RE.match(seg):
            out.append(_mask_segment(seg, prefix="bot"))
        elif _is_token_like_path_segment(seg):
            out.append(_mask_segment(seg))
        else:
            out.append(seg)
    return "/".join(out)


def _fallback_redact_malformed_url(url: str, *, redact_path_tokens: bool = True) -> str:
    """Safely strip userinfo and query/fragment from malformed URLs without raising."""
    try:
        cleaned = _FALLBACK_USERINFO_RE.sub(r"\1", url)
        cleaned = _FALLBACK_QUERY_FRAG_RE.sub("", cleaned)
        if redact_path_tokens and "://" in cleaned:
            scheme, rest = cleaned.split("://", 1)
            if "/" in rest:
                authority, path = rest.split("/", 1)
                cleaned = f"{scheme}://{authority}/{_redact_url_path('/' + path).lstrip('/')}"
        return cleaned
    except Exception:
        return "<redacted-url>"


def redact_url_for_display(url: str) -> str:
    """Strip userinfo, query/fragment, and token-like path segments from URLs.

    Never raises on malformed URLs (invalid ports, bad IPv6 brackets, etc.).
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        port = parsed.port
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{port}" if port is not None else host
        safe_path = _redact_url_path(parsed.path)
        return urllib.parse.urlunsplit((parsed.scheme, netloc, safe_path, "", ""))
    except ValueError:
        return _fallback_redact_malformed_url(url, redact_path_tokens=True)
    except Exception:
        return "<redacted-url>"


def redact_remote_for_display(remote: str) -> str:
    """Strip passwords/query strings from URL-shaped git remotes while preserving SSH user.

    Never raises on malformed URLs (invalid ports, bad IPv6 brackets, etc.).
    """
    try:
        parsed = urllib.parse.urlsplit(remote)
        if parsed.scheme and parsed.netloc:
            port = parsed.port
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            hostport = f"{host}:{port}" if port is not None else host
            # Preserve non-password username on ssh:// URLs so `git ls-remote` uses the right user.
            if (
                parsed.scheme.lower() in ("ssh", "git+ssh")
                and parsed.username
                and parsed.password is None
            ):
                netloc = f"{parsed.username}@{hostport}"
            else:
                netloc = hostport
            return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return remote
    except ValueError:
        return _fallback_redact_malformed_url(remote, redact_path_tokens=False)
    except Exception:
        return "<redacted-url>"


def _replace_url_match(match: re.Match[str], *, remote_mode: bool = False) -> str:
    raw = match.group(0)
    trimmed = raw.rstrip(".,);]'")
    suffix = raw[len(trimmed) :]
    if not trimmed or "://" not in trimmed:
        return raw
    redacted = (
        redact_remote_for_display(trimmed) if remote_mode else redact_url_for_display(trimmed)
    )
    return f"{redacted}{suffix}"


def redact_secrets_in_text(text: str, *, remote_mode: bool = False) -> str:
    """Redact credentials in URLs, auth headers, known token prefixes, and KEY=VAL pairs.

    Guaranteed not to raise; returns ``"<redacted>"`` on any unexpected failure.
    """
    try:
        out = _URL_SCHEME_IN_TEXT_RE.sub(
            lambda m: _replace_url_match(m, remote_mode=remote_mode),
            text,
        )
        out = _AUTH_TOKEN_RE.sub(lambda m: f"{m.group(1)} <redacted>", out)
        out = _FREE_SECRET_TOKEN_RE.sub("<redacted>", out)
        out = _KV_SECRET_RE.sub(r"\1<redacted>", out)
        return out
    except Exception:
        return "<redacted>"
