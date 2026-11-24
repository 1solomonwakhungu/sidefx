# sidefx Launch Assets & Release Notes

Ready-to-use launch copy grounded in verified behavior, real chaos evaluation results (`6/6 passed`, `0 duplicate remote writes`, `100.0% receipt accuracy`), and representative single-run benchmarks (`uv run python evals/benchmark.py` from a git clone on macOS arm64, Python 3.13.14, SQLite 3.53.1).

---

## 1. Show HN

### Title
Show HN: sidefx – SQLite write-ahead journal so AI agents stop double-posting

### Body
I built sidefx after watching coding agents hit the same two failure modes over and over:

1. A `gh issue create`, `git push`, or MCP tool call times out or loses its connection after the remote write already landed. The agent retries and creates duplicate issues, duplicate comments, or duplicate webhook calls.
2. At the end of a long session, the agent summarizes what it changed from compressed conversation context and misses a push or claims a failed write succeeded.

sidefx puts a local SQLite write-ahead journal (`WAL` + `synchronous=FULL`, plus `fullfsync=ON` on macOS) in front of mutating MCP and CLI calls. Pure Python 3.11+ standard library, zero runtime dependencies, zero LLM calls.

Repo: https://github.com/1solomonwakhungu/sidefx

How it works:

1. Transparent stdio MCP proxy: prefix any existing MCP server command in Claude Code, Cursor, or Codex with `sidefx mcp-proxy --deterministic-ids --`. Read-only calls (`get_*`, `list_*`, `read_*`, `search_*`, protocol methods) pass straight through. Mutating `tools/call` requests run through deterministic preflight rules from `.sidefx.json` (numeric caps, protected-branch denylists, email/URL domain allowlists, required arguments), atomically claim `(actor, target, action_id)` as `pending` in SQLite before forwarding, and block blind retries with `-32002 OUTCOME UNKNOWN` if the upstream times out or crashes.
2. CLI mutation wrapper: `sidefx wrap -- git push origin feat/x` or `sidefx wrap -- gh issue create ...`. It derives deterministic action IDs automatically for `git push` (from remote, ref, and local SHA) and `gh issue/pr create|comment`. For `gh` writes and GitHub REST `curl` POSTs, it injects a hidden `<!-- sidefx:id=<marker> -->` comment into the body. On a timeout retry, `sidefx reconcile` checks the remote read-only (`git ls-remote` or GitHub REST GET across open and closed items) and recovers the created URL or ref instead of writing a second time. For tools without a built-in reconciler, `sidefx resolve --action-id <id> --landed|--not-landed` records a manual verification so the action can unblock cleanly.
3. Ground-truth receipts: `sidefx report` renders Markdown, plain text, or JSON strictly from SQLite rows (opened read-only with `mode=ro` and `PRAGMA query_only=ON`). It also ships a Claude Code `Stop` hook (`sidefx hook claude-code`) that stays silent when zero side effects happened and surfaces a loud warning if anything is still `pending` or `unknown`.

Privacy and security choices: the journal stores SHA-256 hashes of arguments/argv rather than raw payloads, redacts URL credentials, webhook path tokens, Bearer/Basic/token headers, known API key prefixes, and `KEY=VALUE` secrets before storing targets, MCP snippets, or stderr lines, sanitizes ANSI/BiDi/Markdown injection in receipts, and writes `.sidefx/` with `0700`/`0600` permissions plus a `.gitignore`.

Numbers you can reproduce yourself:

- Chaos fault-injection suite (`uvx sidefx eval --verbose`): spins up a real local mock GitHub HTTP server, a real bare git repo with fault-injecting `pre-receive`/`post-receive` hooks, a subprocess stdio MCP server with an `fsync`-backed ledger, and 4 concurrent OS workers racing on the same action ID. Result: 6/6 scenarios passed, 0 duplicate remote writes, 100.0% receipt accuracy (17/17 checks) in ~13s.
- Single-run overhead (`uv run python evals/benchmark.py` from a git clone on macOS arm64, Python 3.13.14, SQLite 3.53.1):
  - MCP read-only passthrough: ~0.033 ms median (~+0.02 ms over direct stdio)
  - MCP mutating write-ahead call: ~8.0 ms median (~+8.0 ms, dominated by two `fullfsync` WAL commits)
  - MCP duplicate replay (`--deterministic-ids`): ~0.044 ms median, zero upstream calls
  - CLI `sidefx wrap`: ~69 ms median (~+67 ms over bare `/usr/bin/true`, mostly Python startup)

Try it without installing globally:

```bash
uvx sidefx eval --verbose
```

Or install as a CLI / agent skill:

```bash
uv tool install sidefx
npx skills add 1solomonwakhungu/sidefx
```

I would love feedback on which reconcilers people want next (Linear, Slack, Jira, Stripe, or generic HTTP idempotency headers) and where MCP clients behave strangely around custom `-3200x` errors or injected `initialize` instructions.

---

## 2. Reddit Posts

### A. `r/ClaudeAI`

**Title:** I built a local SQLite write-ahead journal + Claude Code Stop hook so Claude stops double-posting after timeouts and reports real side effects

**Body:**
If you use Claude Code with GitHub, git push, webhooks, or MCP servers, you have probably seen both of these happen:

- A `gh issue create`, `git push`, or MCP tool call times out after the server already accepted it. Claude retries and posts a second issue or fires the webhook twice.
- After compaction or a long run, Claude's final summary misses a push or comment it actually made, or forgets an action whose outcome timed out.

I built **sidefx** to put a deterministic, zero-LLM safety layer in front of those external writes: https://github.com/1solomonwakhungu/sidefx

What it does inside Claude Code:

1. **Drop-in MCP proxy** in `.mcp.json`: wrap any stdio MCP server with `sidefx mcp-proxy --deterministic-ids -- <existing server command>`. Read-only calls pass straight through (~+0.02 ms median overhead in single-run benchmarks). Mutating calls are checked against `.sidefx.json` preflight rules (like capping refund amounts, blocking pushes to `main`, or restricting email domains) and logged as `pending` in SQLite WAL before forwarding. Identical retries in the same session replay the cached result with a `[sidefx] Duplicate call not re-sent` notice instead of writing twice. If the server wedges past `--timeout`, opt-in `--restart-on-timeout` kills the process group, marks the timed-out action `unknown`, and replays the MCP `initialize` handshake cleanly for subsequent calls.
2. **CLI wrapper** (`sidefx wrap -- git push origin feat/x` or `sidefx wrap -- gh issue create ...`): auto-derives deterministic action IDs for `git push` and `gh issue/pr create|comment`. On a timeout retry, it reconciles read-only (`git ls-remote` or GitHub API lookup via an embedded `<!-- sidefx:id=... -->` HTML comment) and returns the original URL/ref with zero duplicates.
3. **Claude Code `Stop` hook** (`sidefx init --claude-code --write` or install the plugin): reads `.sidefx/journal.db` strictly read-only at the end of a turn. Completely silent when 0 external writes occurred; emits a compact receipt when writes happened, and prints a loud warning if any action is still `pending` or `unknown`.

Quick setup:

```bash
# Install CLI (pure Python 3.11+ stdlib, zero runtime dependencies)
uv tool install sidefx

# Or install as a Claude Code plugin:
# /plugin marketplace add 1solomonwakhungu/sidefx
# /plugin install sidefx@sidefx

# Or install the Agent Skill (skills.sh):
npx skills add 1solomonwakhungu/sidefx
```

Every blocking error (`OUTCOME UNKNOWN`, `Preflight check denied`, `Payload mismatch`) includes exact recovery steps (`sidefx reconcile` / `sidefx resolve --landed|--not-landed`) right in the error message so Claude knows what to do without guessing.

You can run the built-in chaos test suite locally (real local HTTP mock GitHub server, bare git repo with fault hooks, subprocess MCP server, and 4-process concurrency race):

```bash
uvx sidefx eval --verbose
```

Passes 6/6 scenarios with 0 duplicate remote writes and 100.0% receipt accuracy. Happy to hear what MCP servers or CLI tools you would want reconcilers for next.

---

### B. `r/mcp`

**Title:** sidefx: A zero-dependency stdio MCP proxy that adds write-ahead SQLite journaling, preflight rules, and timeout recovery to any MCP server

**Body:**
Most stdio MCP servers do not implement idempotency keys, and many single-threaded servers wedge or drop responses under load. When an MCP client times out waiting on a mutating `tools/call`, the model has no way to tell whether the write landed and often retries with the same or slightly reworded arguments.

I built **sidefx**, a transparent stdio JSON-RPC 2.0 proxy in pure Python 3.11+ stdlib (zero third-party runtime dependencies): https://github.com/1solomonwakhungu/sidefx

Drop it in front of any existing stdio MCP server config:

```json
{
  "command": "sidefx",
  "args": ["mcp-proxy", "--deterministic-ids", "--timeout", "30", "--", "npx", "-y", "@modelcontextprotocol/server-github"]
}
```

What the proxy handles:

- **Read-only passthrough:** `initialize`, `tools/list`, `resources/list`, and read-classified tools (`get_*`, `list_*`, `read_*`, `search_*`, configurable in `.sidefx.json`) go straight through with **~0.02 ms** median overhead (~0.033 ms vs ~0.014 ms direct stdio in a single-run benchmark on macOS arm64).
- **Write-ahead SQLite WAL journal:** Mutating calls atomically claim `(actor, target, action_id)` as `pending` (`PRAGMA synchronous=FULL`, plus `fullfsync=ON` on macOS) before touching upstream. Median overhead in a single run on macOS arm64 is **~8.0 ms** (two disk flushes). Raw tool arguments are never stored in SQLite, only SHA-256 hashes.
- **Deterministic deduplication & explicit keys:** Clients can pass `_sidefx_action_id` (stripped before forwarding to upstream) or `idempotency_key`, or turn on `--deterministic-ids` for session-scoped deduplication across proxy restarts (via `.sidefx/current-session`). Replayed calls return in **~0.044 ms** median with a `[sidefx] Duplicate call not re-sent` notice.
- **Automatic protocol guidance:** Appends a short protocol note to the upstream `initialize` response's `result.instructions` (preserving existing server instructions; disable with `--no-inject-instructions`).
- **Deterministic preflight rules (`.sidefx.json`):** Enforce `required: true`, `min`/`max` numeric bounds, string allowlists/denylists, and domain allowlists/denylists (including URL/email parsing hardening) before forwarding. Denied calls return JSON-RPC `-32001`.
- **Fail-closed timeouts & wedged-server recovery:** Timed-out or crashed calls are marked `unknown` and return `-32002 OUTCOME UNKNOWN` with `sidefx reconcile` / `sidefx resolve` commands in both `error.message` and `error.data`. Opt into `--restart-on-timeout` for single-threaded servers that wedge permanently: on the next call, the proxy probes with `ping`, kills the process group if unresponsive, fails any orphaned requests cleanly, and replays `initialize` + `notifications/initialized` transparently.

Also includes `sidefx wrap` for mutating shell commands (`git push`, `gh`, `curl`), read-only reconcilers for GitHub and `git push`, and `sidefx report` for ground-truth session receipts.

Run the chaos evaluation anywhere, or run the benchmark script from a git clone:

```bash
uvx sidefx eval --verbose
git clone https://github.com/1solomonwakhungu/sidefx.git && cd sidefx && uv run python evals/benchmark.py
```

6/6 chaos scenarios pass with 0 duplicate remote writes and 100.0% receipt accuracy. Curious how other MCP server authors are handling crash/timeout idempotency today.

---

## 3. X / Twitter Launch Thread

**Post 1 (Hook + Problem)**
Coding agents keep hitting two bugs on external writes:

1. A `gh issue create`, `git push`, or MCP call times out after landing -> the agent retries and posts twice.
2. Session summaries come from chat memory, not execution logs.

I built `sidefx` to fix both with SQLite.

**Post 2 (MCP Proxy Demo)**
Put `sidefx mcp-proxy --deterministic-ids --` in front of any stdio MCP server (Claude Code, Cursor, Codex):

- Read-only calls pass through (~+0.02 ms)
- Mutating calls log `pending` in SQLite WAL first (~+8 ms on macOS)
- Duplicate retries replay from disk in ~0.04 ms

**Post 3 (Preflight Guardrails)**
Add deterministic rules in `.sidefx.json` before any upstream tool runs:

- Cap refund amounts (`required: true`, `max: 50`)
- Block pushes to `main` or `release/*`
- Restrict `send_email` to allowed domains

Violations fail closed with `-32001` and never reach the server.

**Post 4 (CLI Wrapper + Crash Reconciliation)**
Wrap shell writes directly:

`sidefx wrap -- git push origin feat/retries`
`sidefx wrap -- gh issue create -R acme/app --title "..." --body "..."`

On timeout retry, sidefx checks `git ls-remote` or the GitHub API read-only and returns the original ref with 0 duplicates.

**Post 5 (Ground-Truth Receipts + Claude Code Stop Hook)**
Run `sidefx report` (Markdown, text, or JSON) to print what actually landed straight from SQLite (`mode=ro`).

Wire `sidefx init --claude-code --write` for a `Stop` hook: silent on 0 writes, loud warning if anything is still `pending` or `unknown`.

**Post 6 (Chaos Eval Proof)**
Tested in a self-contained chaos suite (`uvx sidefx eval --verbose`):
- Local mock GitHub HTTP server
- Bare git repo with fault hooks
- Subprocess MCP server with an `fsync` ledger
- 4 OS processes racing on one action ID

6/6 pass, 0 duplicate writes, 100% receipt accuracy.

**Post 7 (Install + CTA)**
Pure Python 3.11+ stdlib. Zero runtime dependencies. MIT licensed.

Try it:
- `uv tool install sidefx`
- `uvx sidefx eval --verbose`
- `npx skills add 1solomonwakhungu/sidefx`

Code & docs: https://github.com/1solomonwakhungu/sidefx

---

## 4. GitHub Release Notes (`v0.1.2` — Ready to Paste)

### Summary
`v0.1.2` is the first PyPI release since `0.1.0` and ships the full public-launch audit and hardening pass across secret redaction, crash-safe error handling, owner-only SQLite permissions, MCP JSON-RPC framing, PEP 639 packaging, expanded CI, and reproducible benchmarks.

### Note for PyPI `0.1.0` Upgraders (Includes `v0.1.1`)
`v0.1.1` was tagged on GitHub for security hardening and never published separately to PyPI. Upgrading from `0.1.0` to `0.1.2` includes all `v0.1.1` changes:
- Automatic redaction of inline credentials on `git push` remotes and token-like Slack/Discord/Telegram webhook URL path segments.
- Owner-only POSIX permissions on newly created `.sidefx/` directories (`0700`) and `journal.db` (`0600`).
- Control/BiDi character stripping and length capping on `OUTCOME UNKNOWN` details and `sidefx status` output (with internal `sidefx_meta` stripped from status text).

### Security & Reliability Fixes in `v0.1.2`
- **Crash-proof, shared secret redaction (`src/sidefx/_redact.py`, `wrapper.py`, `reconcilers/git_push.py`, `mcp_proxy.py`, `guidance.py`):**
  - Guard `urlsplit()` and `.port` lookups against `ValueError` on malformed URLs (invalid ports, out-of-range ports, malformed IPv6 brackets, passwords containing `'`) and isolate redaction before finalization so a malformed stderr line can never crash `sidefx wrap` or leave an action stuck `pending`.
  - Redact URLs across any RFC 3986 scheme (`https://`, `ftp://`, `postgres://`, `ssh://`, etc.), auth headers (`Bearer`, `Basic`, `token`), known secret prefixes (`ghp_`, `github_pat_`, `xoxb-`, `sk-`/`sk_`, `AKIA`, `whsec_`), and `*_TOKEN=`/`*_SECRET=`/`*_KEY=`/`*_PASSWORD=` pairs across wrapped CLI stderr, `git ls-remote` errors, MCP upstream errors, `OUTCOME UNKNOWN` `error.data.detail`, and stored MCP result snippets.
  - Redact inline remote credentials in the remaining non-reconcilable `GitPushSpec` early-return paths (`remote.<name>.push` and `push.default=simple` mismatch).
- **Owner-only journal & session hardening (`src/sidefx/journal.py`, `src/sidefx/session.py`):**
  - Reject symlinks at `journal.db` with `JournalError` before `os.chmod` or `sqlite3.connect`.
  - Eliminate creation-time TOCTOU windows by passing `mode=0o700` on directory creation and pre-creating `journal.db` with `O_CREAT | O_EXCL` (`0o600`), tighten pre-existing user-owned `.sidefx/` directories to `0o700`, and restrict `-wal`, `-shm`, and `current-session` to `0o600` on POSIX.
- **MCP proxy framing & protocol hardening (`src/sidefx/mcp_proxy.py`):**
  - Validate JSON-RPC 2.0 `id` types on client ingress (returning `-32600 Invalid Request` on list/dict/boolean IDs instead of `-32603`) and drop/log upstream responses with invalid JSON-RPC IDs instead of forwarding them.
  - Fix an off-by-one boundary check in `_read_framed_line` when an incoming frame is exactly `MAX_MESSAGE_BYTES + 1` bytes ending in `\n`.
  - Recognize additional ambiguous gateway/transport error formats (`HTTP Error 502`, `HTTP 503`, `status code 504`, `503 Service ...`, `connection aborted`, `connection closed`) so transient failures mark `unknown` instead of `failed`.
- **CLI, launcher & reconciler fixes (`bin/sidefx`, `wrapper.py`, `cli.py`, `reconcilers/`):**
  - Update `bin/sidefx` fallback tag to `@v0.1.2` and add `python3.15` to launcher discovery.
  - Support `curl --url=<url>` and `--url <url>` target detection and GitHub REST marker injection.
  - Reject `inf`/`nan` and out-of-range float timeouts across `sidefx wrap`, `sidefx reconcile`, `GitPushReconciler`, and `GitHubReconciler`.
  - Insert a separating newline before `sidefx:` stderr diagnostics when wrapped child stderr lacks a trailing newline.
  - Explicitly close `urllib.error.HTTPError` handles in `GitHubReconciler` and preserve non-restrictive permissions on `sidefx init --force`.

### Packaging, CI & Benchmarks
- **PEP 639 packaging (`pyproject.toml`):** Migrated to SPDX `license = "MIT"` + `license-files = ["LICENSE"]`, aligned Python classifiers with tested versions (`3.11`, `3.12`, `3.13`), and excluded internal docs/workflows (`docs/launch.md`, `PLAN.md`, `AGENTS.md`, `.github`) from sdist archives.
- **Hardened CI (`.github/workflows/ci.yml`):** Added top-level `permissions: contents: read`, bumped `actions/checkout@v7` and `astral-sh/setup-uv@v7`, expanded the test matrix to Python `3.11`, `3.12`, and `3.13` with `uv run sidefx eval`, and added a `package` job verifying `uv build` + isolated wheel install + CLI smoke checks.
- **Reproducible benchmarks (`evals/benchmark.py`, `README.md`):** Added `evals/benchmark.py` for running local overhead benchmarks from a git clone and documented representative single-run measurements in `README.md`.

### Verification
- `uv run pytest`: all tests passing (including `tests/test_launch_audit_regressions.py`).
- `uv run ruff check .` & `uv run ruff format --check .`: clean.
- `uv run sidefx eval`: `6/6 passed`, `0 duplicate remote writes`, `100.0% receipt accuracy`.
- `uv build` + isolated wheel install smoke test: clean (`sidefx --version` -> `0.1.2`, `sidefx doctor --json`, `python -m sidefx --version`).
