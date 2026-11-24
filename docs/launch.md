# sidefx Launch Assets & Release Notes

Ready-to-use launch copy grounded in verified behavior, real chaos evaluation results (`6/6 passed`, `0 duplicate remote writes`, `100.0% receipt accuracy`), and measured benchmarks (`uv run python evals/benchmark.py` on macOS arm64, Python 3.13.14, SQLite 3.53.1).

---

## 1. Show HN

### Title
Show HN: sidefx – SQLite write-ahead journal and MCP proxy so AI agents stop double-posting

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

Privacy and security choices: the journal stores SHA-256 hashes of arguments/argv rather than raw payloads, redacts inline URL credentials, webhook path tokens, and Bearer/Basic tokens before storing targets or stderr lines, sanitizes ANSI/BiDi/Markdown injection in receipts, and writes `.sidefx/` with `0700`/`0600` permissions plus a `.gitignore`.

Numbers you can reproduce yourself:

- Chaos fault-injection suite (`uv run sidefx eval`): spins up a real local mock GitHub HTTP server, a real bare git repo with fault-injecting `pre-receive`/`post-receive` hooks, a subprocess stdio MCP server with an `fsync`-backed ledger, and 4 concurrent OS workers racing on the same action ID. Result: 6/6 scenarios passed, 0 duplicate remote writes, 100.0% receipt accuracy (17/17 checks) in 13.1s.
- Overhead (`uv run python evals/benchmark.py` on macOS arm64, Python 3.13.14, SQLite 3.53.1):
  - MCP read-only passthrough: 0.033 ms median (+0.02 ms over direct stdio)
  - MCP mutating write-ahead call: 7.98 ms median (+7.96 ms, dominated by two `fullfsync` WAL commits)
  - MCP duplicate replay (`--deterministic-ids`): 0.044 ms median, zero upstream calls
  - CLI `sidefx wrap`: 69.0 ms median (+67.0 ms over bare `/usr/bin/true`, mostly Python startup)

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

1. **Drop-in MCP proxy** in `.mcp.json`: wrap any stdio MCP server with `sidefx mcp-proxy --deterministic-ids -- <existing server command>`. Read-only calls pass straight through (+0.02 ms median overhead). Mutating calls are checked against `.sidefx.json` preflight rules (like capping refund amounts, blocking pushes to `main`, or restricting email domains) and logged as `pending` in SQLite WAL before forwarding. Identical retries in the same session replay the cached result with a `[sidefx] Duplicate call not re-sent` notice instead of writing twice. If the server wedges past `--timeout`, opt-in `--restart-on-timeout` kills the process group, marks the timed-out action `unknown`, and replays the MCP `initialize` handshake cleanly for subsequent calls.
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

- **Read-only passthrough:** `initialize`, `tools/list`, `resources/list`, and read-classified tools (`get_*`, `list_*`, `read_*`, `search_*`, configurable in `.sidefx.json`) go straight through with **0.02 ms** median overhead (0.033 ms vs 0.014 ms direct stdio in `evals/benchmark.py`).
- **Write-ahead SQLite WAL journal:** Mutating calls atomically claim `(actor, target, action_id)` as `pending` (`PRAGMA synchronous=FULL`, plus `fullfsync=ON` on macOS) before touching upstream. Median overhead on macOS arm64 is **7.96 ms** (two disk flushes). Raw tool arguments are never stored in SQLite, only SHA-256 hashes.
- **Deterministic deduplication & explicit keys:** Clients can pass `_sidefx_action_id` (stripped before forwarding to upstream) or `idempotency_key`, or turn on `--deterministic-ids` for session-scoped deduplication across proxy restarts (via `.sidefx/current-session`). Replayed calls return in **0.044 ms** median with a `[sidefx] Duplicate call not re-sent` notice.
- **Automatic protocol guidance:** Appends a short protocol note to the upstream `initialize` response's `result.instructions` (preserving existing server instructions; disable with `--no-inject-instructions`).
- **Deterministic preflight rules (`.sidefx.json`):** Enforce `required: true`, `min`/`max` numeric bounds, string allowlists/denylists, and domain allowlists/denylists (including URL/email parsing hardening) before forwarding. Denied calls return JSON-RPC `-32001`.
- **Fail-closed timeouts & wedged-server recovery:** Timed-out or crashed calls are marked `unknown` and return `-32002 OUTCOME UNKNOWN` with `sidefx reconcile` / `sidefx resolve` commands in both `error.message` and `error.data`. Opt into `--restart-on-timeout` for single-threaded servers that wedge permanently: on the next call, the proxy probes with `ping`, kills the process group if unresponsive, fails any orphaned requests cleanly, and replays `initialize` + `notifications/initialized` transparently.

Also includes `sidefx wrap` for mutating shell commands (`git push`, `gh`, `curl`), read-only reconcilers for GitHub and `git push`, and `sidefx report` for ground-truth session receipts.

Benchmarks and chaos evaluation are included in the repo:

```bash
uvx sidefx eval --verbose
uv run python evals/benchmark.py
```

6/6 chaos scenarios pass with 0 duplicate remote writes and 100.0% receipt accuracy. Curious how other MCP server authors are handling crash/timeout idempotency today.

---

## 3. X / Twitter Launch Thread

**Post 1 (Hook + Problem)**
AI coding agents keep hitting two ugly bugs when they touch external systems:

1. A `gh issue create`, `git push`, or MCP call times out after landing on the server -> the agent retries and posts twice.
2. End-of-session summaries are written from compressed chat history instead of what actually executed.

I built `sidefx` to fix both with SQLite and zero LLM calls.

**Post 2 (MCP Proxy Demo)**
Put `sidefx mcp-proxy --deterministic-ids --` in front of any stdio MCP server in Claude Code, Cursor, or Codex:

- Read-only calls pass straight through (+0.02 ms median overhead)
- Mutating calls write a durable `pending` row to SQLite WAL before forwarding (+7.96 ms on macOS `fullfsync`)
- Duplicate retries replay from disk in 0.04 ms without touching upstream

**Post 3 (Preflight Guardrails)**
Add deterministic guardrails in `.sidefx.json` before any upstream tool runs:

- Cap refund amounts (`required: true`, `max: 50`)
- Block pushes to `main` or `release/*`
- Restrict `send_email` to allowed domains

Violations fail closed with JSON-RPC `-32001` and never reach the server.

**Post 4 (CLI Wrapper + Crash Reconciliation)**
Wrap shell mutations directly:

`sidefx wrap -- git push origin feat/retries`
`sidefx wrap -- gh issue create -R acme/app --title "..." --body "..."`

If `gh` or `git push` times out after the write lands, retrying reconciles read-only (`git ls-remote` or GitHub API via `<!-- sidefx:id=... -->`) and returns the exact URL or ref instead of double-posting.

**Post 5 (Ground-Truth Receipts + Claude Code Stop Hook)**
Run `sidefx report` (Markdown, text, or JSON) to print what actually landed straight from SQLite (`mode=ro`, `PRAGMA query_only=ON`).

Wire `sidefx init --claude-code --write` for a `Stop` hook that stays silent when 0 writes occurred and warns loudly if anything is left `pending` or `unknown`.

**Post 6 (Chaos Eval Proof)**
Every guarantee is tested in a self-contained chaos test suite (`uvx sidefx eval --verbose`):
- Local mock GitHub HTTP server
- Bare git repo with fault-injecting receive hooks
- Subprocess stdio MCP server with an `fsync` ledger
- 4 OS processes racing on the same action ID

Result: 6/6 scenarios pass, 0 duplicate remote writes, 100.0% receipt accuracy.

**Post 7 (Install + CTA)**
Pure Python 3.11+ stdlib. Zero runtime dependencies. MIT licensed.

Try it:
- `uv tool install sidefx`
- `uvx sidefx eval --verbose`
- `npx skills add 1solomonwakhungu/sidefx`

Code & docs: https://github.com/1solomonwakhungu/sidefx

---

## 4. GitHub Release Notes (Ready to Paste for Next Release)

### Summary
Public launch audit and hardening release covering security redaction, POSIX file permissions, MCP JSON-RPC framing edge cases, timeout validation, PEP 639 packaging, expanded CI, and reproducible latency benchmarks.

### Security & Reliability Fixes
- **Credential redaction in error strings (`src/sidefx/wrapper.py`, `src/sidefx/reconcilers/git_push.py`):**
  - Redact inline-credential URLs, query strings, token-like webhook path segments, and `Bearer`/`Basic` auth tokens from child `stderr` lines before persisting to SQLite `actions.error` or echoing in `OUTCOME UNKNOWN`.
  - Redact inline credentials in `git ls-remote` failure messages and in the remaining non-reconcilable `GitPushSpec` early-return paths (`remote.<name>.push` and `push.default=simple` mismatch).
- **Owner-only journal & session permissions (`src/sidefx/journal.py`, `src/sidefx/session.py`):**
  - Eliminate creation-time TOCTOU windows by passing `mode=0o700` to directory creation and atomically pre-creating `journal.db` with `0o600` (`O_CREAT | O_EXCL`).
  - Restrict SQLite `-wal` and `-shm` sidecars and `current-session` (`O_CREAT | O_EXCL` path) to `0o600` on POSIX.
- **MCP proxy framing & error hardening (`src/sidefx/mcp_proxy.py`):**
  - Validate JSON-RPC 2.0 `id` types on client ingress (returning `-32600 Invalid Request` instead of `-32603` on unhashable list/dict or boolean IDs) and guard upstream reader lookups against malformed IDs.
  - Fix an off-by-one boundary check in `_read_framed_line` when an incoming frame is exactly `MAX_MESSAGE_BYTES + 1` bytes ending in `\n`.
  - Sanitize and length-cap `error.data["detail"]` on `OUTCOME UNKNOWN` responses (matching `error.message`).
  - Recognize additional ambiguous gateway/transport error formats (`HTTP Error 502`, `HTTP 503`, `status code 504`, `503 Service ...`, `connection aborted`, `connection closed`) so transient errors mark `unknown` instead of `failed`.
- **CLI & launcher polish (`bin/sidefx`, `src/sidefx/wrapper.py`, `src/sidefx/cli.py`, `src/sidefx/reconcilers/`):**
  - Update `bin/sidefx` fallback tag from non-existent `@v0.1.0` to `@v0.1.1` and add `python3.15` to launcher discovery.
  - Support `curl --url=<url>` and `--url <url>` target detection and GitHub REST marker injection.
  - Reject `inf`/`nan` and out-of-range float timeouts across `sidefx wrap`, `sidefx reconcile`, `GitPushReconciler`, and `GitHubReconciler`.
  - Insert a separating newline before `sidefx:` stderr diagnostics when wrapped child `stderr` lacks a trailing newline.
  - Close `urllib.error.HTTPError` response handles explicitly in `GitHubReconciler` and preserve non-restrictive file permissions on `sidefx init --force`.

### Packaging, CI & Benchmarks
- **PEP 639 packaging (`pyproject.toml`):** Switched to SPDX `license = "MIT"` and `license-files = ["LICENSE"]`.
- **Expanded CI (`.github/workflows/ci.yml`):** Added Python 3.12 to the test matrix (`3.11`, `3.12`, `3.13`), added `uv run sidefx eval` to CI, and added a dedicated `package` job that builds sdist + wheel (`uv build`) and verifies clean installation and CLI execution in an isolated virtual environment.
- **Reproducible benchmarks (`evals/benchmark.py` & `README.md`):** Added a standalone benchmark runner measuring SQLite WAL journal latency, MCP proxy read/mutate/replay overhead, and CLI `sidefx wrap` overhead, and documented the measured results in `README.md`.

### Verification
- `uv run pytest`: all tests passing (including new regression suite in `tests/test_launch_audit_regressions.py`).
- `uv run ruff check .` & `uv run ruff format --check .`: clean.
- `uv run sidefx eval`: `6/6 passed`, `0 duplicate remote writes`, `100.0% receipt accuracy`.
- `uv build` + isolated wheel install smoke test: clean (`sidefx --version`, `sidefx doctor --json`, `python -m sidefx --version`).
