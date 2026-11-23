"""Git push command inspector and read-only ``git ls-remote`` reconciler."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidefx.journal import ActionRecord
from sidefx.reconcilers.base import ReconcileResult, ReconcileState, parse_summary_meta

DEFAULT_GIT_TIMEOUT = 15.0
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_PUSH_TOOLS = frozenset({"git_push", "push_branch", "git-push"})

# Global git flags that consume the next argument token before the subcommand.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--exec-path",
    }
)

# `git push` flags that consume a separate following argument token.
_GIT_PUSH_VALUE_FLAGS = frozenset(
    {
        "--repo",
        "--receive-pack",
        "--exec",
        "-o",
        "--push-option",
        "--recurse-submodules",
    }
)


@dataclass(frozen=True, slots=True)
class GitPushSpec:
    """Parsed and pre-inspected details of a ``git push`` invocation."""

    cwd: str
    remote: str | None
    remote_ref: str | None
    expected_sha: str | None
    before_sha: str | None
    before_known: bool
    is_delete: bool
    is_force: bool
    reconcilable: bool
    reason: str | None = None

    def to_meta(self) -> dict[str, Any]:
        """Serialize into compact metadata for durable storage in the journal."""
        meta: dict[str, Any] = {
            "adapter": "git_push",
            "cwd": self.cwd,
            "is_delete": self.is_delete,
            "is_force": self.is_force,
            "reconcilable": self.reconcilable,
            "before_known": self.before_known,
        }
        if self.remote is not None:
            meta["remote"] = self.remote
        if self.remote_ref is not None:
            meta["remote_ref"] = self.remote_ref
        if self.expected_sha is not None:
            meta["expected_sha"] = self.expected_sha
        if self.before_sha is not None:
            meta["before_sha"] = self.before_sha
        if self.reason is not None:
            meta["reason"] = self.reason
        return meta


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(
    args: Sequence[str], *, cwd: Path, timeout_seconds: float = DEFAULT_GIT_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ls_remote_ref(
    remote: str,
    remote_ref: str,
    *,
    cwd: Path,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT,
) -> tuple[bool, str | None, str | None]:
    """Query a single remote ref read-only via ``git ls-remote``.

    Returns ``(ok, sha_or_none, error_message)``.
      - ``(True, "<40-hex-sha>", None)`` -> ref exists with exact SHA.
      - ``(True, None, None)`` -> command succeeded cleanly and ref is absent.
      - ``(False, None, "<error>")`` -> command failed, timed out, or returned ambiguous output.
    """
    proc = _run_git(
        ["ls-remote", "--", remote, remote_ref],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    if proc is None:
        return False, None, f"git ls-remote failed or timed out for {remote} {remote_ref}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit code {proc.returncode}"
        return False, None, f"git ls-remote failed ({err})"

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return True, None, None

    matched_shas: list[str] = []
    for line in lines:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            return False, None, f"Malformed git ls-remote output line: {line!r}"
        sha, ref_name = parts[0].strip().lower(), parts[1].strip()
        if not _HEX_SHA_RE.match(sha):
            return False, None, f"Invalid SHA in git ls-remote output: {sha!r}"
        if ref_name == remote_ref:
            matched_shas.append(sha)

    if len(matched_shas) == 1:
        return True, matched_shas[0], None
    if len(matched_shas) == 0:
        # `git ls-remote` returned refs (e.g. suffix match) none of which matched exact refname.
        return True, None, None
    return False, None, f"Ambiguous git ls-remote output for {remote_ref}"


def is_git_push_command(argv: Sequence[str]) -> bool:
    """Return True if ``argv`` invokes ``git push`` (accounting for global git flags)."""
    return _extract_push_subcommand(argv, Path.cwd()) is not None


def _extract_push_subcommand(argv: Sequence[str], base_cwd: Path) -> tuple[Path, list[str]] | None:
    """Return ``(effective_cwd, push_args)`` if ``argv`` is a ``git push`` command."""
    if not argv:
        return None
    exe = os.path.basename(argv[0]).lower()
    if exe not in ("git", "git.exe"):
        return None

    cwd = base_cwd
    idx = 1
    while idx < len(argv):
        tok = argv[idx]
        if tok == "push":
            return cwd, list(argv[idx + 1 :])
        if tok == "-C":
            if idx + 1 >= len(argv):
                return None
            cwd = (cwd / argv[idx + 1]).resolve()
            idx += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            cwd = (cwd / tok[2:]).resolve()
            idx += 1
            continue
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        # Another git subcommand (e.g. `git commit`, `git status`).
        return None
    return None


def _normalize_remote_ref(raw_ref: str, *, src_full_ref: str | None = None) -> str:
    if raw_ref.startswith("refs/"):
        return raw_ref
    if src_full_ref and src_full_ref.startswith("refs/tags/"):
        return f"refs/tags/{raw_ref}"
    return f"refs/heads/{raw_ref}"


def _resolve_local_commit(ref: str, *, cwd: Path) -> str | None:
    proc = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
    if proc is None or proc.returncode != 0:
        proc = _run_git(["rev-parse", "--verify", ref], cwd=cwd)
    if proc is None or proc.returncode != 0:
        return None
    sha = proc.stdout.strip().lower()
    return sha if _HEX_SHA_RE.match(sha) else None


def _resolve_symbolic_full_name(ref: str, *, cwd: Path) -> str | None:
    proc = _run_git(["rev-parse", "--symbolic-full-name", ref], cwd=cwd)
    if proc is None or proc.returncode != 0:
        return None
    val = proc.stdout.strip()
    return val if val.startswith("refs/") else None


def inspect_git_push(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    probe_remote: bool = True,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT,
) -> GitPushSpec | None:
    """Inspect a ``git push`` command BEFORE execution and capture target ref + SHAs.

    Returns ``None`` if ``argv`` is not a ``git push`` command.
    """
    base_cwd = (cwd if cwd is not None else Path.cwd()).resolve()
    extracted = _extract_push_subcommand(argv, base_cwd)
    if extracted is None:
        return None

    repo_cwd, push_args = extracted
    is_delete = False
    is_force = False
    is_multi_or_unsupported = False
    unsupported_reason: str | None = None
    repo_flag: str | None = None
    positionals: list[str] = []

    idx = 0
    after_double_dash = False
    while idx < len(push_args):
        tok = push_args[idx]
        if after_double_dash:
            positionals.append(tok)
            idx += 1
            continue
        if tok == "--":
            after_double_dash = True
            idx += 1
            continue
        if tok in ("-d", "--delete"):
            is_delete = True
            idx += 1
            continue
        if tok in ("-f", "--force", "--force-if-includes") or tok.startswith("--force-with-lease"):
            is_force = True
            idx += 1
            continue
        if tok in ("--all", "--mirror", "--tags"):
            is_multi_or_unsupported = True
            unsupported_reason = f"Cannot safely reconcile bulk git push flag {tok!r}"
            idx += 1
            continue
        if tok in ("-n", "--dry-run"):
            is_multi_or_unsupported = True
            unsupported_reason = "Dry-run git push is not reconcilable"
            idx += 1
            continue
        if tok == "--repo":
            if idx + 1 < len(push_args):
                repo_flag = push_args[idx + 1]
                idx += 2
            else:
                idx += 1
            continue
        if tok.startswith("--repo="):
            repo_flag = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in _GIT_PUSH_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        positionals.append(tok)
        idx += 1

    remote: str | None = positionals[0] if positionals else repo_flag
    refspecs: list[str] = positionals[1:] if len(positionals) > 1 else []

    if is_multi_or_unsupported:
        return GitPushSpec(
            cwd=str(repo_cwd),
            remote=remote,
            remote_ref=None,
            expected_sha=None,
            before_sha=None,
            before_known=False,
            is_delete=is_delete,
            is_force=is_force,
            reconcilable=False,
            reason=unsupported_reason,
        )

    if len(refspecs) > 1:
        return GitPushSpec(
            cwd=str(repo_cwd),
            remote=remote,
            remote_ref=None,
            expected_sha=None,
            before_sha=None,
            before_known=False,
            is_delete=is_delete,
            is_force=is_force,
            reconcilable=False,
            reason="Cannot safely reconcile multi-refspec git push",
        )

    # Resolve current branch when remote or refspec was omitted.
    current_branch: str | None = None
    branch_proc = _run_git(["symbolic-ref", "-q", "--short", "HEAD"], cwd=repo_cwd)
    if branch_proc is not None and branch_proc.returncode == 0:
        current_branch = branch_proc.stdout.strip() or None

    if remote is None:
        if current_branch:
            cfg_proc = _run_git(
                ["config", "--get", f"branch.{current_branch}.remote"], cwd=repo_cwd
            )
            if cfg_proc is not None and cfg_proc.returncode == 0 and cfg_proc.stdout.strip():
                remote = cfg_proc.stdout.strip()
        if remote is None:
            remote = "origin"

    remote_ref: str | None = None
    expected_sha: str | None = None

    if not refspecs:
        if is_delete:
            return GitPushSpec(
                cwd=str(repo_cwd),
                remote=remote,
                remote_ref=None,
                expected_sha=None,
                before_sha=None,
                before_known=False,
                is_delete=True,
                is_force=is_force,
                reconcilable=False,
                reason="git push --delete without refspec is not reconcilable",
            )
        if not current_branch:
            return GitPushSpec(
                cwd=str(repo_cwd),
                remote=remote,
                remote_ref=None,
                expected_sha=None,
                before_sha=None,
                before_known=False,
                is_delete=False,
                is_force=is_force,
                reconcilable=False,
                reason="Detached HEAD without explicit refspec cannot be reconciled",
            )
        merge_proc = _run_git(["config", "--get", f"branch.{current_branch}.merge"], cwd=repo_cwd)
        if merge_proc is not None and merge_proc.returncode == 0 and merge_proc.stdout.strip():
            remote_ref = _normalize_remote_ref(merge_proc.stdout.strip())
        else:
            remote_ref = f"refs/heads/{current_branch}"
        expected_sha = _resolve_local_commit("HEAD", cwd=repo_cwd)
    else:
        raw_spec = refspecs[0]
        if "*" in raw_spec:
            return GitPushSpec(
                cwd=str(repo_cwd),
                remote=remote,
                remote_ref=None,
                expected_sha=None,
                before_sha=None,
                before_known=False,
                is_delete=is_delete,
                is_force=is_force,
                reconcilable=False,
                reason=f"Wildcard refspec {raw_spec!r} cannot be safely reconciled",
            )
        if raw_spec.startswith("+"):
            is_force = True
            raw_spec = raw_spec[1:]

        if ":" in raw_spec:
            src_ref, dst_ref = raw_spec.split(":", 1)
            if not src_ref:
                is_delete = True
                if not dst_ref:
                    return GitPushSpec(
                        cwd=str(repo_cwd),
                        remote=remote,
                        remote_ref=None,
                        expected_sha=None,
                        before_sha=None,
                        before_known=False,
                        is_delete=True,
                        is_force=is_force,
                        reconcilable=False,
                        reason="Empty delete refspec",
                    )
                remote_ref = _normalize_remote_ref(dst_ref)
            else:
                src_full = _resolve_symbolic_full_name(src_ref, cwd=repo_cwd)
                remote_ref = _normalize_remote_ref(dst_ref or src_ref, src_full_ref=src_full)
                expected_sha = _resolve_local_commit(src_ref, cwd=repo_cwd)
        else:
            if is_delete:
                remote_ref = _normalize_remote_ref(raw_spec)
            else:
                src_full = _resolve_symbolic_full_name(raw_spec, cwd=repo_cwd)
                if raw_spec == "HEAD" and current_branch:
                    remote_ref = f"refs/heads/{current_branch}"
                else:
                    remote_ref = _normalize_remote_ref(raw_spec, src_full_ref=src_full)
                expected_sha = _resolve_local_commit(raw_spec, cwd=repo_cwd)

    if remote_ref is None or (not is_delete and expected_sha is None):
        return GitPushSpec(
            cwd=str(repo_cwd),
            remote=remote,
            remote_ref=remote_ref,
            expected_sha=expected_sha,
            before_sha=None,
            before_known=False,
            is_delete=is_delete,
            is_force=is_force,
            reconcilable=False,
            reason="Could not resolve local target commit SHA or remote ref",
        )

    before_known = False
    before_sha: str | None = None
    if probe_remote and remote is not None:
        ok, current_sha, _ = _ls_remote_ref(
            remote, remote_ref, cwd=repo_cwd, timeout_seconds=timeout_seconds
        )
        if ok:
            before_known = True
            before_sha = current_sha

    return GitPushSpec(
        cwd=str(repo_cwd),
        remote=remote,
        remote_ref=remote_ref,
        expected_sha=expected_sha,
        before_sha=before_sha,
        before_known=before_known,
        is_delete=is_delete,
        is_force=is_force,
        reconcilable=True,
    )


class GitPushReconciler:
    """Read-only ``git ls-remote`` reconciler for wrapped ``git push`` actions."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_GIT_TIMEOUT) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self._timeout_seconds = float(timeout_seconds)

    @property
    def name(self) -> str:
        return "git_push"

    def can_reconcile(self, record: ActionRecord) -> bool:
        _, meta = parse_summary_meta(record.summary)
        if meta.get("adapter") == "git_push":
            return True
        return record.tool_name.lower() in _GIT_PUSH_TOOLS

    def reconcile(self, record: ActionRecord) -> ReconcileResult:
        """Compare remote ref SHA via read-only ``git ls-remote`` against ``expected_sha``."""
        _, meta = parse_summary_meta(record.summary)
        if meta.get("reconcilable") is False:
            reason = meta.get("reason") or "git push action was marked non-reconcilable"
            return ReconcileResult(state=ReconcileState.UNKNOWN, details=str(reason))

        remote = meta.get("remote")
        remote_ref = meta.get("remote_ref")
        expected_sha = meta.get("expected_sha")
        before_sha = meta.get("before_sha")
        before_known = bool(meta.get("before_known", False))
        is_delete = bool(meta.get("is_delete", False))
        cwd_raw = meta.get("cwd")

        if not isinstance(remote, str) or not remote.strip():
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details="Missing remote name in git push metadata",
            )
        if not isinstance(remote_ref, str) or not remote_ref.strip():
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details="Missing remote_ref in git push metadata",
            )
        if not is_delete:
            if not isinstance(expected_sha, str) or not _HEX_SHA_RE.match(expected_sha.lower()):
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details="Missing or invalid expected_sha in git push metadata",
                )
            expected_sha = expected_sha.lower()

        if before_sha is not None:
            if not isinstance(before_sha, str) or not _HEX_SHA_RE.match(before_sha.lower()):
                before_known = False
                before_sha = None
            else:
                before_sha = before_sha.lower()

        repo_cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw.strip() else Path.cwd()
        if not repo_cwd.is_dir():
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=f"Recorded repository directory does not exist: {repo_cwd}",
            )

        ok, current_sha, err = _ls_remote_ref(
            remote,
            remote_ref,
            cwd=repo_cwd,
            timeout_seconds=self._timeout_seconds,
        )
        if not ok:
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=err or f"git ls-remote failed for {remote} {remote_ref}",
            )

        if is_delete:
            if current_sha is None:
                if before_known and before_sha is not None:
                    return ReconcileResult(
                        state=ReconcileState.FOUND,
                        result_ref=f"deleted {remote} {remote_ref}",
                        summary=f"Confirmed deletion of {remote} {remote_ref}",
                        details=f"Remote ref {remote_ref} (was {before_sha[:12]}) is now absent",
                    )
                return ReconcileResult(
                    state=ReconcileState.UNKNOWN,
                    details=(
                        f"Remote ref {remote_ref} is absent, but pre-push state was not recorded"
                    ),
                )
            if before_known and before_sha is not None and current_sha == before_sha:
                return ReconcileResult(
                    state=ReconcileState.NOT_FOUND,
                    details=(
                        f"Remote ref {remote} {remote_ref} is still at pre-push SHA "
                        f"{before_sha[:12]}; delete did not land"
                    ),
                )
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=(
                    f"Remote ref {remote} {remote_ref} is at {current_sha[:12]} "
                    f"(pre-push SHA was {before_sha[:12] if before_sha else 'unknown'}); "
                    "concurrent push detected"
                ),
            )

        assert expected_sha is not None
        if current_sha == expected_sha:
            return ReconcileResult(
                state=ReconcileState.FOUND,
                result_ref=f"{remote} {remote_ref}@{expected_sha[:12]}",
                summary=f"Confirmed push of {remote} {remote_ref} at {expected_sha[:12]}",
                details=f"git ls-remote matched expected SHA {expected_sha}",
            )

        if before_known:
            if current_sha == before_sha:
                before_desc = before_sha[:12] if before_sha else "absent"
                return ReconcileResult(
                    state=ReconcileState.NOT_FOUND,
                    details=(
                        f"Remote ref {remote} {remote_ref} is still {before_desc} "
                        f"(expected {expected_sha[:12]}); push did not land"
                    ),
                )
            curr_desc = current_sha[:12] if current_sha else "absent"
            before_desc = before_sha[:12] if before_sha else "absent"
            return ReconcileResult(
                state=ReconcileState.UNKNOWN,
                details=(
                    f"Remote ref {remote} {remote_ref} moved concurrently to {curr_desc} "
                    f"(was {before_desc}, expected {expected_sha[:12]})"
                ),
            )

        curr_desc = current_sha[:12] if current_sha else "absent"
        return ReconcileResult(
            state=ReconcileState.UNKNOWN,
            details=(
                f"Remote ref {remote} {remote_ref} is {curr_desc} (expected {expected_sha[:12]}) "
                "and pre-push remote state was unknown"
            ),
        )
