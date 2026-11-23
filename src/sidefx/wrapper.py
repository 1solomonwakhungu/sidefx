"""Write-ahead journaled CLI mutation wrapper (`sidefx wrap`)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.parse
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidefx.config import SidefxConfig, load_config
from sidefx.journal import (
    ActionStatus,
    BeginDecision,
    InvalidStateTransitionError,
    Journal,
    PayloadMismatchError,
    StaleAttemptError,
    Surface,
    derive_marker,
    resolve_db_path,
)
from sidefx.reconcilers import (
    DEFAULT_GITHUB_API_URL,
    ENV_GITHUB_API_URL,
    Reconciler,
    ReconcileResult,
    ReconcileState,
    _record_age_seconds,
    default_reconcilers,
    format_summary_with_meta,
    inject_marker_into_gh_args,
    inject_marker_into_json_body,
    inspect_git_push,
    reconcile_action,
)
from sidefx.reconcilers.git_push import redact_remote_for_display
from sidefx.reconcilers.github import _is_loopback_host, parse_github_repo

DEFAULT_MIN_RETRY_AGE_SECONDS = 5.0
EXIT_PAYLOAD_MISMATCH = 2
EXIT_OUTCOME_UNKNOWN = 75
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_GH_ISSUE_URL_RE = re.compile(r"https?://[^\s'\"<>]+/(?:issues|pull)/\d+(?:#issuecomment-\d+)?")
_GITHUB_REST_ENDPOINT_RE = re.compile(
    r"^/repos/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/"
    r"(issues|pulls|issues/(\d+)/comments)/?$"
)
_GIT_SSH_REPO_RE = re.compile(
    r"^(?:git@|ssh://git@)([^/:]+)[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?$"
)
_GIT_HTTPS_REPO_RE = re.compile(
    r"^https?://([^/]+)/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
_GH_HOST_REPO_RE = re.compile(r"^([A-Za-z0-9.-]+)/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})$")

# Curl exit codes that prove failure occurred BEFORE sending any request payload.
_CURL_PRE_CONNECT_EXIT_CODES = frozenset({2, 3, 5, 6, 7})


class _SignalTermination(BaseException):
    """Internal control-flow exception raised when wrap receives SIGTERM/SIGHUP."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"Terminated by signal {signum}")


@dataclass(frozen=True, slots=True)
class WrapResult:
    """Structured outcome of `wrap_command` for CLI and programmatic callers."""

    exit_code: int
    action_id: str
    marker: str
    target: str
    tool_name: str
    status: ActionStatus | None
    executed: bool
    replayed: bool = False
    reconciled: bool = False
    result_ref: str | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    error: str | None = None
    reconcile_result: ReconcileResult | None = None


@dataclass(frozen=True, slots=True)
class _CommandPlan:
    tool_name: str
    target: str
    display_summary: str
    meta: dict[str, Any]
    payload: dict[str, Any]
    exec_argv: list[str]
    is_reconcilable: bool
    kind: str


def _redact_url_for_display(url: str) -> str:
    """Strip userinfo and query/fragment from URLs before storing in target/summary."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _parse_gh_repo_arg(raw: str) -> tuple[str | None, str | None]:
    """Parse ``OWNER/REPO``, ``HOST/OWNER/REPO``, or URL into ``(owner/repo, host_or_none)``."""
    cleaned = raw.strip()
    m_host = _GH_HOST_REPO_RE.match(cleaned)
    if m_host and "." in m_host.group(1):
        host, owner, repo = m_host.group(1).lower(), m_host.group(2), m_host.group(3)
        parsed = parse_github_repo(f"{owner}/{repo}")
        if parsed is not None:
            return f"{parsed[0]}/{parsed[1]}", host
        return None, host

    parsed_url = urllib.parse.urlsplit(cleaned)
    if parsed_url.scheme in ("http", "https") and parsed_url.hostname:
        parsed_repo = parse_github_repo(cleaned)
        if parsed_repo is not None:
            return f"{parsed_repo[0]}/{parsed_repo[1]}", parsed_url.hostname.lower()
        return None, parsed_url.hostname.lower()

    parsed_repo = parse_github_repo(cleaned)
    if parsed_repo is not None:
        return f"{parsed_repo[0]}/{parsed_repo[1]}", None
    return None, None


def _detect_git_origin_repo(cwd: Path) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0:
        return None, None
    raw = proc.stdout.strip()
    for pattern in (_GIT_SSH_REPO_RE, _GIT_HTTPS_REPO_RE):
        m = pattern.match(raw)
        if m:
            host = m.group(1).split("@")[-1].split(":")[0].lower()
            parsed = parse_github_repo(m.group(2))
            if parsed is not None:
                return f"{parsed[0]}/{parsed[1]}", host
    return None, None


def _extract_gh_repo_and_number(
    argv: Sequence[str],
    *,
    explicit_target: str | None,
    cwd: Path,
    github_api_url: str | None,
) -> tuple[str | None, int | None, bool, bool]:
    """Return ``(repo, issue_number, repo_inferred, host_unverified)`` for a ``gh`` command."""
    repo: str | None = None
    repo_host: str | None = None
    issue_number: int | None = None
    repo_inferred = False

    # Value-taking `gh` flags to skip when scanning for -R/--repo or positional issue number.
    gh_value_flags = {
        "-t",
        "--title",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-a",
        "--assignee",
        "-l",
        "--label",
        "-m",
        "--milestone",
        "-p",
        "--project",
        "-B",
        "--base",
        "-H",
        "--head",
        "-r",
        "--reviewer",
        "-T",
        "--template",
    }

    idx = 3
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--":
            break
        if tok in ("-R", "--repo") and idx + 1 < len(argv):
            repo, repo_host = _parse_gh_repo_arg(argv[idx + 1])
            idx += 2
            continue
        if tok.startswith("--repo="):
            repo, repo_host = _parse_gh_repo_arg(tok.split("=", 1)[1])
            idx += 1
            continue
        if tok.startswith("-R="):
            repo, repo_host = _parse_gh_repo_arg(tok.split("=", 1)[1])
            idx += 1
            continue
        if tok.startswith("-R") and len(tok) > 2:
            repo, repo_host = _parse_gh_repo_arg(tok[2:])
            idx += 1
            continue
        if tok in gh_value_flags:
            idx += 2
            continue
        idx += 1

    # Positional issue/PR number or URL for `gh issue comment <num|url>` / `gh pr comment <num|url>`
    if len(argv) >= 4 and argv[2].lower() == "comment":
        comment_val_flags = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
        p_idx = 3
        while p_idx < len(argv):
            tok = argv[p_idx]
            if tok == "--":
                if p_idx + 1 < len(argv):
                    cand = argv[p_idx + 1]
                    if cand.isdigit() and int(cand) > 0:
                        issue_number = int(cand)
                break
            if tok in comment_val_flags:
                p_idx += 2
                continue
            if tok.startswith("-"):
                p_idx += 1
                continue
            if tok.isdigit() and int(tok) > 0:
                issue_number = int(tok)
                break
            m = re.search(
                r"^https?://([^/\s]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:issues|pull)/(\d+)",
                tok,
            )
            if m:
                if repo is None:
                    repo = f"{m.group(2)}/{m.group(3)}"
                    repo_host = m.group(1).lower()
                issue_number = int(m.group(4))
                break
            p_idx += 1

    if repo is None:
        env_repo = os.environ.get("GH_REPO", "").strip()
        if env_repo:
            repo, repo_host = _parse_gh_repo_arg(env_repo)

    if repo is None and explicit_target:
        # `gh` never sees sidefx's `--target`, so a repo derived only from `--target`
        # is NOT verified to be `gh`'s destination and must be capped at UNKNOWN on 0 matches!
        target_repo, target_host = _parse_gh_repo_arg(explicit_target)
        if target_repo is not None:
            repo = target_repo
            repo_host = target_host
            repo_inferred = True
        m_num = re.search(r"#(\d+)$", explicit_target.strip())
        if issue_number is None and m_num:
            issue_number = int(m_num.group(1))

    if repo is None:
        detected_repo, detected_host = _detect_git_origin_repo(cwd)
        if detected_repo is not None:
            repo = detected_repo
            repo_host = detected_host
            repo_inferred = True

    gh_host_env = os.environ.get("GH_HOST", "").strip().lower()
    effective_host = repo_host or gh_host_env or None
    configured_api = _resolve_configured_github_api_base(github_api_url)
    api_parsed = urllib.parse.urlsplit(configured_api)
    api_host = (api_parsed.hostname or "").lower()

    if api_host == "api.github.com":
        host_unverified = effective_host is not None and effective_host != "github.com"
    elif _is_loopback_host(api_host):
        host_unverified = effective_host is not None and effective_host not in (
            "github.com",
            api_host,
        )
    else:
        # Non-default GHE/custom API host: require effective gh host to match the API host!
        host_unverified = effective_host is None or effective_host != api_host

    return repo, issue_number, repo_inferred, host_unverified


def _resolve_configured_github_api_base(github_api_url: str | None) -> str:
    raw = (
        github_api_url
        if github_api_url is not None
        else os.environ.get(ENV_GITHUB_API_URL, "").strip() or DEFAULT_GITHUB_API_URL
    )
    return raw.strip().rstrip("/")


def _inspect_curl_command(
    argv: Sequence[str], *, marker: str, configured_github_api_url: str
) -> tuple[str | None, dict[str, Any], list[str], bool]:
    """Detect ``curl`` target URL and strictly validated GitHub REST POST marker injection."""
    url: str | None = None
    method: str | None = None
    force_get = False
    data_indices: list[tuple[int, str]] = []  # (idx, "separate" | "equals" | "attached")
    value_flags = {
        "-X",
        "--request",
        "-H",
        "--header",
        "-u",
        "--user",
        "-o",
        "--output",
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-m",
        "--max-time",
        "--connect-timeout",
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-ascii",
        "--json",
    }

    idx = 1
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--":
            if idx + 1 < len(argv) and argv[idx + 1].startswith(("http://", "https://")):
                url = argv[idx + 1]
            break
        if tok in ("-G", "--get"):
            force_get = True
            idx += 1
            continue
        if tok in ("-X", "--request") and idx + 1 < len(argv):
            method = argv[idx + 1].upper()
            idx += 2
            continue
        if tok.startswith("--request="):
            method = tok.split("=", 1)[1].upper()
            idx += 1
            continue
        if tok.startswith("-X") and len(tok) > 2:
            method = tok[2:].upper()
            idx += 1
            continue
        if tok in (
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-ascii",
            "--json",
        ) and idx + 1 < len(argv):
            data_indices.append((idx + 1, "separate"))
            idx += 2
            continue
        if tok.startswith("-d") and len(tok) > 2:
            data_indices.append((idx, "attached"))
            idx += 1
            continue
        matched_eq = False
        for prefix in ("--data=", "--data-raw=", "--data-binary=", "--data-ascii=", "--json="):
            if tok.startswith(prefix):
                data_indices.append((idx, "equals"))
                matched_eq = True
                break
        if matched_eq:
            idx += 1
            continue
        if tok in value_flags:
            idx += 2
            continue
        if tok.startswith(("http://", "https://")):
            url = tok
            idx += 1
            continue
        idx += 1

    if force_get and method is None:
        method = "GET"
    elif method is None and data_indices:
        method = "POST"

    exec_argv = list(argv)
    meta: dict[str, Any] = {}
    is_gh_post = False

    if url and method == "POST":
        parsed_url = urllib.parse.urlsplit(url)
        parsed_base = urllib.parse.urlsplit(configured_github_api_url)
        base_path = parsed_base.path.rstrip("/")

        origin_matches = (
            parsed_url.scheme.lower() == parsed_base.scheme.lower()
            and parsed_url.netloc.lower() == parsed_base.netloc.lower()
        )
        path_matches_prefix = parsed_url.path.startswith(f"{base_path}/") if base_path else True
        if origin_matches and path_matches_prefix:
            rel_path = parsed_url.path[len(base_path) :] if base_path else parsed_url.path
            m = _GITHUB_REST_ENDPOINT_RE.fullmatch(rel_path)
            if m:
                owner, repo = m.group(1), m.group(2)
                tail = m.group(3)
                issue_num = int(m.group(4)) if m.group(4) else None
                if tail == "pulls":
                    gh_kind = "pr"
                elif tail.startswith("issues/") and tail.endswith("/comments"):
                    gh_kind = "comment"
                else:
                    gh_kind = "issue"

                injected = False
                if len(data_indices) == 1:
                    d_idx, form = data_indices[0]
                    if form == "separate":
                        raw_val = exec_argv[d_idx]
                        if not raw_val.startswith("@"):
                            updated_json, injected = inject_marker_into_json_body(raw_val, marker)
                            if injected:
                                exec_argv[d_idx] = updated_json
                    elif form == "equals":
                        flag_part, raw_val = exec_argv[d_idx].split("=", 1)
                        if not raw_val.startswith("@"):
                            updated_json, injected = inject_marker_into_json_body(raw_val, marker)
                            if injected:
                                exec_argv[d_idx] = f"{flag_part}={updated_json}"
                    else:  # attached -d{...}
                        raw_val = exec_argv[d_idx][2:]
                        if not raw_val.startswith("@"):
                            updated_json, injected = inject_marker_into_json_body(raw_val, marker)
                            if injected:
                                exec_argv[d_idx] = f"-d{updated_json}"

                meta = {
                    "adapter": "github",
                    "kind": gh_kind,
                    "repo": f"{owner}/{repo}",
                    "api_base_url": configured_github_api_url,
                    "marker_injected": injected,
                }
                if issue_num is not None:
                    meta["issue_number"] = issue_num
                is_gh_post = True

    return url, meta, exec_argv, is_gh_post


def _build_command_plan(
    cmd: Sequence[str],
    *,
    actor: str,
    action_id: str,
    explicit_target: str | None,
    explicit_tool: str | None,
    cwd: Path,
    github_api_url: str | None = None,
    probe_git_remote: bool = True,
    probe_timeout_seconds: float = 15.0,
) -> _CommandPlan:
    if not cmd:
        raise ValueError("Wrapped command must not be empty")

    exe = os.path.basename(cmd[0]).lower()
    orig_argv = list(cmd)
    configured_gh_api = _resolve_configured_github_api_base(github_api_url)

    # 1. `gh` commands (reconcilable writes + all other `gh` commands treated safely as kind="gh")
    if exe in ("gh", "gh.exe"):
        sub1 = cmd[1].lower() if len(cmd) >= 2 else ""
        sub2 = cmd[2].lower() if len(cmd) >= 3 else ""
        gh_kind: str | None = None
        default_tool: str | None = None
        if sub1 == "issue" and sub2 == "create":
            gh_kind = "issue"
            default_tool = "gh_issue_create"
        elif sub1 == "pr" and sub2 == "create":
            gh_kind = "pr"
            default_tool = "gh_pr_create"
        elif sub1 == "issue" and sub2 == "comment":
            gh_kind = "comment"
            default_tool = "gh_issue_comment"
        elif sub1 == "pr" and sub2 == "comment":
            gh_kind = "comment"
            default_tool = "gh_pr_comment"

        if gh_kind is not None and default_tool is not None:
            repo, issue_number, repo_inferred, host_unverified = _extract_gh_repo_and_number(
                cmd,
                explicit_target=explicit_target,
                cwd=cwd,
                github_api_url=github_api_url,
            )
            target = (
                explicit_target.strip()
                if explicit_target and explicit_target.strip()
                else (
                    f"{repo}#{issue_number}"
                    if repo and issue_number is not None
                    else (repo or "github")
                )
            )
            tool_name = explicit_tool.strip() if explicit_tool else default_tool
            marker = derive_marker(actor, target, action_id)
            exec_argv, injected = inject_marker_into_gh_args(cmd, marker)
            meta: dict[str, Any] = {
                "adapter": "github",
                "kind": gh_kind,
                "marker_injected": injected,
                "repo_inferred": repo_inferred,
                "host_unverified": host_unverified,
            }
            if repo is not None:
                meta["repo"] = repo
            if issue_number is not None:
                meta["issue_number"] = issue_number
            payload = {
                "argv": orig_argv,
                "target": target,
                "tool_name": tool_name,
            }
            return _CommandPlan(
                tool_name=tool_name,
                target=target,
                display_summary=f"gh {sub1} {sub2} ({target})",
                meta=meta,
                payload=payload,
                exec_argv=exec_argv,
                is_reconcilable=True,
                kind="gh",
            )

        # Other `gh` subcommands (e.g., `gh api`, `gh pr merge`): non-reconcilable,
        # but STILL classified as kind="gh" so non-zero exits mark `unknown` (never double-write!).
        safe_sub = "_".join(
            re.sub(r"[^A-Za-z0-9._-]+", "_", part) for part in orig_argv[1:3] if part
        )
        tool_name = (
            explicit_tool.strip() if explicit_tool else (f"gh_{safe_sub}" if safe_sub else "gh")
        )
        target = (
            explicit_target.strip() if explicit_target and explicit_target.strip() else "github"
        )
        payload = {
            "argv": orig_argv,
            "target": target,
            "tool_name": tool_name,
        }
        return _CommandPlan(
            tool_name=tool_name,
            target=target,
            display_summary=f"gh {sub1} {sub2}".strip(),
            meta={},
            payload=payload,
            exec_argv=orig_argv,
            is_reconcilable=False,
            kind="gh",
        )

    # 2. `git push`
    push_spec = inspect_git_push(
        cmd,
        cwd=cwd,
        probe_remote=probe_git_remote,
        timeout_seconds=probe_timeout_seconds,
    )
    if push_spec is not None:
        tool_name = explicit_tool.strip() if explicit_tool else "git_push"
        safe_remote = redact_remote_for_display(push_spec.remote) if push_spec.remote else None
        default_target = (
            f"{safe_remote}:{push_spec.remote_ref}"
            if safe_remote and push_spec.remote_ref
            else (safe_remote or "git")
        )
        target = (
            explicit_target.strip()
            if explicit_target and explicit_target.strip()
            else default_target
        )
        payload: dict[str, Any] = {
            "argv": orig_argv,
            "target": target,
            "tool_name": tool_name,
            "remote": safe_remote,
            "remote_ref": push_spec.remote_ref,
            "expected_sha": push_spec.expected_sha,
            "is_delete": push_spec.is_delete,
        }
        return _CommandPlan(
            tool_name=tool_name,
            target=target,
            display_summary=f"git push {default_target}",
            meta=push_spec.to_meta(),
            payload=payload,
            exec_argv=orig_argv,
            is_reconcilable=push_spec.reconcilable,
            kind="git_push",
        )

    # 3. `curl` (including strictly validated GitHub REST POST detection)
    if exe in ("curl", "curl.exe"):
        probe_url, _, _, _ = _inspect_curl_command(
            cmd, marker="placeholder", configured_github_api_url=configured_gh_api
        )
        redacted_url = _redact_url_for_display(probe_url) if probe_url else None
        tool_name = explicit_tool.strip() if explicit_tool else "curl"
        target = (
            explicit_target.strip()
            if explicit_target and explicit_target.strip()
            else (redacted_url or "curl")
        )
        marker = derive_marker(actor, target, action_id)
        _, curl_meta, exec_argv, is_gh_post = _inspect_curl_command(
            cmd, marker=marker, configured_github_api_url=configured_gh_api
        )
        if is_gh_post and not explicit_tool:
            tool_name = "github_post"
        payload = {
            "argv": orig_argv,
            "target": target,
            "tool_name": tool_name,
        }
        return _CommandPlan(
            tool_name=tool_name,
            target=target,
            display_summary=f"curl {target}",
            meta=curl_meta,
            payload=payload,
            exec_argv=exec_argv,
            is_reconcilable=is_gh_post,
            kind="curl",
        )

    # 4. Generic CLI mutation (avoid logging raw CLI arguments that may contain secrets!)
    safe_tool = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(cmd[0])) or "cli_cmd"
    tool_name = explicit_tool.strip() if explicit_tool else safe_tool
    target = explicit_target.strip() if explicit_target and explicit_target.strip() else safe_tool
    payload = {
        "argv": orig_argv,
        "target": target,
        "tool_name": tool_name,
    }
    return _CommandPlan(
        tool_name=tool_name,
        target=target,
        display_summary=f"{safe_tool} ({len(orig_argv) - 1} arg(s))",
        meta={},
        payload=payload,
        exec_argv=orig_argv,
        is_reconcilable=False,
        kind="generic",
    )


def _extract_json_html_url(stdout: str) -> str | None:
    stripped = stdout.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    html_url = parsed.get("html_url")
    if isinstance(html_url, str) and html_url.strip().startswith(("http://", "https://")):
        return html_url.strip()
    return None


def _extract_result_ref(plan: _CommandPlan, stdout: str) -> tuple[str | None, bool]:
    """Extract ``(result_ref, confirmed_ok)`` from a zero-exit subprocess stdout.

    For recognized GitHub REST ``curl`` POSTs, ``curl`` exits 0 on HTTP 4xx/5xx unless
    ``-f`` was passed; therefore we confirm ONLY when a valid JSON response containing
    ``html_url`` is returned. Otherwise ``confirmed_ok=False`` so wrap marks ``unknown``
    instead of falsely confirming a rejected write!
    """
    if plan.kind == "git_push":
        remote = plan.meta.get("remote")
        remote_ref = plan.meta.get("remote_ref")
        expected_sha = plan.meta.get("expected_sha")
        if plan.meta.get("is_delete") and remote and remote_ref:
            return f"deleted {remote} {remote_ref}", True
        if remote and remote_ref and isinstance(expected_sha, str) and expected_sha:
            return f"{remote} {remote_ref}@{expected_sha[:12]}", True

    if plan.kind == "curl" and plan.meta.get("adapter") == "github":
        html_url = _extract_json_html_url(stdout)
        if html_url is not None:
            return html_url, True
        return None, False

    # Prefer JSON html_url over arbitrary URLs (such as API `url` or `documentation_url`).
    json_url = _extract_json_html_url(stdout)
    if json_url is not None:
        return json_url, True

    m_gh = _GH_ISSUE_URL_RE.search(stdout)
    if m_gh:
        return m_gh.group(0).rstrip(".,);]"), True

    m_url = _URL_RE.search(stdout)
    if m_url:
        return _redact_url_for_display(m_url.group(0).rstrip(".,);]")), True

    return None, True


def _is_ambiguous_nonzero_exit(plan: _CommandPlan, returncode: int) -> bool:
    """Decide whether a non-zero exit could have occurred AFTER a remote side effect landed.

    Safety rules:
      - Process killed by signal (``returncode < 0``) or shell signal/timeout exit
        (``returncode >= 128`` or ``124``) is ALWAYS ambiguous -> ``unknown``.
      - Wrapped ``gh`` and ``git push`` commands exiting non-zero are treated as
        ``unknown`` so post-write response-loss crashes are reconciled on retry rather
        than blindly re-executed.
      - Wrapped ``curl`` commands are ``failed`` ONLY for pre-connect/usage exit codes
        (2, 3, 5, 6, 7); all other curl non-zero exits are ``unknown``.
      - Generic non-reconcilable commands exiting ``1..125`` (excluding 124) are ``failed``.
    """
    if returncode < 0 or returncode >= 128 or returncode == EXIT_TIMEOUT:
        return True
    if plan.kind in ("gh", "git_push") or plan.is_reconcilable:
        return True
    if plan.kind == "curl":
        return returncode not in _CURL_PRE_CONNECT_EXIT_CODES
    return False


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a child process and its entire POSIX process group cleanly."""
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=0.5)
            # ALWAYS send SIGKILL to the process group even if the leader already exited
            # so any SIGTERM-ignoring grandchildren are terminated.
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)
            return

    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1.0)


def _run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> tuple[int | None, bytes, bytes, str | None, int | None]:
    """Execute ``argv`` in binary mode with process-group isolation and signal cleanup.

    Returns ``(returncode_or_none, stdout_bytes, stderr_bytes, error_kind, term_signum)``.
    Binary capture prevents ``UnicodeDecodeError`` from crashing after a side effect lands.
    """
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    installed_signals: list[tuple[int, Any]] = []
    if threading.current_thread() is threading.main_thread():

        def _on_signal(signum: int, _frame: Any) -> None:
            raise _SignalTermination(signum)

        for sig_name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    prev = signal.signal(sig, _on_signal)
                    installed_signals.append((sig, prev))
                except (OSError, ValueError):
                    pass

    try:
        try:
            proc = subprocess.Popen(list(argv), **popen_kwargs)
        except FileNotFoundError as exc:
            return None, b"", str(exc).encode("utf-8", errors="replace"), "not_found", None
        except PermissionError as exc:
            return None, b"", str(exc).encode("utf-8", errors="replace"), "permission", None
        except OSError as exc:
            return None, b"", str(exc).encode("utf-8", errors="replace"), "os_error", None

        try:
            out_b, err_b = proc.communicate(timeout=timeout_seconds)
            return proc.returncode, out_b or b"", err_b or b"", None, None
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                out_b, err_b = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                out_b, err_b = b"", b""
            return None, out_b or b"", err_b or b"", "timeout", None
        except _SignalTermination as exc:
            # Ignore a second SIGTERM/SIGHUP during cleanup so child-tree SIGKILL completes.
            for sig, _ in installed_signals:
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(sig, signal.SIG_IGN)
            _kill_process_tree(proc)
            try:
                out_b, err_b = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                out_b, err_b = b"", b""
            return None, out_b or b"", err_b or b"", "signal", exc.signum
        except KeyboardInterrupt:
            for sig, _ in installed_signals:
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(sig, signal.SIG_IGN)
            _kill_process_tree(proc)
            try:
                out_b, err_b = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                out_b, err_b = b"", b""
            return None, out_b or b"", err_b or b"", "interrupted", signal.SIGINT
        except BaseException:
            _kill_process_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=1.0)
            raise
    finally:
        for sig, prev in installed_signals:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, prev)


def _safe_finalize(
    journal: Journal,
    *,
    fn_name: str,
    actor: str,
    target: str,
    action_id: str,
    attempt: int,
    result_ref: str | None = None,
    error: str | None = None,
) -> tuple[ActionStatus | None, str | None, str | None]:
    """Apply an attempt-fenced journal finalizer, handling concurrent external transitions safely.

    Returns ``(final_status, final_result_ref, conflict_message_or_none)``.
    """
    try:
        if fn_name == "confirmed":
            rec = journal.mark_confirmed(
                actor, target, action_id, attempt=attempt, result_ref=result_ref
            )
        elif fn_name == "failed":
            rec = journal.mark_failed(actor, target, action_id, attempt=attempt, error=error)
        elif fn_name == "unknown":
            rec = journal.mark_unknown(actor, target, action_id, attempt=attempt, error=error)
        else:
            raise ValueError(f"Unknown finalizer: {fn_name}")
        return rec.status, rec.result_ref, None
    except (StaleAttemptError, InvalidStateTransitionError) as exc:
        latest = journal.get_action(actor, target, action_id)
        status = latest.status if latest is not None else None
        ref = latest.result_ref if latest is not None else result_ref
        return status, ref, f"Journal state changed concurrently during execution: {exc}"


def wrap_command(
    cmd: Sequence[str],
    *,
    journal: Journal,
    action_id: str | None = None,
    session_id: str = "default",
    actor: str = "agent",
    target: str | None = None,
    tool_name: str | None = None,
    timeout_seconds: float = 30.0,
    min_retry_age_seconds: float = DEFAULT_MIN_RETRY_AGE_SECONDS,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    reconcilers: Sequence[Reconciler] | None = None,
    github_api_url: str | None = None,
    auto_reconcile: bool = True,
) -> WrapResult:
    """Wrap a mutating CLI command with write-ahead journaling and safe reconciliation.

    Guarantees:
      - Writes a durable ``pending`` journal row BEFORE spawning any subprocess.
      - Rejects reused ``action_id`` with modified payload before dispatch.
      - Replays already ``confirmed`` / ``reconciled`` results without re-execution.
      - Never blindly re-executes a ``pending`` or ``unknown`` action:
        - Automatically reconciles (unless ``auto_reconcile=False``).
        - On ``FOUND``: marks ``reconciled`` and returns recovered ``result_ref`` (0 writes).
        - On ``NOT_FOUND`` for an already-terminated ``unknown`` action whose age is at
          least ``min_retry_age_seconds``: marks prior attempt ``failed`` (attempt-fenced)
          and re-claims atomically to retry once.
        - On ``PENDING`` action (or ``unknown`` younger than ``min_retry_age_seconds``):
          NEVER auto-retries on ``NOT_FOUND`` because a local worker or slow remote server
          may still be committing!
        - On ``UNKNOWN``: blocks retry and returns ``EXIT_OUTCOME_UNKNOWN``.
    """
    if not cmd:
        raise ValueError("Wrapped command must not be empty")
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if isinstance(min_retry_age_seconds, bool) or min_retry_age_seconds < 0:
        raise ValueError("min_retry_age_seconds must be >= 0")

    effective_cwd = (cwd if cwd is not None else Path.cwd()).resolve()
    resolved_action_id = action_id.strip() if action_id else f"cli-{uuid.uuid4().hex[:16]}"
    if not resolved_action_id:
        raise ValueError("action_id must not be empty")

    plan = _build_command_plan(
        cmd,
        actor=actor,
        action_id=resolved_action_id,
        explicit_target=target,
        explicit_tool=tool_name,
        cwd=effective_cwd,
        github_api_url=github_api_url,
        probe_git_remote=True,
        probe_timeout_seconds=min(timeout_seconds, 15.0),
    )

    summary_str = format_summary_with_meta(plan.display_summary, plan.meta)
    expected_marker = derive_marker(actor, plan.target, resolved_action_id)

    try:
        begin_res = journal.begin_action(
            action_id=resolved_action_id,
            session_id=session_id,
            actor=actor,
            surface=Surface.CLI,
            tool_name=plan.tool_name,
            target=plan.target,
            payload=plan.payload,
            marker=expected_marker,
            summary=summary_str,
        )
    except PayloadMismatchError as exc:
        return WrapResult(
            exit_code=EXIT_PAYLOAD_MISMATCH,
            action_id=resolved_action_id,
            marker=expected_marker,
            target=plan.target,
            tool_name=plan.tool_name,
            status=None,
            executed=False,
            error=str(exc),
        )

    record = begin_res.record

    if begin_res.decision == BeginDecision.REPLAY:
        out_text = f"{record.result_ref}\n" if record.result_ref else ""
        return WrapResult(
            exit_code=0,
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=record.status,
            executed=False,
            replayed=True,
            result_ref=record.result_ref,
            stdout=out_text,
            stdout_bytes=out_text.encode("utf-8"),
        )

    if begin_res.decision == BeginDecision.OUTCOME_UNKNOWN:
        if not auto_reconcile:
            msg = (
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={record.action_id}, marker={record.marker})"
            )
            return WrapResult(
                exit_code=EXIT_OUTCOME_UNKNOWN,
                action_id=record.action_id,
                marker=record.marker,
                target=record.target,
                tool_name=record.tool_name,
                status=record.status,
                executed=False,
                error=msg,
            )

        active_reconcilers = (
            list(reconcilers)
            if reconcilers is not None
            else default_reconcilers(
                github_api_url=github_api_url,
                timeout_seconds=min(timeout_seconds, 15.0),
            )
        )

        # CRITICAL CONCURRENCY & SLOW-REMOTE SAFETY:
        # 1. Never transition NOT_FOUND -> failed when `status == PENDING` (live worker!).
        # 2. For `status == UNKNOWN`, require the row to be at least `min_retry_age_seconds`
        #    old so an in-flight slow remote request has time to settle before re-dispatch.
        age_seconds = _record_age_seconds(record)
        cooldown_active = (
            record.status == ActionStatus.UNKNOWN
            and min_retry_age_seconds > 0
            and (age_seconds is None or age_seconds < min_retry_age_seconds)
        )
        allow_retry_on_not_found = record.status == ActionStatus.UNKNOWN and not cooldown_active
        outcome = reconcile_action(
            journal,
            record,
            reconcilers=active_reconcilers,
            dry_run=False,
            mark_not_found_failed=allow_retry_on_not_found,
            mark_pending_not_found_failed=False,
        )

        if outcome.result.state == ReconcileState.FOUND:
            final_rec = outcome.updated_record or record
            ref = outcome.result.result_ref or final_rec.result_ref
            out_text = f"{ref}\n" if ref else ""
            return WrapResult(
                exit_code=0,
                action_id=final_rec.action_id,
                marker=final_rec.marker,
                target=final_rec.target,
                tool_name=final_rec.tool_name,
                status=final_rec.status,
                executed=False,
                reconciled=True,
                result_ref=ref,
                stdout=out_text,
                stdout_bytes=out_text.encode("utf-8"),
                reconcile_result=outcome.result,
            )

        if (
            outcome.result.state == ReconcileState.NOT_FOUND
            and allow_retry_on_not_found
            and outcome.applied
        ):
            retry_begin = journal.begin_action(
                action_id=resolved_action_id,
                session_id=session_id,
                actor=actor,
                surface=Surface.CLI,
                tool_name=plan.tool_name,
                target=plan.target,
                payload=plan.payload,
                marker=expected_marker,
                summary=summary_str,
            )
            if retry_begin.decision == BeginDecision.PROCEED:
                record = retry_begin.record
            elif retry_begin.decision == BeginDecision.REPLAY:
                rec = retry_begin.record
                out_text = f"{rec.result_ref}\n" if rec.result_ref else ""
                return WrapResult(
                    exit_code=0,
                    action_id=rec.action_id,
                    marker=rec.marker,
                    target=rec.target,
                    tool_name=rec.tool_name,
                    status=rec.status,
                    executed=False,
                    replayed=True,
                    result_ref=rec.result_ref,
                    stdout=out_text,
                    stdout_bytes=out_text.encode("utf-8"),
                    reconcile_result=outcome.result,
                )
            else:
                rec_retry = retry_begin.record
                msg = (
                    f"OUTCOME UNKNOWN — concurrent worker claimed retry "
                    f"(action_id={rec_retry.action_id}, marker={rec_retry.marker})"
                )
                return WrapResult(
                    exit_code=EXIT_OUTCOME_UNKNOWN,
                    action_id=rec_retry.action_id,
                    marker=rec_retry.marker,
                    target=rec_retry.target,
                    tool_name=rec_retry.tool_name,
                    status=rec_retry.status,
                    executed=False,
                    error=msg,
                    reconcile_result=outcome.result,
                )
        else:
            if record.status == ActionStatus.PENDING:
                extra = " (action is still pending; reconcile explicitly before retrying)"
            elif cooldown_active:
                age_desc = f"{age_seconds:.1f}s" if age_seconds is not None else "unknown"
                extra = (
                    f" (recently marked unknown {age_desc} ago; wait for "
                    f"{min_retry_age_seconds:g}s cooldown before retrying so in-flight "
                    "remote writes can settle, or pass --min-retry-age 0)"
                )
            else:
                extra = f" ({outcome.result.details})" if outcome.result.details else ""
            msg = (
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={record.action_id}, marker={record.marker}){extra}"
            )
            return WrapResult(
                exit_code=EXIT_OUTCOME_UNKNOWN,
                action_id=record.action_id,
                marker=record.marker,
                target=record.target,
                tool_name=record.tool_name,
                status=record.status,
                executed=False,
                error=msg,
                reconcile_result=outcome.result,
            )

    # BeginDecision.PROCEED (or safe retry after proven NOT_FOUND)
    attempt = record.attempt
    try:
        returncode, out_bytes, err_bytes, err_kind, term_signum = _run_subprocess(
            plan.exec_argv,
            cwd=effective_cwd,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    except BaseException as exc:
        _safe_finalize(
            journal,
            fn_name="unknown",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            error=f"Unexpected wrapper error during execution: {exc}",
        )
        raise

    stdout = out_bytes.decode("utf-8", errors="replace")
    stderr = err_bytes.decode("utf-8", errors="replace")

    if err_kind in ("not_found", "permission", "os_error"):
        err_msg = stderr or f"Failed to start command: {err_kind}"
        status, _, conflict = _safe_finalize(
            journal,
            fn_name="failed",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            error=err_msg,
        )
        code = (
            EXIT_NOT_FOUND
            if err_kind == "not_found"
            else (EXIT_NOT_EXECUTABLE if err_kind == "permission" else 1)
        )
        return WrapResult(
            exit_code=code,
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=status,
            executed=False,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=out_bytes,
            stderr_bytes=err_bytes,
            error=conflict or err_msg,
        )

    if err_kind == "timeout":
        timeout_msg = f"Command timed out after {timeout_seconds:g}s"
        status, _, conflict = _safe_finalize(
            journal,
            fn_name="unknown",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            error=timeout_msg,
        )
        err_out = (
            f"OUTCOME UNKNOWN — reconcile before retrying "
            f"(action_id={record.action_id}, marker={record.marker}): {timeout_msg}"
        )
        if conflict:
            err_out = f"{err_out} ({conflict})"
        return WrapResult(
            exit_code=EXIT_TIMEOUT,
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=out_bytes,
            stderr_bytes=err_bytes,
            error=err_out,
        )

    if err_kind in ("interrupted", "signal"):
        sig_num = term_signum if term_signum is not None else signal.SIGINT
        sig_msg = f"Terminated by signal {sig_num} during execution"
        status, _, conflict = _safe_finalize(
            journal,
            fn_name="unknown",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            error=sig_msg,
        )
        err_out = (
            f"OUTCOME UNKNOWN — reconcile before retrying "
            f"(action_id={record.action_id}, marker={record.marker}): {sig_msg}"
        )
        if conflict:
            err_out = f"{err_out} ({conflict})"
        return WrapResult(
            exit_code=128 + sig_num,
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=out_bytes,
            stderr_bytes=err_bytes,
            error=err_out,
        )

    assert returncode is not None
    if returncode == 0:
        result_ref, confirmed_ok = _extract_result_ref(plan, stdout)
        if not confirmed_ok:
            err_msg = (
                "Command exited 0 but response did not confirm a created GitHub resource "
                "(missing html_url)"
            )
            status, _, conflict = _safe_finalize(
                journal,
                fn_name="unknown",
                actor=actor,
                target=plan.target,
                action_id=resolved_action_id,
                attempt=attempt,
                error=err_msg,
            )
            err_out = (
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={record.action_id}, marker={record.marker}): {err_msg}"
            )
            if conflict:
                err_out = f"{err_out} ({conflict})"
            return WrapResult(
                exit_code=EXIT_OUTCOME_UNKNOWN,
                action_id=record.action_id,
                marker=record.marker,
                target=record.target,
                tool_name=record.tool_name,
                status=status,
                executed=True,
                stdout=stdout,
                stderr=stderr,
                stdout_bytes=out_bytes,
                stderr_bytes=err_bytes,
                error=err_out,
            )

        status, final_ref, conflict = _safe_finalize(
            journal,
            fn_name="confirmed",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            result_ref=result_ref,
        )
        # If another worker reconciled or confirmed the exact same action concurrently,
        # the side effect succeeded and is durably recorded — return 0 without error.
        benign_conflict = conflict is not None and status in (
            ActionStatus.CONFIRMED,
            ActionStatus.RECONCILED,
        )
        return WrapResult(
            exit_code=0 if (conflict is None or benign_conflict) else 1,
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=status,
            executed=True,
            result_ref=final_ref,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=out_bytes,
            stderr_bytes=err_bytes,
            error=None if benign_conflict else conflict,
        )

    # Non-zero exit code: classify ambiguous crash/transport errors as `unknown`.
    err_detail = (stderr.strip().splitlines()[-1] if stderr.strip() else None) or (
        f"Command exited with code {returncode}"
    )
    if _is_ambiguous_nonzero_exit(plan, returncode):
        status, _, conflict = _safe_finalize(
            journal,
            fn_name="unknown",
            actor=actor,
            target=plan.target,
            action_id=resolved_action_id,
            attempt=attempt,
            error=f"exit {returncode}: {err_detail}",
        )
        err_out = (
            f"OUTCOME UNKNOWN — reconcile before retrying "
            f"(action_id={record.action_id}, marker={record.marker}): {err_detail}"
        )
        if conflict:
            err_out = f"{err_out} ({conflict})"
        return WrapResult(
            exit_code=returncode if returncode > 0 else 128 + abs(returncode),
            action_id=record.action_id,
            marker=record.marker,
            target=record.target,
            tool_name=record.tool_name,
            status=status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=out_bytes,
            stderr_bytes=err_bytes,
            error=err_out,
        )

    status, _, conflict = _safe_finalize(
        journal,
        fn_name="failed",
        actor=actor,
        target=plan.target,
        action_id=resolved_action_id,
        attempt=attempt,
        error=f"exit {returncode}: {err_detail}",
    )
    return WrapResult(
        exit_code=returncode,
        action_id=record.action_id,
        marker=record.marker,
        target=record.target,
        tool_name=record.tool_name,
        status=status,
        executed=True,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=out_bytes,
        stderr_bytes=err_bytes,
        error=conflict or f"exit {returncode}: {err_detail}",
    )


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {raw!r}") from exc
    if value <= 0 or value != value:
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


def _build_wrap_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidefx wrap",
        description="Wrap a mutating CLI command with write-ahead journaling and reconciliation.",
    )
    parser.add_argument("--db", dest="db_path", default=None, help="Path to SQLite journal DB.")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to .sidefx.json.")
    parser.add_argument("--session-id", dest="session_id", default=None, help="Session identifier.")
    parser.add_argument(
        "--action-id",
        dest="action_id",
        default=None,
        help="Deterministic idempotency action ID (auto-generated if omitted).",
    )
    parser.add_argument(
        "--target",
        dest="target",
        default=None,
        help="External target identifier (auto-detected for gh/git push/curl if omitted).",
    )
    parser.add_argument(
        "--tool",
        dest="tool_name",
        default=None,
        help="Override recorded tool name.",
    )
    parser.add_argument(
        "--actor",
        dest="actor",
        default=None,
        help="Override actor name (default from config).",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout_seconds",
        type=_positive_float,
        default=None,
        help="Command execution timeout in seconds (default from config).",
    )
    parser.add_argument(
        "--min-retry-age",
        dest="min_retry_age_seconds",
        type=_non_negative_float,
        default=DEFAULT_MIN_RETRY_AGE_SECONDS,
        help=(
            "Minimum age in seconds since an unknown outcome before NOT_FOUND may trigger "
            f"automatic retry (default: {DEFAULT_MIN_RETRY_AGE_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--github-api-url",
        dest="github_api_url",
        default=None,
        help="Override GitHub REST API base URL for automatic reconciliation.",
    )
    parser.add_argument(
        "--no-reconcile",
        dest="no_reconcile",
        action="store_true",
        help="Disable automatic reconciliation when an existing action is pending/unknown.",
    )
    return parser


def _split_wrap_argv(raw_args: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``sidefx wrap`` args at the first ``--`` so wrapped command args stay verbatim."""
    raw = list(raw_args)
    if "--" in raw:
        sep = raw.index("--")
        return raw[:sep], raw[sep + 1 :]
    return raw, []


def _write_stream(stream: Any, raw_bytes: bytes, text_fallback: str) -> None:
    if not raw_bytes and not text_fallback:
        return
    buf = getattr(stream, "buffer", None)
    if buf is not None and raw_bytes:
        try:
            buf.write(raw_bytes)
            buf.flush()
            return
        except (OSError, AttributeError):
            pass
    if text_fallback:
        stream.write(text_fallback)
        stream.flush()


def handle_wrap_cli(args: argparse.Namespace, config: SidefxConfig) -> int:
    """CLI handler for ``sidefx wrap``."""
    raw_extras = list(getattr(args, "args", []))
    wrapper_tokens, cmd_tokens = _split_wrap_argv(raw_extras)

    parser = _build_wrap_parser()
    if not cmd_tokens:
        parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute.")
        sub_args = parser.parse_args(wrapper_tokens)
        cmd_tokens = list(sub_args.cmd)
    else:
        sub_args = parser.parse_args(wrapper_tokens)

    if not cmd_tokens:
        print("error: sidefx wrap requires a command after '--'", file=sys.stderr)
        return 2

    effective_config = load_config(sub_args.config_path) if sub_args.config_path else config
    db_raw = sub_args.db_path or getattr(args, "db_path", None)
    db_path = resolve_db_path(db_raw, config=effective_config)
    session_id = (
        sub_args.session_id
        or getattr(args, "session_id", None)
        or effective_config.default_session_id
        or os.environ.get("SIDEFX_SESSION_ID", "").strip()
        or "default"
    )
    actor = sub_args.actor or effective_config.default_actor
    timeout_seconds = (
        sub_args.timeout_seconds
        if sub_args.timeout_seconds is not None
        else effective_config.default_timeout_seconds
    )

    with Journal(db_path) as journal:
        res = wrap_command(
            cmd_tokens,
            journal=journal,
            action_id=sub_args.action_id,
            session_id=session_id,
            actor=actor,
            target=sub_args.target,
            tool_name=sub_args.tool_name,
            timeout_seconds=timeout_seconds,
            min_retry_age_seconds=sub_args.min_retry_age_seconds,
            github_api_url=sub_args.github_api_url,
            auto_reconcile=not sub_args.no_reconcile,
        )

    _write_stream(sys.stdout, res.stdout_bytes, res.stdout)
    _write_stream(sys.stderr, res.stderr_bytes, res.stderr)

    if res.replayed:
        print(
            f"sidefx: replayed {res.status.value if res.status else 'confirmed'} "
            f"action {res.action_id}",
            file=sys.stderr,
        )
    elif res.reconciled:
        print(
            f"sidefx: reconciled action {res.action_id} without re-executing",
            file=sys.stderr,
        )
    elif res.error and res.exit_code != 0 and res.error.strip() != res.stderr.strip():
        print(res.error, file=sys.stderr)

    return res.exit_code
