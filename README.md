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

## Quick Start

```bash
uv tool install git+https://github.com/1solomonwakhungu/sidefx   # 1. install
cd your-repo && sidefx init                                      # 2. write .sidefx.json
```

3. Put `sidefx mcp-proxy --deterministic-ids --` in front of any stdio MCP server command (full configs for Claude Code, Cursor, and Codex are [below](#drop-in-mcp-proxy-setup)):

```json
{ "command": "sidefx", "args": ["mcp-proxy", "--deterministic-ids", "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "."] }
```

4. Wrap mutating shell commands with a stable action ID:

```bash
sidefx wrap --action-id push-feat-retries -- git push origin feat/retries
```

5. Check what happened with `sidefx status` or `sidefx report`.

The journal lives in `.sidefx/journal.db` next to `.sidefx.json`. `sidefx` writes a `.gitignore` into that directory when it creates it, so `git add -A` never picks the journal up.

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

How action IDs behave:

- **`--deterministic-ids`** derives the ID from `(session, tool, arguments)`, so a repeated identical call in the same proxy session is answered from the journal and never re-sent. The repeated result ends with a `[sidefx] Duplicate call not re-sent` notice so the agent knows no second write happened. To repeat a call on purpose, the agent adds a unique `_sidefx_action_id` argument.
- **The session is one proxy process** unless you pin it with `--session-id` or `SIDEFX_SESSION_ID`. Pin it if you want deduplication to survive a proxy or agent restart.
- **Without `--deterministic-ids` and without an explicit ID**, every call gets a random ID: it is journaled and shows up in receipts, but a retry is not recognized as a duplicate.

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

### Resolve an action no reconciler can check (`sidefx resolve`)

Built-in reconcilers cover `git push` and GitHub issues, PRs, and comments. For every other tool (an arbitrary MCP server, a deploy script, a webhook) `sidefx reconcile` reports `No reconciler available`, and the action stays `unknown`, which blocks that exact call. Check the external system yourself, then record what you found:

```bash
# The write exists: later retries replay this ref instead of writing again
sidefx resolve --action-id mcp-webhook-7 --landed --ref https://hooks.example.com/d/81 --note "seen in list_deliveries"

# The write does not exist: the same call is accepted and sent again
sidefx resolve --action-id mcp-webhook-7 --not-landed
```

- `--landed` marks the action `reconciled`; `--not-landed` marks it `failed`, which re-opens the call.
- The note is stored in the journal row, so receipts show the outcome was a manual attestation (`manually resolved as landed: seen in list_deliveries`).
- A `pending` action is refused unless you pass `--force-pending`, because a worker may still be running it.
- If the same action ID exists on several targets, pass `--target` (and `--actor`) to pick one.

---

## What the Agent Sees, and What It Should Do

Every blocking error carries its own next steps in the error text, because most MCP clients show the model only the JSON-RPC `message`. The same fields are in `error.data` for programmatic clients.

| Error | JSON-RPC code / `wrap` exit | Meaning | What to do |
|---|---|---|---|
| `OUTCOME UNKNOWN — reconcile before retrying` | `-32002` / exit `75` (`124` on the timeout itself) | A write was sent and the reply was lost. It may have landed. | Do not re-send or reword the call. Run the `sidefx reconcile` command from the message. If no reconciler covers the tool, verify with a read-only call and run `sidefx resolve --landed` or `--not-landed`, then repeat the original call unchanged. |
| `Preflight check denied tool ...` | `-32001` | A `.sidefx.json` rule rejected an argument. Nothing was sent. | Do not retry unchanged. Change the argument to satisfy the rule, or ask the user. |
| `Payload mismatch for action ...` | `-32004` / exit `2` | The action ID was already used for a different payload. Nothing was sent. | Use a new action ID for a new write, or repeat the original payload to get the original result. |
| `[sidefx] Duplicate call not re-sent` (appended to a normal result) | none | With `--deterministic-ids`, an identical call already ran in this session. | Nothing, unless a second write was intended: then repeat with a unique `_sidefx_action_id`. |

`OUTCOME UNKNOWN` `error.data` example:

```json
{
  "kind": "outcome_unknown",
  "action_id": "det-1aec4c3caa608cc3782451d3",
  "tool": "create_ticket",
  "status": "unknown",
  "detail": "upstream MCP server timed out after 30s",
  "journal": "/path/to/repo/.sidefx/journal.db",
  "reconcile_command": "sidefx reconcile --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3",
  "resolve_commands": {
    "landed": "sidefx resolve --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3 --landed",
    "not_landed": "sidefx resolve --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3 --not-landed"
  },
  "next_steps": ["Do not re-send or reword this call: the earlier attempt may have landed.", "..."]
}
```

### Paste into `AGENTS.md` / `CLAUDE.md`

```markdown
## sidefx (outbound write journal)

Mutating MCP calls and `sidefx wrap` commands in this repo are journaled by sidefx.

- `OUTCOME UNKNOWN`: the write may have landed. Never re-send or reword the call. Run the
  `sidefx reconcile --action-id <id>` command from the error. If it says no reconciler is
  available, check with a read-only call whether the write exists, then run
  `sidefx resolve --action-id <id> --landed --ref <url>` or `--not-landed`, and repeat the
  original call unchanged.
- `Preflight check denied`: a policy rule in `.sidefx.json` blocked the call. Do not retry
  unchanged; fix the argument or ask the user.
- `Payload mismatch`: an action ID was reused for a different write. Use a new ID.
- `[sidefx] Duplicate call not re-sent`: the identical call already ran this session. To run
  it again on purpose, add a unique `_sidefx_action_id` string argument.
- Wrap mutating shell commands: `sidefx wrap --action-id <stable-id> -- <command>`. Reuse the
  same ID when retrying the same command; use a new ID for a new write.
- Before summarizing work, run `sidefx report` and list side effects from it, not from memory.
```

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
