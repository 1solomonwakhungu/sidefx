"""GitHub Issues, Pull Requests, and Comments reconciler adapter and marker helpers."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

from sidefx.journal import ActionRecord
from sidefx.reconcilers.base import (
    ReconcileResult,
    ReconcileState,
    parse_summary_meta,
    validate_marker_token,
)

DEFAULT_GITHUB_API_URL = "https://api.github.com"
ENV_GITHUB_API_URL = "SIDEFX_GITHUB_API_URL"
DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 10
DEFAULT_HTTP_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB safety cap per page

_MARKER_COMMENT_RE = re.compile(r"<!--\s*sidefx:id=([A-Za-z0-9._-]{1,128})\s*-->")
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TARGET_REPO_ISSUE_RE = re.compile(
    r"^(?:github:)?([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})(?:#(\d+))?$"
)
_GITHUB_URL_TARGET_RE = re.compile(
    r"^https?://[^/\s]+/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})"
    r"(?:/(issues|pull|pulls)(?:/(\d+))?)?(?:/.*)?$"
)
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*[^,]*\brel\s*=\s*"?next"?', re.IGNORECASE)

_GITHUB_ISSUE_TOOLS = frozenset(
    {
        "gh_issue_create",
        "create_issue",
        "github_create_issue",
        "create_github_issue",
    }
)
_GITHUB_PR_TOOLS = frozenset(
    {
        "gh_pr_create",
        "create_pr",
        "create_pull_request",
        "github_create_pr",
        "github_create_pull_request",
    }
)
_GITHUB_COMMENT_TOOLS = frozenset(
    {
        "gh_issue_comment",
        "gh_pr_comment",
        "create_issue_comment",
        "create_pr_comment",
        "comment_issue",
        "comment_pr",
        "add_issue_comment",
        "github_comment",
    }
)
_GITHUB_GENERIC_TOOLS = frozenset(
    {
        "github_post",
        "gh_api",
    }
)
_ALL_GITHUB_TOOLS = (
    _GITHUB_ISSUE_TOOLS | _GITHUB_PR_TOOLS | _GITHUB_COMMENT_TOOLS | _GITHUB_GENERIC_TOOLS
)


def format_marker_comment(marker: str) -> str:
    """Format a compact deterministic HTML comment marker for GitHub bodies."""
    safe = validate_marker_token(marker)
    return f"<!-- sidefx:id={safe} -->"


def extract_markers(text: str | None) -> list[str]:
    """Extract all sidefx marker tokens embedded in ``text``."""
    if not text:
        return []
    return _MARKER_COMMENT_RE.findall(text)


def body_has_marker(body: str | None, marker: str) -> bool:
    """Return True if ``body`` contains an exact sidefx comment marker for ``marker``."""
    if not body or not marker:
        return False
    return marker in extract_markers(body)


def inject_marker_into_body(body: str | None, marker: str) -> str:
    """Idempotently append ``<!-- sidefx:id=<marker> -->`` to a markdown/text body."""
    comment = format_marker_comment(marker)
    if body is None or body == "":
        return comment
    if body_has_marker(body, marker):
        return body
    trimmed = body.rstrip("\r\n")
    if not trimmed:
        return comment
    return f"{trimmed}\n\n{comment}"


def inject_marker_into_json_body(
    raw_body: str, marker: str, *, field: str = "body"
) -> tuple[str, bool]:
    """Inject a sidefx marker into a JSON object payload's ``field`` (default ``body``).

    Returns ``(updated_json_str, injected_bool)``. If ``raw_body`` is not a JSON object
    or ``field`` exists with a non-string/non-null value, leaves ``raw_body`` untouched
    and returns ``(raw_body, False)``.
    """
    validate_marker_token(marker)
    try:
        parsed = json.loads(raw_body)
    except (ValueError, RecursionError):
        return raw_body, False
    if not isinstance(parsed, dict):
        return raw_body, False
    existing = parsed.get(field)
    if existing is not None and not isinstance(existing, str):
        return raw_body, False
    parsed[field] = inject_marker_into_body(existing, marker)
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False), True


def _is_gh_write_subcommand(argv: Sequence[str]) -> str | None:
    """Return ``'issue'``, ``'pr'``, or ``'comment'`` for supported ``gh`` write commands."""
    if len(argv) < 3:
        return None
    exe = os.path.basename(argv[0]).lower()
    if exe not in ("gh", "gh.exe"):
        return None
    sub1 = argv[1].lower()
    sub2 = argv[2].lower()
    if sub1 == "issue" and sub2 == "create":
        return "issue"
    if sub1 == "pr" and sub2 == "create":
        return "pr"
    if sub1 in ("issue", "pr") and sub2 == "comment":
        return "comment"
    return None


def inject_marker_into_gh_args(argv: Sequence[str], marker: str) -> tuple[list[str], bool]:
    """Inject a deterministic sidefx HTML comment marker into a ``gh`` write command.

    Supports ``gh issue create``, ``gh pr create``, ``gh issue comment``, and
    ``gh pr comment``. Avoids clobbering ``--body-file`` / ``-F``, ``--editor`` / ``-e``,
    ``--web`` / ``-w``, or ``--fill*`` flags (returning ``injected=False`` in those cases
    so the reconciler knows no marker was embedded).
    """
    validate_marker_token(marker)
    args = list(argv)
    kind = _is_gh_write_subcommand(args)
    if kind is None:
        return args, False

    end_opts_idx = len(args)
    for idx in range(3, len(args)):
        if args[idx] == "--":
            end_opts_idx = idx
            break

    # Flags where we cannot safely inject an inline --body without changing user intent.
    no_inject_flags = {
        "-F",
        "--body-file",
        "-e",
        "--editor",
        "-w",
        "--web",
        "-f",
        "--fill",
        "--fill-first",
        "--fill-verbose",
        "-T",
        "--template",
    }

    idx = 3
    while idx < end_opts_idx:
        tok = args[idx]
        if tok in ("-b", "--body"):
            if idx + 1 >= end_opts_idx:
                return args, False
            args[idx + 1] = inject_marker_into_body(args[idx + 1], marker)
            return args, True
        if tok.startswith("--body="):
            prefix, val = tok.split("=", 1)
            args[idx] = f"{prefix}={inject_marker_into_body(val, marker)}"
            return args, True
        if tok.startswith("-b="):
            prefix, val = tok.split("=", 1)
            args[idx] = f"{prefix}={inject_marker_into_body(val, marker)}"
            return args, True
        if tok in no_inject_flags or any(
            tok.startswith(f"{flag}=") for flag in ("--body-file", "-F", "--template", "-T")
        ):
            return args, False
        idx += 1

    # No explicit body or conflicting flag: insert `--body <marker>` before any `--`.
    insertion = ["--body", format_marker_comment(marker)]
    updated = args[:end_opts_idx] + insertion + args[end_opts_idx:]
    return updated, True


def parse_github_repo(value: str) -> tuple[str, str] | None:
    """Parse and validate a GitHub ``owner/repo`` pair from slug or URL."""
    if not value or "\x00" in value:
        return None
    cleaned = value.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    m = _TARGET_REPO_ISSUE_RE.match(cleaned)
    if m:
        owner, repo = m.group(1), m.group(2)
        if _is_valid_repo_segment(owner) and _is_valid_repo_segment(repo):
            return owner, repo
        return None
    m_url = _GITHUB_URL_TARGET_RE.match(cleaned)
    if m_url:
        owner, repo = m_url.group(1), m_url.group(2)
        if _is_valid_repo_segment(owner) and _is_valid_repo_segment(repo):
            return owner, repo
    return None


def _is_valid_repo_segment(seg: str) -> bool:
    return bool(_REPO_SEGMENT_RE.match(seg)) and seg not in (".", "..")


def _validate_api_base_url(url: str) -> tuple[str, str, str]:
    """Validate and normalize ``api_base_url``, returning ``(base_url, scheme, netloc)``."""
    cleaned = url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"GitHub API base URL must use http or https, got {url!r}")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Invalid GitHub API base URL (host required, no credentials): {url!r}")
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
        raise ValueError("GitHub API base URL must not contain control characters")
    return cleaned, parsed.scheme.lower(), parsed.netloc.lower()


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract RFC 5988 ``rel="next"`` URL from an HTTP ``Link`` header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        match = _LINK_NEXT_RE.search(part)
        if match:
            return match.group(1).strip()
    return None


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses cross-origin redirects to prevent token leaks and SSRF."""

    def __init__(self, allowed_scheme: str, allowed_netloc: str) -> None:
        super().__init__()
        self._allowed_scheme = allowed_scheme
        self._allowed_netloc = allowed_netloc

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        parts = urllib.parse.urlsplit(resolved)
        if (
            parts.scheme.lower() != self._allowed_scheme
            or parts.netloc.lower() != self._allowed_netloc
        ):
            raise urllib.error.HTTPError(
                resolved,
                code,
                "Refused cross-origin redirect from GitHub API",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


class GitHubReconciler:
    """Read-only GitHub Issues/PRs/Comments reconciler using stdlib ``urllib.request``.

    Queries across BOTH open and closed items (``state=all``) with RFC 5988 pagination
    and matches strictly by exact ``<!-- sidefx:id=<marker> -->`` comment marker.
    """

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT,
        per_page: int = DEFAULT_PER_PAGE,
        max_pages: int = DEFAULT_MAX_PAGES,
        expected_author: str | None = None,
    ) -> None:
        raw_base = (
            api_base_url
            if api_base_url is not None
            else os.environ.get(ENV_GITHUB_API_URL, "").strip() or DEFAULT_GITHUB_API_URL
        )
        base, scheme, netloc = _validate_api_base_url(raw_base)
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if (
            isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or not (1 <= per_page <= 100)
        ):
            raise ValueError("per_page must be an integer between 1 and 100")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")

        self._api_base_url = base
        self._scheme = scheme
        self._netloc = netloc
        self._token = (
            token
            if token is not None
            else (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
            or None
        )
        self._timeout_seconds = float(timeout_seconds)
        self._per_page = per_page
        self._max_pages = max_pages
        self._expected_author = expected_author.strip() if expected_author else None
        self._opener = urllib.request.build_opener(
            _SameOriginRedirectHandler(self._scheme, self._netloc)
        )

    @property
    def name(self) -> str:
        return "github"

    @property
    def api_base_url(self) -> str:
        return self._api_base_url

    def can_reconcile(self, record: ActionRecord) -> bool:
        _, meta = parse_summary_meta(record.summary)
        if meta.get("adapter") == "github":
            return True
        if record.tool_name.lower() in _ALL_GITHUB_TOOLS:
            return True
        return record.target.startswith("github:")

    def _resolve_query_spec(self, record: ActionRecord) -> dict[str, Any] | None:
        """Extract validated repo, kind, optional issue_number, and author from ``record``."""
        _, meta = parse_summary_meta(record.summary)
        tool = record.tool_name.lower()

        if meta.get("marker_injected") is False:
            return {"error": "Marker was not injected into outgoing GitHub payload"}

        repo_pair: tuple[str, str] | None = None
        if isinstance(meta.get("repo"), str):
            repo_pair = parse_github_repo(meta["repo"])
        if repo_pair is None:
            repo_pair = parse_github_repo(record.target)
        if repo_pair is None:
            return {"error": f"Unable to determine GitHub owner/repo from target {record.target!r}"}

        owner, repo = repo_pair

        kind = meta.get("kind")
        if not isinstance(kind, str) or kind not in ("issue", "pr", "comment", "issue_or_pr"):
            if tool in _GITHUB_ISSUE_TOOLS:
                kind = "issue"
            elif tool in _GITHUB_PR_TOOLS:
                kind = "pr"
            elif tool in _GITHUB_COMMENT_TOOLS:
                kind = "comment"
            else:
                kind = "issue_or_pr"

        issue_number: int | None = None
        raw_num = meta.get("issue_number")
        if isinstance(raw_num, int) and not isinstance(raw_num, bool) and raw_num > 0:
            issue_number = raw_num
        elif isinstance(raw_num, str) and raw_num.isdigit() and int(raw_num) > 0:
            issue_number = int(raw_num)
        else:
            m_target = _TARGET_REPO_ISSUE_RE.match(record.target.strip())
            if m_target and m_target.group(3):
                issue_number = int(m_target.group(3))
            else:
                m_url = _GITHUB_URL_TARGET_RE.match(record.target.strip())
                if m_url and m_url.group(4):
                    issue_number = int(m_url.group(4))

        author = self._expected_author
        if author is None and isinstance(meta.get("author"), str) and meta["author"].strip():
            author = meta["author"].strip()

        return {
            "owner": owner,
            "repo": repo,
            "kind": kind,
            "issue_number": issue_number,
            "author": author,
        }

    def _build_initial_url(
        self, *, owner: str, repo: str, kind: str, issue_number: int | None
    ) -> str:
        safe_owner = urllib.parse.quote(owner, safe="")
        safe_repo = urllib.parse.quote(repo, safe="")
        base = f"{self._api_base_url}/repos/{safe_owner}/{safe_repo}"

        if kind == "issue":
            params = urllib.parse.urlencode(
                {
                    "state": "all",
                    "per_page": str(self._per_page),
                    "sort": "created",
                    "direction": "desc",
                }
            )
            return f"{base}/issues?{params}"

        if kind == "pr":
            params = urllib.parse.urlencode(
                {
                    "state": "all",
                    "per_page": str(self._per_page),
                    "sort": "created",
                    "direction": "desc",
                }
            )
            return f"{base}/pulls?{params}"

        if kind == "comment":
            if issue_number is not None:
                params = urllib.parse.urlencode({"per_page": str(self._per_page)})
                return f"{base}/issues/{issue_number}/comments?{params}"
            params = urllib.parse.urlencode(
                {
                    "per_page": str(self._per_page),
                    "sort": "created",
                    "direction": "desc",
                }
            )
            return f"{base}/issues/comments?{params}"

        # Generic issue_or_pr fallback: GitHub /issues?state=all returns both issues and PRs.
        params = urllib.parse.urlencode(
            {
                "state": "all",
                "per_page": str(self._per_page),
                "sort": "created",
                "direction": "desc",
            }
        )
        return f"{base}/issues?{params}"

    def _fetch_page(self, url: str) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
        """Fetch one JSON list page from GitHub API.

        Returns ``(items, next_url, error_message)``. Any HTTP, network, timeout,
        origin mismatch, or JSON structure problem yields ``(None, None, error_message)``.
        """
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != self._scheme or parsed.netloc.lower() != self._netloc:
            return None, None, f"Refused pagination URL with mismatched origin: {url!r}"

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "sidefx-reconciler",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as resp:
                status = getattr(resp, "status", resp.getcode())
                if status != 200:
                    return None, None, f"Unexpected GitHub API HTTP status {status}"
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    return None, None, "GitHub API response exceeded safety size limit"
                link_header = resp.headers.get("Link")
        except urllib.error.HTTPError as exc:
            return None, None, f"GitHub API HTTP error {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            return None, None, f"GitHub API connection error: {exc.reason}"
        except TimeoutError as exc:
            return None, None, f"GitHub API request timed out: {exc}"
        except OSError as exc:
            return None, None, f"GitHub API I/O error: {exc}"

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            return None, None, f"Malformed JSON from GitHub API: {exc}"

        if not isinstance(payload, list):
            return None, None, f"Expected JSON list from GitHub API, got {type(payload).__name__}"

        items: list[dict[str, Any]] = []
        for idx, entry in enumerate(payload):
            if not isinstance(entry, dict):
                return (
                    None,
                    None,
                    f"Malformed GitHub API list element at index {idx}: expected object",
                )
            items.append(entry)

        next_raw = _parse_next_link(link_header)
        next_url = urllib.parse.urljoin(url, next_raw) if next_raw else None
        return items, next_url, None

    def reconcile(self, record: ActionRecord) -> ReconcileResult:
        """Query GitHub API read-only across open and closed items for ``record.marker``."""
        try:
            validate_marker_token(record.marker)
        except ValueError as exc:
            return ReconcileResult(state=ReconcileState.UNKNOWN, details=str(exc))

        spec = self._resolve_query_spec(record)
        if spec is None:
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details="Unable to build GitHub reconciliation query",
            )
        if "error" in spec:
            return ReconcileResult(state=ReconcileState.UNKNOWN, details=str(spec["error"]))

        owner = spec["owner"]
        repo = spec["repo"]
        kind = spec["kind"]
        issue_number = spec["issue_number"]
        author = spec["author"]

        url: str | None = self._build_initial_url(
            owner=owner, repo=repo, kind=kind, issue_number=issue_number
        )
        visited: set[str] = set()
        matches: list[dict[str, Any]] = []
        pages_fetched = 0
        truncated = False

        while url is not None:
            if url in visited:
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details="Detected pagination loop in GitHub API Link headers",
                )
            if pages_fetched >= self._max_pages:
                truncated = True
                break
            visited.add(url)
            pages_fetched += 1

            items, next_url, err = self._fetch_page(url)
            if err is not None or items is None:
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details=err or "GitHub API lookup failed",
                )

            for item in items:
                if kind == "issue" and "pull_request" in item:
                    continue
                if author is not None:
                    user_obj = item.get("user")
                    login = user_obj.get("login") if isinstance(user_obj, dict) else None
                    if not isinstance(login, str) or login.lower() != author.lower():
                        continue
                body = item.get("body")
                if isinstance(body, str) and body_has_marker(body, record.marker):
                    matches.append(item)

            if len(matches) > 1:
                urls = [str(m.get("html_url") or m.get("id") or "?") for m in matches]
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details=(
                        f"Ambiguous GitHub reconciliation: found {len(matches)} items matching "
                        f"marker {record.marker} ({', '.join(urls)})"
                    ),
                )

            url = next_url

        if len(matches) == 1:
            matched = matches[0]
            html_url = matched.get("html_url")
            if not isinstance(html_url, str) or not html_url.strip():
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details="Matched GitHub item is missing a valid html_url",
                )
            number = matched.get("number")
            state = matched.get("state")
            detail_parts = [f"Matched GitHub {kind}"]
            if isinstance(number, int):
                detail_parts.append(f"#{number}")
            if isinstance(state, str) and state:
                detail_parts.append(f"(state={state})")
            return ReconcileResult(
                state=ReconcileState.FOUND,
                result_ref=html_url.strip(),
                summary=" ".join(detail_parts),
                details=f"Recovered via exact marker {record.marker} in {owner}/{repo}",
            )

        if truncated:
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=(
                    f"Pagination cap ({self._max_pages} pages) reached in {owner}/{repo} "
                    "without finding marker; cannot confirm not_found"
                ),
            )

        return ReconcileResult(
            state=ReconcileState.NOT_FOUND,
            details=(
                f"Exhaustive scan of {pages_fetched} page(s) in {owner}/{repo} ({kind}) "
                f"found 0 matches for marker {record.marker}"
            ),
        )
