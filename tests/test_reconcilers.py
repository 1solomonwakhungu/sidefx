"""Unit and integration tests for GitHub and git-push reconcilers and `sidefx reconcile`."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from sidefx.cli import main
from sidefx.journal import ActionRecord, ActionStatus, Journal, Surface, derive_marker
from sidefx.reconcilers import (
    GitHubReconciler,
    GitPushReconciler,
    ReconcileState,
    body_has_marker,
    extract_markers,
    format_marker_comment,
    format_summary_with_meta,
    inject_marker_into_body,
    inject_marker_into_gh_args,
    inject_marker_into_json_body,
    inspect_git_push,
    reconcile_action,
)


class MockGitHubState:
    """In-memory state for local HTTP mock GitHub REST API server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.issues: list[dict[str, Any]] = []
        self.pulls: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.force_status: int | None = None
        self.force_raw_body: bytes | None = None
        self.force_link_header: str | None = None
        self.redirect_location: str | None = None
        self.delay_seconds: float = 0.0
        self.page_size_override: int | None = None
        self.requests: list[dict[str, Any]] = []
        self._next_number = 1
        self._next_comment_id = 100

    def add_issue(
        self,
        *,
        owner: str = "org",
        repo: str = "repo",
        title: str = "Issue",
        body: str = "",
        state: str = "open",
        author: str = "octocat",
        is_pr: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            num = self._next_number
            self._next_number += 1
            item: dict[str, Any] = {
                "id": num * 10,
                "number": num,
                "title": title,
                "body": body,
                "state": state,
                "user": {"login": author},
                "html_url": (
                    f"https://github.com/{owner}/{repo}/pull/{num}"
                    if is_pr
                    else f"https://github.com/{owner}/{repo}/issues/{num}"
                ),
            }
            if is_pr:
                item["pull_request"] = {"url": item["html_url"]}
                self.pulls.append(item)
            self.issues.append(item)
            return item

    def add_comment(
        self,
        *,
        issue_number: int,
        owner: str = "org",
        repo: str = "repo",
        body: str = "",
        author: str = "octocat",
    ) -> dict[str, Any]:
        with self.lock:
            cid = self._next_comment_id
            self._next_comment_id += 1
            item = {
                "id": cid,
                "issue_number": issue_number,
                "body": body,
                "user": {"login": author},
                "html_url": (
                    f"https://github.com/{owner}/{repo}/issues/{issue_number}#issuecomment-{cid}"
                ),
            }
            self.comments.append(item)
            return item


@pytest.fixture()
def mock_github() -> tuple[str, MockGitHubState]:
    state = MockGitHubState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            with state.lock:
                state.requests.append(
                    {
                        "method": "GET",
                        "path": parsed.path,
                        "query": query,
                        "auth": self.headers.get("Authorization"),
                    }
                )
                delay = state.delay_seconds
                force_status = state.force_status
                force_raw_body = state.force_raw_body
                force_link = state.force_link_header
                redirect_loc = state.redirect_location
                page_size_override = state.page_size_override

            if delay > 0:
                time.sleep(delay)

            if redirect_loc is not None:
                self.send_response(302)
                self.send_header("Location", redirect_loc)
                self.end_headers()
                return

            if force_status is not None:
                self.send_response(force_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(force_raw_body or b'{"message":"forced error"}')
                return

            if force_raw_body is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if force_link is not None:
                    self.send_header("Link", force_link)
                self.end_headers()
                self.wfile.write(force_raw_body)
                return

            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 4 or parts[0] != "repos":
                self.send_response(404)
                self.end_headers()
                return

            endpoint = "/".join(parts[3:])
            req_state = (query.get("state") or ["open"])[0]
            page = int((query.get("page") or ["1"])[0])
            per_page = page_size_override or int((query.get("per_page") or ["100"])[0])

            with state.lock:
                if endpoint == "issues":
                    items = list(reversed(state.issues))
                    if req_state != "all":
                        items = [i for i in items if i["state"] == req_state]
                elif endpoint == "pulls":
                    items = list(reversed(state.pulls))
                    if req_state != "all":
                        items = [i for i in items if i["state"] == req_state]
                elif endpoint == "issues/comments":
                    items = list(reversed(state.comments))
                elif len(parts) == 6 and parts[3] == "issues" and parts[5] == "comments":
                    issue_num = int(parts[4])
                    items = [c for c in state.comments if c["issue_number"] == issue_num]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

            start = (page - 1) * per_page
            end = start + per_page
            page_items = items[start:end]

            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if force_link is not None:
                self.send_header("Link", force_link)
            elif end < len(items):
                next_qs = f"state={req_state}&per_page={per_page}&page={page + 1}"
                self.send_header("Link", f'<{base_url}{parsed.path}?{next_qs}>; rel="next"')
            self.end_headers()
            self.wfile.write(json.dumps(page_items).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _gh_summary(
    *,
    kind: str = "issue",
    repo: str = "org/repo",
    marker_injected: bool = True,
    repo_inferred: bool = False,
    issue_number: int | None = None,
) -> str:
    meta: dict[str, Any] = {
        "adapter": "github",
        "kind": kind,
        "repo": repo,
        "marker_injected": marker_injected,
        "repo_inferred": repo_inferred,
    }
    if issue_number is not None:
        meta["issue_number"] = issue_number
    return format_summary_with_meta(f"gh {kind} ({repo})", meta)


def _make_record(
    *,
    action_id: str = "act-1",
    tool_name: str = "gh_issue_create",
    target: str = "org/repo",
    marker: str | None = None,
    summary: str | None = None,
    status: ActionStatus = ActionStatus.UNKNOWN,
) -> ActionRecord:
    resolved_marker = marker or derive_marker("agent", target, action_id)
    return ActionRecord(
        action_id=action_id,
        session_id="sess-1",
        actor="agent",
        surface=Surface.CLI,
        tool_name=tool_name,
        target=target,
        payload_hash="a" * 64,
        marker=resolved_marker,
        status=status,
        attempt=1,
        summary=summary,
    )


class TestGitHubMarkerHelpers:
    def test_marker_format_extract_and_exact_match(self) -> None:
        comment = format_marker_comment("abc123")
        assert comment == "<!-- sidefx:id=abc123 -->"
        body = "Hello world\n\n<!-- sidefx:id=abc123def -->"
        assert extract_markers(body) == ["abc123def"]
        assert body_has_marker(body, "abc123def") is True
        assert body_has_marker(body, "abc123") is False

    def test_inject_marker_into_body_is_idempotent(self) -> None:
        once = inject_marker_into_body("Fix bug", "m1")
        assert once == "Fix bug\n\n<!-- sidefx:id=m1 -->"
        twice = inject_marker_into_body(once, "m1")
        assert twice == once
        assert inject_marker_into_body("", "m1") == "<!-- sidefx:id=m1 -->"
        assert inject_marker_into_body(None, "m1") == "<!-- sidefx:id=m1 -->"

    def test_inject_marker_into_gh_args_variants_and_edge_cases(self) -> None:
        args1, ok1 = inject_marker_into_gh_args(
            ["gh", "issue", "create", "--title", "Bug", "--body", "Details"], "tok1"
        )
        assert ok1 is True
        assert args1[-1] == "Details\n\n<!-- sidefx:id=tok1 -->"

        args2, ok2 = inject_marker_into_gh_args(
            ["gh", "pr", "create", "-t", "PR", "--body=Initial"], "tok2"
        )
        assert ok2 is True
        assert args2[-1] == "--body=Initial\n\n<!-- sidefx:id=tok2 -->"

        # Attached short -bUser body text (B5!)
        args_att, ok_att = inject_marker_into_gh_args(
            ["gh", "issue", "create", "--title", "T", "-bUser body text"], "tok_att"
        )
        assert ok_att is True
        assert args_att[-1] == "-bUser body text\n\n<!-- sidefx:id=tok_att -->"
        assert "--body" not in args_att

        # Attached -Fbody.md MUST NOT append --body (B5!)
        args_fatt, ok_fatt = inject_marker_into_gh_args(
            ["gh", "issue", "create", "--title", "T", "-Fbody.md"], "tok_f"
        )
        assert ok_fatt is False
        assert args_fatt == ["gh", "issue", "create", "--title", "T", "-Fbody.md"]

        # `--title -b --label bug` must NOT misinterpret `-b` (value of --title) as body flag (B5!)
        args_val, ok_val = inject_marker_into_gh_args(
            ["gh", "issue", "create", "--title", "-b", "--label", "bug"], "tok_val"
        )
        assert ok_val is True
        assert args_val == [
            "gh",
            "issue",
            "create",
            "--title",
            "-b",
            "--label",
            "bug",
            "--body",
            "<!-- sidefx:id=tok_val -->",
        ]

        # `gh issue comment 5` with NO body must NOT fabricate a marker-only comment (B5!)
        args_cmt, ok_cmt = inject_marker_into_gh_args(["gh", "issue", "comment", "5"], "tok_c")
        assert ok_cmt is False
        assert args_cmt == ["gh", "issue", "comment", "5"]

        # Interactive `gh issue create` with neither --title nor --body -> injected=False
        args_int, ok_int = inject_marker_into_gh_args(["gh", "issue", "create"], "tok_i")
        assert ok_int is False
        assert args_int == ["gh", "issue", "create"]

        # Conflicting flags (--body-file / --fill / -we) are NOT clobbered
        for cmd in (
            ["gh", "issue", "create", "--title", "F", "--body-file", "body.md"],
            ["gh", "pr", "create", "--fill"],
            ["gh", "issue", "create", "--title", "W", "-we"],
        ):
            out_args, ok = inject_marker_into_gh_args(cmd, "tok_x")
            assert ok is False
            assert out_args == cmd

    def test_inject_marker_into_json_body(self) -> None:
        out, ok = inject_marker_into_json_body('{"title":"T","body":"B"}', "m9")
        assert ok is True
        parsed = json.loads(out)
        assert parsed["title"] == "T"
        assert body_has_marker(parsed["body"], "m9")

        raw_bad = "[1, 2, 3]"
        out_bad, ok_bad = inject_marker_into_json_body(raw_bad, "m9")
        assert ok_bad is False
        assert out_bad == raw_bad

        # Overflow float (1e400 -> Infinity) must fail closed, never emit NaN/Infinity JSON
        raw_inf = '{"title":"T","n":1e400}'
        out_inf, ok_inf = inject_marker_into_json_body(raw_inf, "m9")
        assert ok_inf is False
        assert out_inf == raw_inf


class TestGitHubReconciler:
    def test_recovers_open_and_closed_issues_and_prs_and_comments(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        reconciler = GitHubReconciler(api_base_url=base_url, token="test-token")

        rec_open = _make_record(
            action_id="iss-open", tool_name="gh_issue_create", summary=_gh_summary(kind="issue")
        )
        open_item = state.add_issue(
            title="Open", body=inject_marker_into_body("desc", rec_open.marker), state="open"
        )
        res_open = reconciler.reconcile(rec_open)
        assert res_open.state == ReconcileState.FOUND
        assert res_open.result_ref == open_item["html_url"]

        # Closed issue proves state=all is sent
        rec_closed = _make_record(
            action_id="iss-closed", tool_name="gh_issue_create", summary=_gh_summary(kind="issue")
        )
        closed_item = state.add_issue(
            title="Closed", body=inject_marker_into_body("done", rec_closed.marker), state="closed"
        )
        res_closed = reconciler.reconcile(rec_closed)
        assert res_closed.state == ReconcileState.FOUND
        assert res_closed.result_ref == closed_item["html_url"]
        assert state.requests[-1]["query"]["state"] == ["all"]
        assert state.requests[-1]["auth"] == "Bearer test-token"

        # Closed PR
        rec_pr = _make_record(
            action_id="pr-closed", tool_name="gh_pr_create", summary=_gh_summary(kind="pr")
        )
        closed_pr = state.add_issue(
            title="Closed PR",
            body=inject_marker_into_body("pr body", rec_pr.marker),
            state="closed",
            is_pr=True,
        )
        res_pr = reconciler.reconcile(rec_pr)
        assert res_pr.state == ReconcileState.FOUND
        assert res_pr.result_ref == closed_pr["html_url"]

        # Issue comment
        rec_comment = _make_record(
            action_id="cmt-1",
            tool_name="gh_issue_comment",
            target=f"org/repo#{open_item['number']}",
            summary=_gh_summary(kind="comment", issue_number=open_item["number"]),
        )
        cmt = state.add_comment(
            issue_number=open_item["number"],
            body=inject_marker_into_body("comment text", rec_comment.marker),
        )
        res_cmt = reconciler.reconcile(rec_comment)
        assert res_cmt.state == ReconcileState.FOUND
        assert res_cmt.result_ref == cmt["html_url"]

    def test_pagination_across_multiple_pages_and_cap_safety(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        state.page_size_override = 2

        rec_target = _make_record(
            action_id="deep-item", tool_name="gh_issue_create", summary=_gh_summary()
        )
        target_issue = state.add_issue(
            title="Target", body=inject_marker_into_body("old", rec_target.marker)
        )
        for i in range(5):
            state.add_issue(title=f"Noise {i}", body=f"no marker {i}")

        reconciler_ok = GitHubReconciler(api_base_url=base_url, per_page=2, max_pages=5)
        res_found = reconciler_ok.reconcile(rec_target)
        assert res_found.state == ReconcileState.FOUND
        assert res_found.result_ref == target_issue["html_url"]

        # Capped max_pages=2 MUST return UNKNOWN, NEVER NOT_FOUND!
        reconciler_capped = GitHubReconciler(api_base_url=base_url, per_page=2, max_pages=2)
        res_capped = reconciler_capped.reconcile(rec_target)
        assert res_capped.state == ReconcileState.UNKNOWN
        assert "Pagination cap" in (res_capped.details or "")

    def test_exact_marker_lookalike_and_genuine_not_found(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        rec = _make_record(action_id="missing", marker="abc123", summary=_gh_summary())
        state.add_issue(title="Lookalike", body="<!-- sidefx:id=abc123456 -->")

        reconciler = GitHubReconciler(api_base_url=base_url)
        res = reconciler.reconcile(rec)
        assert res.state == ReconcileState.NOT_FOUND

    def test_fails_closed_without_verified_marker_injection_or_explicit_repo(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        """B2 & B4: Never return NOT_FOUND when marker injection or target repo is unverified."""
        base_url, state = mock_github
        reconciler = GitHubReconciler(api_base_url=base_url)

        # Generic non-GitHub tool `create_issue` is not claimed by GitHubReconciler
        generic = _make_record(action_id="lin-1", tool_name="create_issue", target="org/repo")
        assert reconciler.can_reconcile(generic) is False

        # Sparse `gh_issue_create` with NO sidefx_meta: 0 remote matches -> UNKNOWN (not NOT_FOUND!)
        unverified = _make_record(action_id="unv-1", tool_name="gh_issue_create", summary=None)
        res_missing = reconciler.reconcile(unverified)
        assert res_missing.state == ReconcileState.UNKNOWN
        assert "not verified" in (res_missing.details or "")

        # Inferred repo (from git origin) also caps 0-matches at UNKNOWN
        inferred = _make_record(
            action_id="inf-1",
            tool_name="gh_issue_create",
            summary=_gh_summary(repo_inferred=True),
        )
        assert reconciler.reconcile(inferred).state == ReconcileState.UNKNOWN

        # Explicitly marker_injected=False returns UNKNOWN immediately
        no_inj = _make_record(
            action_id="noinj-1",
            tool_name="gh_issue_create",
            summary=_gh_summary(marker_injected=False),
        )
        assert reconciler.reconcile(no_inj).state == ReconcileState.UNKNOWN

        # Even if metadata was sparse, an EXACT remote marker match still safely returns FOUND
        found_item = state.add_issue(
            title="Found", body=inject_marker_into_body("ok", unverified.marker)
        )
        res_found = reconciler.reconcile(unverified)
        assert res_found.state == ReconcileState.FOUND
        assert res_found.result_ref == found_item["html_url"]

    def test_ambiguous_duplicate_markers_returns_unknown(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        rec = _make_record(action_id="dup", marker="dup-marker", summary=_gh_summary())
        state.add_issue(title="First", body=inject_marker_into_body("1", "dup-marker"))
        state.add_issue(title="Second", body=inject_marker_into_body("2", "dup-marker"))

        reconciler = GitHubReconciler(api_base_url=base_url)
        res = reconciler.reconcile(rec)
        assert res.state == ReconcileState.UNKNOWN
        assert "Ambiguous" in (res.details or "")

    @pytest.mark.parametrize("status_code", [401, 403, 404, 429, 500, 503])
    def test_http_errors_always_return_unknown(
        self, mock_github: tuple[str, MockGitHubState], status_code: int
    ) -> None:
        base_url, state = mock_github
        state.force_status = status_code
        rec = _make_record(action_id=f"err-{status_code}", summary=_gh_summary())

        reconciler = GitHubReconciler(api_base_url=base_url)
        res = reconciler.reconcile(rec)
        assert res.state == ReconcileState.UNKNOWN
        assert str(status_code) in (res.details or "")

    def test_malformed_json_and_non_list_return_unknown(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        rec = _make_record(action_id="json-1", summary=_gh_summary())
        reconciler = GitHubReconciler(api_base_url=base_url)

        state.force_raw_body = b"{not-json"
        assert reconciler.reconcile(rec).state == ReconcileState.UNKNOWN

        state.force_raw_body = b'{"message": "not a list"}'
        assert reconciler.reconcile(rec).state == ReconcileState.UNKNOWN

    def test_cross_origin_link_redirect_and_timeout_return_unknown(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        rec = _make_record(action_id="ssrf-1", summary=_gh_summary())
        reconciler = GitHubReconciler(
            api_base_url=base_url, token="secret-tok", timeout_seconds=0.3
        )

        # Cross-origin Link header refused
        state.force_raw_body = b"[]"
        state.force_link_header = '<http://evil.example.com/steal>; rel="next"'
        res_ssrf = reconciler.reconcile(rec)
        assert res_ssrf.state == ReconcileState.UNKNOWN
        assert "mismatched origin" in (res_ssrf.details or "")
        state.force_raw_body = None
        state.force_link_header = None

        # Cross-origin HTTP redirect refused
        state.redirect_location = "http://evil.example.com/steal"
        assert reconciler.reconcile(rec).state == ReconcileState.UNKNOWN
        state.redirect_location = None

        # Slow server timeout -> UNKNOWN
        state.delay_seconds = 1.0
        assert reconciler.reconcile(rec).state == ReconcileState.UNKNOWN
        state.delay_seconds = 0.0

    def test_ambient_token_not_leaked_to_non_github_host_and_http_non_loopback_rejected(
        self,
        mock_github: tuple[str, MockGitHubState],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N3: Never attach ambient GITHUB_TOKEN to custom hosts; reject non-loopback http://."""
        base_url, state = mock_github
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret")

        reconciler = GitHubReconciler(api_base_url=base_url)
        rec = _make_record(action_id="tok-leak", summary=_gh_summary())
        reconciler.reconcile(rec)
        assert state.requests[-1]["auth"] is None

        with pytest.raises(ValueError, match="loopback"):
            GitHubReconciler(api_base_url="http://api.example.com")

    def test_author_filter_excludes_other_authors(
        self, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        rec = _make_record(action_id="auth-1", marker="auth-marker", summary=_gh_summary())
        state.add_issue(
            title="Spoofed", body=inject_marker_into_body("x", "auth-marker"), author="attacker"
        )
        reconciler = GitHubReconciler(api_base_url=base_url, expected_author="bot-user")
        assert reconciler.reconcile(rec).state == ReconcileState.NOT_FOUND

        legit = state.add_issue(
            title="Real", body=inject_marker_into_body("y", "auth-marker"), author="Bot-User"
        )
        res2 = reconciler.reconcile(rec)
        assert res2.state == ReconcileState.FOUND
        assert res2.result_ref == legit["html_url"]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _setup_git_repos(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test User")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "README.md").write_text("v1\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "main")
    return work, bare


class TestGitPushReconciler:
    def test_push_found_not_found_concurrent_move_and_unreachable_remote(
        self, tmp_path: Path
    ) -> None:
        work, bare = _setup_git_repos(tmp_path)

        (work / "README.md").write_text("v2\n", encoding="utf-8")
        _git(work, "add", "README.md")
        _git(work, "commit", "-m", "v2")
        sha_v2 = _git(work, "rev-parse", "HEAD")

        spec = inspect_git_push(["git", "push", "origin", "main"], cwd=work)
        assert spec is not None
        assert spec.reconcilable is True
        assert spec.expected_sha == sha_v2
        assert spec.before_known is True
        assert spec.before_sha != sha_v2

        rec = _make_record(
            action_id="push-1",
            tool_name="git_push",
            target="origin:refs/heads/main",
            summary=format_summary_with_meta("git push origin main", spec.to_meta()),
        )
        reconciler = GitPushReconciler()

        assert reconciler.reconcile(rec).state == ReconcileState.NOT_FOUND

        _git(work, "push", "origin", "main")
        res_after = reconciler.reconcile(rec)
        assert res_after.state == ReconcileState.FOUND
        assert res_after.result_ref == f"origin refs/heads/main@{sha_v2[:12]}"

        # Concurrent push from another clone -> UNKNOWN
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-b", "main", str(bare), str(other))
        _git(other, "config", "user.email", "other@example.com")
        _git(other, "config", "user.name", "Other User")
        (other / "README.md").write_text("v3-concurrent\n", encoding="utf-8")
        _git(other, "add", "README.md")
        _git(other, "commit", "-m", "v3")
        _git(other, "push", "origin", "main")

        res_concurrent = reconciler.reconcile(rec)
        assert res_concurrent.state == ReconcileState.UNKNOWN
        assert "concurrently" in (res_concurrent.details or "")

        # Broken / unreachable remote -> UNKNOWN
        bad_meta = dict(spec.to_meta())
        bad_meta["remote"] = str(tmp_path / "nonexistent-remote.git")
        rec_bad = _make_record(
            action_id="push-bad",
            tool_name="git_push",
            target="bad:refs/heads/main",
            summary=format_summary_with_meta("git push bad", bad_meta),
        )
        assert reconciler.reconcile(rec_bad).state == ReconcileState.UNKNOWN

    def test_triangular_push_pushurl_annotated_tag_and_global_flags(self, tmp_path: Path) -> None:
        """B4 & N11: Triangular push.default=simple, pushurl, annotated tags, and -c/--git-dir."""
        work, bare = _setup_git_repos(tmp_path)
        upstream_bare = tmp_path / "upstream.git"
        upstream_bare.mkdir()
        _git(upstream_bare, "init", "--bare", "-b", "main")
        _git(work, "remote", "add", "upstream", str(upstream_bare))
        _git(work, "push", "upstream", "main")

        # Branch `feat` tracks `upstream/main`, then pushes to `origin` under push.default=simple!
        _git(work, "checkout", "-b", "feat", "--track", "upstream/main")
        (work / "feat.txt").write_text("triangular\n", encoding="utf-8")
        _git(work, "add", "feat.txt")
        _git(work, "commit", "-m", "feat commit")
        feat_sha = _git(work, "rev-parse", "HEAD")

        spec = inspect_git_push(["git", "push", "origin"], cwd=work)
        assert spec is not None
        assert spec.reconcilable is True
        assert spec.remote == "origin"
        assert spec.remote_ref == "refs/heads/feat"  # NOT refs/heads/main!
        assert spec.expected_sha == feat_sha

        _git(work, "push", "origin")
        rec = _make_record(
            action_id="tri-1",
            tool_name="git_push",
            target="origin:refs/heads/feat",
            summary=format_summary_with_meta("git push origin", spec.to_meta()),
        )
        assert GitPushReconciler().reconcile(rec).state == ReconcileState.FOUND

        # Annotated tag push compares exact tag object SHA (N11!)
        _git(work, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")
        tag_spec = inspect_git_push(["git", "push", "origin", "v1.0.0"], cwd=work)
        assert tag_spec is not None and tag_spec.reconcilable is True
        assert tag_spec.remote_ref == "refs/tags/v1.0.0"
        _git(work, "push", "origin", "v1.0.0")
        rec_tag = _make_record(
            action_id="tag-1",
            tool_name="git_push",
            target="origin:refs/tags/v1.0.0",
            summary=format_summary_with_meta("git push origin v1.0.0", tag_spec.to_meta()),
        )
        assert GitPushReconciler().reconcile(rec_tag).state == ReconcileState.FOUND

        # pushurl is respected when fetch URL and push URL differ!
        push_only_bare = tmp_path / "push_only.git"
        push_only_bare.mkdir()
        _git(push_only_bare, "init", "--bare", "-b", "main")
        _git(work, "remote", "set-url", "--push", "origin", str(push_only_bare))
        _git(work, "checkout", "main")
        (work / "README.md").write_text("pushurl-v2\n", encoding="utf-8")
        _git(work, "add", "README.md")
        _git(work, "commit", "-m", "pushurl")
        spec_pushurl = inspect_git_push(["git", "push", "origin", "main"], cwd=work)
        assert spec_pushurl is not None and spec_pushurl.reconcilable is True
        assert spec_pushurl.before_sha is None  # push_only_bare has no refs yet!
        _git(work, "push", "origin", "main")
        rec_pu = _make_record(
            action_id="pu-1",
            tool_name="git_push",
            target="origin:refs/heads/main",
            summary=format_summary_with_meta("git push origin main", spec_pushurl.to_meta()),
        )
        assert GitPushReconciler().reconcile(rec_pu).state == ReconcileState.FOUND

        # Global `-c` alters config -> non-reconcilable (UNKNOWN)
        spec_c = inspect_git_push(
            ["git", "-c", "push.default=matching", "push", "origin"], cwd=work
        )
        assert spec_c is not None and spec_c.reconcilable is False

    def test_explicit_refspec_remapping_mirror_and_combined_delete_flags(
        self, tmp_path: Path
    ) -> None:
        """Round-2 B2 & nits: explicit refspec remapping, mirror remote, `-fd`, and ssh:// user."""
        work, _ = _setup_git_repos(tmp_path)
        _git(work, "checkout", "-b", "feature", "--track", "origin/main")

        # push.default=upstream with branch.feature.merge=refs/heads/main -> non-reconcilable
        _git(work, "config", "push.default", "upstream")
        spec_up = inspect_git_push(["git", "push", "origin", "feature"], cwd=work)
        assert spec_up is not None and spec_up.reconcilable is False

        # Reset push.default; custom remote.origin.push refspec -> non-reconcilable
        _git(work, "config", "--unset", "push.default")
        _git(work, "config", "remote.origin.push", "refs/heads/*:refs/heads/review/*")
        spec_map = inspect_git_push(["git", "push", "origin", "feature"], cwd=work)
        assert spec_map is not None and spec_map.reconcilable is False
        _git(work, "config", "--unset", "remote.origin.push")

        # Combined `-fd` flag marks delete!
        spec_fd = inspect_git_push(["git", "push", "-fd", "origin", "feature"], cwd=work)
        assert spec_fd is not None and spec_fd.is_delete is True and spec_fd.is_force is True

        # Mirror remote -> non-reconcilable
        _git(work, "config", "remote.origin.mirror", "true")
        spec_mir = inspect_git_push(["git", "push", "origin", "feature"], cwd=work)
        assert spec_mir is not None and spec_mir.reconcilable is False

        # ssh://git@host preserves username, while https://user:pass@host strips credentials
        from sidefx.reconcilers.git_push import redact_remote_for_display

        assert (
            redact_remote_for_display("ssh://git@github.com/org/repo.git")
            == "ssh://git@github.com/org/repo.git"
        )
        assert (
            redact_remote_for_display("https://user:secret@github.com/org/repo.git?tok=1")
            == "https://github.com/org/repo.git"
        )

    def test_delete_and_multi_ref_and_dash_c_flag(self, tmp_path: Path) -> None:
        work, _ = _setup_git_repos(tmp_path)
        _git(work, "checkout", "-b", "feat")
        (work / "feat.txt").write_text("hi\n", encoding="utf-8")
        _git(work, "add", "feat.txt")
        _git(work, "commit", "-m", "feat")
        _git(work, "push", "origin", "feat")

        del_spec = inspect_git_push(
            ["git", "-C", str(work), "push", "--delete", "origin", "feat"], cwd=tmp_path
        )
        assert del_spec is not None
        assert del_spec.reconcilable is True
        assert del_spec.is_delete is True
        assert del_spec.before_known is True
        assert del_spec.before_sha is not None

        rec_del = _make_record(
            action_id="del-1",
            tool_name="git_push",
            target="origin:refs/heads/feat",
            summary=format_summary_with_meta("git push --delete origin feat", del_spec.to_meta()),
        )
        reconciler = GitPushReconciler()
        assert reconciler.reconcile(rec_del).state == ReconcileState.NOT_FOUND

        _git(work, "push", "--delete", "origin", "feat")
        res_del = reconciler.reconcile(rec_del)
        assert res_del.state == ReconcileState.FOUND
        assert res_del.result_ref == "deleted origin refs/heads/feat"

        all_spec = inspect_git_push(["git", "push", "--all", "origin"], cwd=work)
        assert all_spec is not None and all_spec.reconcilable is False
        rec_all = _make_record(
            action_id="all-1",
            tool_name="git_push",
            target="origin",
            summary=format_summary_with_meta("git push --all", all_spec.to_meta()),
        )
        assert reconciler.reconcile(rec_all).state == ReconcileState.UNKNOWN


class TestReconcileCliAndOrchestration:
    def test_reconcile_cli_dry_run_apply_and_json(
        self,
        tmp_path: Path,
        mock_github: tuple[str, MockGitHubState],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        base_url, state = mock_github
        db_path = tmp_path / "journal.db"

        rc_missing = main(["--db", str(db_path), "reconcile", "--all"])
        assert rc_missing == 0
        assert not db_path.exists()
        _ = capsys.readouterr()

        with Journal(db_path) as journal:
            b1 = journal.begin_action(
                action_id="rec-found",
                session_id="s1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"t": "found"},
                summary=_gh_summary(),
            )
            journal.mark_unknown("agent", "org/repo", "rec-found", attempt=1, error="timeout")

            journal.begin_action(
                action_id="rec-missing",
                session_id="s1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"t": "missing"},
                summary=_gh_summary(),
            )
            journal.mark_unknown("agent", "org/repo", "rec-missing", attempt=1, error="timeout")

        issue = state.add_issue(title="Found", body=inject_marker_into_body("ok", b1.record.marker))

        # --dry-run does NOT mutate journal
        rc_dry = main(
            [
                "--db",
                str(db_path),
                "reconcile",
                "--all",
                "--dry-run",
                "--json",
                "--github-api-url",
                base_url,
            ]
        )
        assert rc_dry == 0
        dry_data = json.loads(capsys.readouterr().out)
        assert dry_data["dry_run"] is True
        assert len(dry_data["outcomes"]) == 2
        assert all(o["applied"] is False for o in dry_data["outcomes"])

        with Journal.open_readonly(db_path) as j:
            assert j.get_action("agent", "org/repo", "rec-found").status == ActionStatus.UNKNOWN
            assert j.get_action("agent", "org/repo", "rec-missing").status == ActionStatus.UNKNOWN

        rc_one = main(
            [
                "reconcile",
                "--db",
                str(db_path),
                "--action-id",
                "rec-found",
                "--github-api-url",
                base_url,
            ]
        )
        assert rc_one == 0
        assert issue["html_url"] in capsys.readouterr().out

        with Journal.open_readonly(db_path) as j:
            updated = j.get_action("agent", "org/repo", "rec-found")
            assert updated is not None
            assert updated.status == ActionStatus.RECONCILED
            assert updated.result_ref == issue["html_url"]

        rc_all = main(["--db", str(db_path), "reconcile", "--all", "--github-api-url", base_url])
        assert rc_all == 0
        _ = capsys.readouterr()

        with Journal.open_readonly(db_path) as j:
            missing_rec = j.get_action("agent", "org/repo", "rec-missing")
            assert missing_rec is not None
            assert missing_rec.status == ActionStatus.FAILED

    def test_reconcile_cli_never_marks_live_pending_failed_without_explicit_opt_in(
        self,
        tmp_path: Path,
        mock_github: tuple[str, MockGitHubState],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """B1: `sidefx reconcile` on a live PENDING row must leave it PENDING by default."""
        base_url, _ = mock_github
        db_path = tmp_path / "journal.db"

        with Journal(db_path) as journal:
            journal.begin_action(
                action_id="live-pending",
                session_id="s1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"t": "in-flight"},
                summary=_gh_summary(),
            )

        # Default reconcile leaves PENDING untouched and exits 1 (still unresolved!)
        rc1 = main(["--db", str(db_path), "reconcile", "--all", "--github-api-url", base_url])
        assert rc1 == 1
        out1 = capsys.readouterr().out
        assert "still pending" in out1
        with Journal.open_readonly(db_path) as j:
            assert j.get_action("agent", "org/repo", "live-pending").status == ActionStatus.PENDING

        # `--stale-pending-after 3600` also protects fresh pending rows
        rc2 = main(
            [
                "--db",
                str(db_path),
                "reconcile",
                "--all",
                "--stale-pending-after",
                "3600",
                "--github-api-url",
                base_url,
            ]
        )
        assert rc2 == 1
        _ = capsys.readouterr()
        with Journal.open_readonly(db_path) as j:
            assert j.get_action("agent", "org/repo", "live-pending").status == ActionStatus.PENDING

        # Explicit `--force-pending` allows operator to mark a known-dead pending row failed
        rc3 = main(
            [
                "--db",
                str(db_path),
                "reconcile",
                "--all",
                "--force-pending",
                "--github-api-url",
                base_url,
            ]
        )
        assert rc3 == 0
        _ = capsys.readouterr()
        with Journal.open_readonly(db_path) as j:
            assert j.get_action("agent", "org/repo", "live-pending").status == ActionStatus.FAILED

    def test_reconcile_action_handles_stale_attempt_cleanly(
        self, tmp_path: Path, mock_github: tuple[str, MockGitHubState]
    ) -> None:
        base_url, state = mock_github
        db_path = tmp_path / "journal.db"
        with Journal(db_path) as journal:
            b1 = journal.begin_action(
                action_id="race-1",
                session_id="s1",
                actor="agent",
                surface=Surface.CLI,
                tool_name="gh_issue_create",
                target="org/repo",
                payload={"t": "race"},
                summary=_gh_summary(),
            )
            stale_snapshot = b1.record
            journal.mark_confirmed(
                "agent",
                "org/repo",
                "race-1",
                attempt=1,
                result_ref="https://github.com/org/repo/issues/99",
            )
            state.add_issue(title="Race", body=inject_marker_into_body("x", stale_snapshot.marker))
            outcome = reconcile_action(
                journal,
                stale_snapshot,
                reconcilers=[GitHubReconciler(api_base_url=base_url)],
            )
            assert outcome.applied is False
            assert outcome.updated_record is not None
            assert outcome.updated_record.status == ActionStatus.CONFIRMED
