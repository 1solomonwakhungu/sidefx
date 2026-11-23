# sidefx

**Outbound effects journal, transparent stdio MCP proxy, CLI mutation wrapper, crash reconciler, and ground-truth session receipts for AI agents.**

When an AI agent mutates external state (`gh issue create`, `git push`, MCP `tools/call`, webhooks, payments), two failure modes happen repeatedly:

1. **Timeouts and crashes cause duplicate writes** — the agent retries without knowing whether the first call already landed on the remote service.
2. **Handoff summaries miss or hallucinate side effects** — session wrap-ups are written from compressed model context instead of durable execution records.

`sidefx` places a durable SQLite write-ahead journal (`WAL` + `synchronous=FULL`) in front of mutating MCP and CLI calls:

- **Write-ahead intent log:** Atomically claims `(actor, target, action_id)` as `pending` *before* dispatching any external write. Rejects modified payloads under a reused `action_id` and blocks blind retries with `OUTCOME UNKNOWN — reconcile before retrying`.
- **Read-only crash reconciliation:** Recovers lost URLs/refs after response-loss crashes or timeouts (GitHub Issues/PRs/Comments via embedded `<!-- sidefx:id=<marker> -->` comment markers, and `git push` via read-only `git ls-remote`) instead of double-posting.
- **Deterministic preflight guardrails:** Enforces `.sidefx.json` numeric caps (`min`/`max`), string allowlists/denylists, and domain allowlists/denylists before any upstream tool is called.
- **Ground-truth receipts:** Renders compact Markdown, plain-text, or JSON receipts strictly from SQLite rows via `sidefx report` (and integrates directly with Claude Code's `Stop` hook).

**Zero LLM calls. Zero third-party runtime dependencies. Pure Python standard library (`>=3.11`).**

---

## Installation

### Run without installing (`uvx`)

```bash
uvx --from git+https://github.com/1solomonwakhungu/sidefx sidefx --help
```

### Install globally with `uv` or `pip`

```bash
# uv tool (recommended)
uv tool install git+https://github.com/1solomonwakhungu/sidefx

# pip
pip install git+https://github.com/1solomonwakhungu/sidefx
```

### Local editable install (development)

```bash
git clone https://github.com/1solomonwakhungu/sidefx.git
cd sidefx
uv sync --dev
uv run sidefx --version
```

---

## Drop-In MCP Proxy Setup

Wrap any stdio MCP server with `sidefx mcp-proxy [options] -- <upstream_cmd ...>`.

Read-only tools (`get_*`, `list_*`, `read_*`, `search_*`, etc.) and protocol methods (`initialize`, `tools/list`, `resources/list`) pass straight through. Mutating `tools/call` requests pass through preflight guardrails, write a durable `pending` journal row, and are protected against duplicate dispatch on timeout or crash.

Clients can pass an explicit `_sidefx_action_id` (stripped before forwarding to the upstream server) or `idempotency_key`, or enable `--deterministic-ids` for session-scoped deduplication.

### Claude Code (`.mcp.json` or `~/.claude.json`)

```json
{
  "mcpServers": {
    "github": {
      "command": "sidefx",
      "args": [
        "mcp-proxy",
        "--deterministic-ids",
        "--timeout",
        "30",
        "--",
        "docker",
        "run",
        "-i",
        "--rm",
        "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"
      }
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "sidefx",
      "args": [
        "mcp-proxy",
        "--target",
        "mcp:filesystem",
        "--deterministic-ids",
        "--",
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "."
      ]
    }
  }
}
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.github]
command = "sidefx"
args = [
  "mcp-proxy",
  "--deterministic-ids",
  "--timeout",
  "30",
  "--",
  "docker",
  "run",
  "-i",
  "--rm",
  "-e",
  "GITHUB_PERSONAL_ACCESS_TOKEN",
  "ghcr.io/github/github-mcp-server",
]
```

> **Tip:** If you haven't installed `sidefx` globally, use `"command": "uvx"` with `["--from", "git+https://github.com/1solomonwakhungu/sidefx", "sidefx", "mcp-proxy", ...]`.

---

## CLI Mutation Wrapper (`sidefx wrap`)

Wrap mutating shell commands (`gh`, `git push`, `curl`, deploy scripts) with `--action-id`, `--target`, and `--timeout`:

### GitHub issues / PRs / comments (`gh`)

```bash
sidefx wrap \
  --action-id issue-fix-retry-loop \
  --timeout 15 \
  -- gh issue create -R acme/app --title "Fix retry loop" --body "Repro steps..."
```

- Automatically injects a compact marker `<!-- sidefx:id=<marker> -->` into the outgoing body.
- If `gh` times out or crashes after the HTTP POST already landed, retrying the same command reconciles via the GitHub REST API (checking both `open` and `closed` items) and returns the exact created URL with **zero duplicate issues**.

### Git push (`git push`)

```bash
sidefx wrap \
  --action-id push-feat-retries \
  --timeout 20 \
  -- git push origin feat/retries
```

- Records the local target commit SHA and pre-push remote ref SHA before pushing.
- On timeout, retrying reconciles via read-only `git ls-remote` and recovers the exact remote ref (`origin refs/heads/feat/retries@<sha12>`) without pushing twice.

### Webhooks & REST POSTs (`curl`)

```bash
sidefx wrap \
  --action-id notify-deploy-v1-4 \
  --target https://hooks.example.com/deploys \
  --timeout 10 \
  -- curl -fsS -X POST https://hooks.example.com/deploys \
       -H "Content-Type: application/json" \
       -d '{"release":"v1.4.0"}'
```

- If `curl` targets a GitHub REST issues/PRs/comments POST endpoint, `sidefx wrap` also injects the reconciliation marker into the JSON `body` field and verifies `html_url` before marking `confirmed`.

---

## Deterministic Preflight Guardrails (`.sidefx.json`)

Scaffold a starter config file in your repository root:

```bash
sidefx init
```

Configure tool classification and deterministic preflight rules in `.sidefx.json`:

```json
{
  "version": 1,
  "journal_path": ".sidefx/journal.db",
  "default_actor": "agent",
  "default_timeout_seconds": 30.0,
  "tools": {
    "read": ["check_status"],
    "mutate": ["run_migration"],
    "read_prefixes": ["get_", "list_", "read_", "search_", "describe_", "fetch_", "show_", "find_"],
    "mutate_prefixes": [
      "create_", "update_", "delete_", "remove_", "post_", "send_",
      "push_", "merge_", "refund_", "charge_", "write_", "put_",
      "patch_", "close_", "reopen_", "comment_"
    ],
    "unknown_tool_policy": "mutate"
  },
  "preflight": {
    "rules": [
      {
        "tool": "refund_payment",
        "arg": "amount",
        "min": 0.01,
        "max": 50,
        "reason": "Refunds above $50 require human approval"
      },
      {
        "tool": "push_*",
        "arg": "branch",
        "deny": ["main", "master", "release/*"],
        "reason": "Direct pushes to protected branches are blocked"
      },
      {
        "tool": "send_email",
        "arg": "to",
        "allow_domains": ["acme.com"],
        "deny_domains": ["competitor.com"],
        "reason": "Automated emails may only be sent within acme.com"
      },
      {
        "tool": "create_issue",
        "arg": "repo",
        "allow": ["acme/*"],
        "reason": "Only acme/* repositories are allowed"
      }
    ]
  }
}
```

Supported rule fields:
- `tool`: exact tool name, glob pattern (e.g. `push_*`), or `"*"` (default).
- `arg`: argument key or dotted nested path (e.g. `recipient.email`).
- Constraints (at least one required): `min`, `max`, `allow`, `deny`, `allow_domains`, `deny_domains`.
- `reason`: optional human-readable explanation returned in the JSON-RPC `-32001` denial error.

---

## Inspecting Status & Reconciling Unknown Outcomes

### Check journal status

```bash
# Compact human-readable summary
sidefx status

# Filter to unresolved actions as JSON
sidefx status --status pending --status unknown --json
```

### Reconcile pending or unknown actions

```bash
# Reconcile all unresolved (pending/unknown) actions across all sessions
sidefx reconcile

# Scope reconciliation to a single session
sidefx reconcile --session-id sess-2026-11-22

# Dry-run across all sessions (overrides an inherited --session-id) without modifying SQLite
sidefx reconcile --all --dry-run

# Reconcile a single action ID
sidefx reconcile --action-id issue-fix-retry-loop

# After verifying a crashed worker is dead, allow marking stale pending rows failed on not_found
sidefx reconcile --stale-pending-after 60
```

`sidefx reconcile` is fail-closed: it only transitions an action to `reconciled` on an exact external match (`found`), and only transitions to `failed` on an exhaustive, un-truncated `not_found`. Any HTTP error, auth error, or pagination truncation leaves the row `unknown`.

---

## Ground-Truth Session Receipts (`sidefx report`)

Render an uneditable receipt built strictly from SQLite rows (supports `--format markdown`, `--format text`, and `--format json`):

```bash
sidefx report --format markdown --session-id sess-2026-11-22
```

Sample output:

```markdown
## sidefx Session Receipt
- **Session:** `sess-2026-11-22`
- **Journal:** `/path/to/repo/.sidefx/journal.db`
- **Totals:** 4 total (1 confirmed, 1 reconciled, 1 failed, 1 unknown)

### Confirmed (1)
- `gh-issue-42` · `cli` · `gh_issue_create` · target `acme/app` -> `https://github.com/acme/app/issues/42`

### Reconciled (1)
- `git-push-feat` · `cli` · `git_push` · target `origin:refs/heads/feat/retries` -> `origin refs/heads/feat/retries@9f3c2a1b4d8e`

### Failed (1)
- `mcp-ticket-9` · `mcp` · `create_ticket` · target `mcp:linear` — error: `validation error: missing team_id`

### Unknown / Unresolved (1)
- `mcp-webhook-7` · [unknown] · `mcp` · `post_webhook` · target `mcp:hooks` — error: `upstream MCP server timed out after 30s` (marker: `2f88db28d824e145511b6f9c`)
```

### Claude Code `Stop` Hook Integration

Print the ready-to-paste Claude Code `Stop` hook snippet (by default it reports all sessions in the journal; pass `--session-id <id>` and/or `--db <path>` to scope the generated hook command):

```bash
sidefx init --claude-code
```

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sidefx report --format markdown"
          }
        ]
      }
    ]
  }
}
```

Or merge it safely and idempotently directly into `.claude/settings.json`:

```bash
sidefx init --claude-code --write
# or target a custom settings file:
sidefx init --claude-code --settings ~/.claude/settings.json
```

---

## Chaos Fault-Injection Benchmark (`sidefx eval`)

`sidefx` ships with a self-contained chaos evaluation harness that spins up a real local HTTP mock GitHub server, a real local bare git repository with fault-injecting `pre-receive`/`post-receive` hooks, a real subprocess stdio MCP server with an `fsync`-backed ledger, and concurrent OS worker processes.

Run it yourself:

```bash
sidefx eval
sidefx eval --verbose
sidefx eval --json
sidefx eval --scenario post_commit_response_loss --verbose
# or directly from the repository:
uv run python evals/chaos_suite.py
```

Latest benchmark results (`uv run sidefx eval`):

| Scenario | What It Injects | Status | Duplicate Writes | Remote Writes | Receipt Accuracy |
|---|---|---|---:|---:|---:|
| `pre_commit_crash_and_timeout` | Pre-write timeout & crash across GitHub CLI, `git push` (`pre-receive`), and MCP proxy; plus HTTP 503 uncertain reconcile | PASS | **0** | 3/3 | — |
| `post_commit_response_loss` | Post-write timeout & crash before reply across GitHub CLI, `git push` (slow `post-receive`), and MCP proxy | PASS | **0** | 5/5 | — |
| `concurrent_duplicate_dispatch` | 4 concurrent OS worker processes racing on `sidefx wrap` with the same `action_id` | PASS | **0** | 1/1 | — |
| `payload_mutation_reused_id` | Modified arguments/command under an existing `action_id` across CLI `wrap` and `mcp-proxy` | PASS | **0** | 3/3 | — |
| `preflight_policy_enforcement` | Numeric cap (`amount > 50`), protected branch denylist (`main`), and email domain allow/deny rules through `mcp-proxy` | PASS | **0** | 2/2 | — |
| `session_receipt_ground_truth` | Mixed CLI + MCP session (`confirmed`, `reconciled`, `failed`, `unknown`) verified against remote server state (JSON & Markdown) | PASS | **0** | 5/5 | **100.0%** |

**Totals:** `6/6 passed` · `0 duplicate remote writes` · `100.0% receipt accuracy`

---

## Architecture & Guarantees

- **Zero runtime dependencies:** Requires only Python `>=3.11` (`sqlite3`, `json`, `urllib`, `subprocess`, `hashlib`, `uuid`, `argparse`, `threading`, `http.server`). Fast startup, no native compilation, CI-tested on Python 3.11 and 3.13.
- **Durable SQLite WAL journal (`src/sidefx/journal.py`):** Opens with `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL` (plus `PRAGMA fullfsync=ON` on macOS), `UNIQUE (actor, target, action_id)`, and attempt-fenced `BEGIN IMMEDIATE` state transitions.
- **Read-only queries never mutate state:** `sidefx status`, `sidefx report`, and `sidefx reconcile --dry-run` open SQLite in strict read-only URI mode (`mode=ro`, `PRAGMA query_only=ON`).
- **Sanitized receipts & terminal output:** ANSI escape sequences, control characters, and Markdown link/heading injection payloads in external tool outputs are stripped and safely code-fenced before rendering.
