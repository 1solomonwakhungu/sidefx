"""Write-ahead journaled CLI mutation wrapper (`sidefx wrap`)."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
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
    Journal,
    PayloadMismatchError,
    Surface,
    derive_marker,
    resolve_db_path,
)
from sidefx.reconcilers import (
    Reconciler,
    ReconcileResult,
    ReconcileState,
    default_reconcilers,
    format_summary_with_meta,
    inject_marker_into_gh_args,
    inject_marker_into_json_body,
    inspect_git_push,
    reconcile_action,
)
from sidefx.reconcilers.github import parse_github_repo

EXIT_PAYLOAD_MISMATCH = 2
EXIT_OUTCOME_UNKNOWN = 75
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_GH_ISSUE_URL_RE = re.compile(r"https?://[^\s'\"<>]+/(?:issues|pull)/\d+(?:#issuecomment-\d+)?")
_GITHUB_API_PATH_RE = re.compile(
    r"/repos/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/"
    r"(issues(?:/(\d+)/comments)?|pulls)(?:[/?#]|$)"
)
_GIT_SSH_REPO_RE = re.compile(
    r"^(?:git@|ssh://git@)[^/:]+[:/]([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?$"
)
_GIT_HTTPS_REPO_RE = re.compile(r"^https?://[^/]+/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")

# Curl exit codes that prove failure occurred BEFORE sending any request payload.
_CURL_PRE_CONNECT_EXIT_CODES = frozenset({2, 3, 5, 6, 7})


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


def _detect_git_origin_repo(cwd: Path) -> str | None:
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
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    for pattern in (_GIT_SSH_REPO_RE, _GIT_HTTPS_REPO_RE):
        m = pattern.match(raw)
        if m:
            parsed = parse_github_repo(m.group(1))
            if parsed is not None:
                return f"{parsed[0]}/{parsed[1]}"
    return None


def _extract_gh_repo_and_number(
    argv: Sequence[str], *, explicit_target: str | None, cwd: Path
) -> tuple[str | None, int | None]:
    repo: str | None = None
    issue_number: int | None = None

    idx = 3
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--":
            break
        if tok in ("-R", "--repo") and idx + 1 < len(argv):
            parsed = parse_github_repo(argv[idx + 1])
            if parsed is not None:
                repo = f"{parsed[0]}/{parsed[1]}"
            idx += 2
            continue
        if tok.startswith("--repo="):
            parsed = parse_github_repo(tok.split("=", 1)[1])
            if parsed is not None:
                repo = f"{parsed[0]}/{parsed[1]}"
            idx += 1
            continue
        if tok.startswith("-R="):
            parsed = parse_github_repo(tok.split("=", 1)[1])
            if parsed is not None:
                repo = f"{parsed[0]}/{parsed[1]}"
            idx += 1
            continue
        idx += 1

    # Positional issue/PR number or URL for `gh issue comment <num|url>` / `gh pr comment <num|url>`
    if len(argv) >= 4 and argv[2].lower() == "comment":
        # Skip flags with values to find first positional argument.
        value_flags = {
            "-b",
            "--body",
            "-F",
            "--body-file",
            "-R",
            "--repo",
        }
        p_idx = 3
        while p_idx < len(argv):
            tok = argv[p_idx]
            if tok == "--":
                if p_idx + 1 < len(argv):
                    cand = argv[p_idx + 1]
                    if cand.isdigit() and int(cand) > 0:
                        issue_number = int(cand)
                break
            if tok in value_flags:
                p_idx += 2
                continue
            if tok.startswith("-"):
                p_idx += 1
                continue
            if tok.isdigit() and int(tok) > 0:
                issue_number = int(tok)
                break
            m = re.search(r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/(?:issues|pull)/(\d+)", tok)
            if m:
                if repo is None:
                    repo = f"{m.group(1)}/{m.group(2)}"
                issue_number = int(m.group(3))
                break
            p_idx += 1

    if repo is None and explicit_target:
        parsed = parse_github_repo(explicit_target)
        if parsed is not None:
            repo = f"{parsed[0]}/{parsed[1]}"
        m_num = re.search(r"#(\d+)$", explicit_target.strip())
        if issue_number is None and m_num:
            issue_number = int(m_num.group(1))

    if repo is None:
        env_repo = os.environ.get("GH_REPO", "").strip()
        if env_repo:
            parsed = parse_github_repo(env_repo)
            if parsed is not None:
                repo = f"{parsed[0]}/{parsed[1]}"

    if repo is None:
        repo = _detect_git_origin_repo(cwd)

    return repo, issue_number


def _inspect_curl_command(
    argv: Sequence[str], *, marker: str
) -> tuple[str | None, dict[str, Any], list[str], bool]:
    """Detect ``curl`` target URL and optional GitHub REST POST marker injection."""
    url: str | None = None
    method: str | None = None
    data_indices: list[tuple[int, str]] = []  # (idx, form: "separate" | "equals")
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
        if tok in ("-X", "--request") and idx + 1 < len(argv):
            method = argv[idx + 1].upper()
            idx += 2
            continue
        if tok.startswith("--request="):
            method = tok.split("=", 1)[1].upper()
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
        for prefix in ("--data=", "--data-raw=", "--data-binary=", "--data-ascii=", "--json="):
            if tok.startswith(prefix):
                data_indices.append((idx, "equals"))
                break
        if tok in value_flags:
            idx += 2
            continue
        if tok.startswith("http://") or tok.startswith("https://"):
            url = tok
            idx += 1
            continue
        idx += 1

    if method is None and data_indices:
        method = "POST"

    exec_argv = list(argv)
    meta: dict[str, Any] = {}
    is_gh_post = False

    if url and method == "POST":
        parsed = urllib.parse.urlsplit(url)
        m = _GITHUB_API_PATH_RE.search(parsed.path)
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
                else:
                    flag_part, raw_val = exec_argv[d_idx].split("=", 1)
                    if not raw_val.startswith("@"):
                        updated_json, injected = inject_marker_into_json_body(raw_val, marker)
                        if injected:
                            exec_argv[d_idx] = f"{flag_part}={updated_json}"

            meta = {
                "adapter": "github",
                "kind": gh_kind,
                "repo": f"{owner}/{repo}",
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
    probe_git_remote: bool = True,
    probe_timeout_seconds: float = 15.0,
) -> _CommandPlan:
    if not cmd:
        raise ValueError("Wrapped command must not be empty")

    exe = os.path.basename(cmd[0]).lower()
    orig_argv = list(cmd)

    # 1. `gh` write commands
    if exe in ("gh", "gh.exe") and len(cmd) >= 3:
        sub1 = cmd[1].lower()
        sub2 = cmd[2].lower()
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
            repo, issue_number = _extract_gh_repo_and_number(
                cmd, explicit_target=explicit_target, cwd=cwd
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

    # 2. `git push`
    push_spec = inspect_git_push(
        cmd,
        cwd=cwd,
        probe_remote=probe_git_remote,
        timeout_seconds=probe_timeout_seconds,
    )
    if push_spec is not None:
        tool_name = explicit_tool.strip() if explicit_tool else "git_push"
        default_target = (
            f"{push_spec.remote}:{push_spec.remote_ref}"
            if push_spec.remote and push_spec.remote_ref
            else (push_spec.remote or "git")
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
            "remote": push_spec.remote,
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

    # 3. `curl` (including GitHub REST POST detection)
    if exe in ("curl", "curl.exe"):
        # Compute preliminary target before marker derivation.
        # Inspect once with placeholder to extract URL, then derive real marker.
        probe_url, _, _, _ = _inspect_curl_command(cmd, marker="placeholder")
        tool_name = explicit_tool.strip() if explicit_tool else "curl"
        target = (
            explicit_target.strip()
            if explicit_target and explicit_target.strip()
            else (probe_url or "curl")
        )
        marker = derive_marker(actor, target, action_id)
        _, curl_meta, exec_argv, is_gh_post = _inspect_curl_command(cmd, marker=marker)
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

    # 4. Generic CLI mutation
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
        display_summary=" ".join(orig_argv[:4]),
        meta={},
        payload=payload,
        exec_argv=orig_argv,
        is_reconcilable=False,
        kind="generic",
    )


def _extract_result_ref(plan: _CommandPlan, stdout: str) -> str | None:
    if plan.kind == "git_push":
        remote = plan.meta.get("remote")
        remote_ref = plan.meta.get("remote_ref")
        expected_sha = plan.meta.get("expected_sha")
        if plan.meta.get("is_delete") and remote and remote_ref:
            return f"deleted {remote} {remote_ref}"
        if remote and remote_ref and isinstance(expected_sha, str) and expected_sha:
            return f"{remote} {remote_ref}@{expected_sha[:12]}"

    m_gh = _GH_ISSUE_URL_RE.search(stdout)
    if m_gh:
        return m_gh.group(0).rstrip(".,);]")

    m_url = _URL_RE.search(stdout)
    if m_url:
        return m_url.group(0).rstrip(".,);]")

    # For JSON responses (e.g. curl POST to GitHub REST API), extract `html_url` if present.
    stripped = stdout.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        import json

        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                html_url = parsed.get("html_url")
                if isinstance(html_url, str) and html_url.strip():
                    return html_url.strip()
        except (ValueError, RecursionError):
            pass
    return None


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


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a child process and its POSIX process group cleanly."""
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=0.25)
                return
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
            return

    with contextlib.suppress(OSError):
        proc.kill()


def _run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str, str | None]:
    """Execute ``argv`` with process-group isolation and timeout cleanup.

    Returns ``(returncode_or_none, stdout, stderr, error_kind)`` where ``error_kind`` is:
      - ``None`` when the process exited normally or by signal.
      - ``"timeout"`` on timeout.
      - ``"not_found"`` on ``FileNotFoundError``.
      - ``"permission"`` on ``PermissionError``.
      - ``"os_error"`` on other ``OSError`` before spawn.
      - ``"interrupted"`` on ``KeyboardInterrupt``.
    """
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(list(argv), **popen_kwargs)
    except FileNotFoundError as exc:
        return None, "", str(exc), "not_found"
    except PermissionError as exc:
        return None, "", str(exc), "permission"
    except OSError as exc:
        return None, "", str(exc), "os_error"

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, stdout, stderr, None
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return None, stdout, stderr, "timeout"
    except KeyboardInterrupt:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return None, stdout, stderr, "interrupted"


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
        - On ``NOT_FOUND`` for an already-terminated ``unknown`` action: marks prior
          attempt ``failed`` (attempt-fenced) and re-claims atomically to retry once.
        - On ``PENDING`` action: NEVER auto-retries even on ``NOT_FOUND`` because a
          concurrent worker may still be in flight!
        - On ``UNKNOWN``: blocks retry and returns ``EXIT_OUTCOME_UNKNOWN``.
    """
    if not cmd:
        raise ValueError("Wrapped command must not be empty")
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

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

        # CRITICAL CONCURRENCY SAFETY:
        # Only transition NOT_FOUND -> failed when the prior attempt already terminated
        # (`status == UNKNOWN`). If `status == PENDING`, a live worker may still be in flight!
        allow_retry_on_not_found = record.status == ActionStatus.UNKNOWN
        outcome = reconcile_action(
            journal,
            record,
            reconcilers=active_reconcilers,
            dry_run=False,
            mark_not_found_failed=allow_retry_on_not_found,
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
                reconcile_result=outcome.result,
            )

        if (
            outcome.result.state == ReconcileState.NOT_FOUND
            and allow_retry_on_not_found
            and outcome.applied
        ):
            # Re-claim atomically with incremented attempt fence.
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
                    stdout=f"{rec.result_ref}\n" if rec.result_ref else "",
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
                    action_id=retry_begin.record.action_id,
                    marker=retry_begin.record.marker,
                    target=retry_begin.record.target,
                    tool_name=retry_begin.record.tool_name,
                    status=retry_begin.record.status,
                    executed=False,
                    error=msg,
                    reconcile_result=outcome.result,
                )
        else:
            extra = (
                " (action is still pending; reconcile explicitly before retrying)"
                if record.status == ActionStatus.PENDING
                else (f" ({outcome.result.details})" if outcome.result.details else "")
            )
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
    returncode, stdout, stderr, err_kind = _run_subprocess(
        plan.exec_argv,
        cwd=effective_cwd,
        timeout_seconds=timeout_seconds,
        env=env,
    )

    if err_kind in ("not_found", "permission", "os_error"):
        updated = journal.mark_failed(
            actor,
            plan.target,
            resolved_action_id,
            attempt=attempt,
            error=stderr or f"Failed to start command: {err_kind}",
        )
        code = (
            EXIT_NOT_FOUND
            if err_kind == "not_found"
            else (EXIT_NOT_EXECUTABLE if err_kind == "permission" else 1)
        )
        return WrapResult(
            exit_code=code,
            action_id=updated.action_id,
            marker=updated.marker,
            target=updated.target,
            tool_name=updated.tool_name,
            status=updated.status,
            executed=False,
            stdout=stdout,
            stderr=stderr,
            error=updated.error,
        )

    if err_kind == "timeout":
        timeout_msg = f"Command timed out after {timeout_seconds:g}s"
        updated = journal.mark_unknown(
            actor,
            plan.target,
            resolved_action_id,
            attempt=attempt,
            error=timeout_msg,
        )
        return WrapResult(
            exit_code=EXIT_TIMEOUT,
            action_id=updated.action_id,
            marker=updated.marker,
            target=updated.target,
            tool_name=updated.tool_name,
            status=updated.status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            error=(
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={updated.action_id}, marker={updated.marker}): {timeout_msg}"
            ),
        )

    if err_kind == "interrupted":
        updated = journal.mark_unknown(
            actor,
            plan.target,
            resolved_action_id,
            attempt=attempt,
            error="Interrupted by KeyboardInterrupt during execution",
        )
        return WrapResult(
            exit_code=130,
            action_id=updated.action_id,
            marker=updated.marker,
            target=updated.target,
            tool_name=updated.tool_name,
            status=updated.status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            error=(
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={updated.action_id}, marker={updated.marker}): interrupted"
            ),
        )

    assert returncode is not None
    if returncode == 0:
        result_ref = _extract_result_ref(plan, stdout)
        updated = journal.mark_confirmed(
            actor,
            plan.target,
            resolved_action_id,
            attempt=attempt,
            result_ref=result_ref,
        )
        return WrapResult(
            exit_code=0,
            action_id=updated.action_id,
            marker=updated.marker,
            target=updated.target,
            tool_name=updated.tool_name,
            status=updated.status,
            executed=True,
            result_ref=updated.result_ref,
            stdout=stdout,
            stderr=stderr,
        )

    # Non-zero exit code: carefully classify ambiguous crash/transport errors as `unknown`.
    err_detail = (stderr.strip().splitlines()[-1] if stderr.strip() else None) or (
        f"Command exited with code {returncode}"
    )
    if _is_ambiguous_nonzero_exit(plan, returncode):
        updated = journal.mark_unknown(
            actor,
            plan.target,
            resolved_action_id,
            attempt=attempt,
            error=f"exit {returncode}: {err_detail}",
        )
        return WrapResult(
            exit_code=returncode if returncode > 0 else 128 + abs(returncode),
            action_id=updated.action_id,
            marker=updated.marker,
            target=updated.target,
            tool_name=updated.tool_name,
            status=updated.status,
            executed=True,
            stdout=stdout,
            stderr=stderr,
            error=(
                f"OUTCOME UNKNOWN — reconcile before retrying "
                f"(action_id={updated.action_id}, marker={updated.marker}): {err_detail}"
            ),
        )

    updated = journal.mark_failed(
        actor,
        plan.target,
        resolved_action_id,
        attempt=attempt,
        error=f"exit {returncode}: {err_detail}",
    )
    return WrapResult(
        exit_code=returncode,
        action_id=updated.action_id,
        marker=updated.marker,
        target=updated.target,
        tool_name=updated.tool_name,
        status=updated.status,
        executed=True,
        stdout=stdout,
        stderr=stderr,
        error=updated.error,
    )


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {raw!r}") from exc
    if value <= 0 or value != value:
        raise argparse.ArgumentTypeError("value must be > 0")
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
            github_api_url=sub_args.github_api_url,
            auto_reconcile=not sub_args.no_reconcile,
        )

    if res.stdout:
        sys.stdout.write(res.stdout)
        sys.stdout.flush()
    if res.stderr:
        sys.stderr.write(res.stderr)
        sys.stderr.flush()

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
    elif res.error and res.exit_code != 0:
        print(res.error, file=sys.stderr)

    return res.exit_code
