---
name: sidefx
description: 'Use BEFORE running any external write that must not happen twice (git push, gh issue/pr create or comment, curl POST/PUT/PATCH/DELETE, webhooks, deploy or publish scripts) by prefixing it with `sidefx wrap --`. Use IMMEDIATELY when any command or MCP tool reports OUTCOME UNKNOWN (JSON-RPC -32002), Preflight check denied (-32001), or Payload mismatch (-32004), when a mutating command timed out or crashed and you are about to retry, when asked whether a write actually landed, before summarizing external changes made in a session (`sidefx report`), or when putting `sidefx mcp-proxy` in front of a stdio MCP server. Journals each side effect in local SQLite so retries are idempotent and receipts are ground truth. No LLM calls. Not for local-only commands (git commit, build, test) or read-only calls. First command: uvx sidefx wrap -- <command>'
license: MIT
compatibility: Requires uv (uvx) or pipx; Python >= 3.11. CLI wrapper and stdio MCP proxy work in any harness. Hook auto-wiring (init --claude-code) is Claude Code only.
---

# sidefx

**Run `sidefx ...`** (if not on `PATH`, prefix with `uvx sidefx ...`; no `uv`? use `pipx run sidefx ...`).

Durable SQLite write-ahead journal for external mutations (`git push`, `gh`, `curl`, webhooks, deploy/publish scripts, and stdio MCP servers) so timeouts, crashes, and retries never double-send, and session receipts come from disk rather than memory.

## When to Run

| Situation | Action |
| --- | --- |
| About to run an external write (`git push`, `gh issue/pr create/comment`, `curl POST/PUT/PATCH/DELETE`, webhook, deploy/publish) | Prefix with `sidefx wrap -- <cmd>` (pass `--action-id <id>` for `curl`/scripts and auto-ID exceptions below; pass `--timeout <sec>` if it may take >30s) |
| Any command or MCP tool output contains `OUTCOME UNKNOWN` (`-32002`, exit `124`/`75`, or child's own non-zero code) | **STOP. Do not retry or reword.** Follow the recovery loop below: run the printed `sidefx reconcile ... --action-id <id>` command first |
| An **unwrapped** mutating command timed out or crashed | Do not re-run blindly. Check remote state read-only (`gh issue list --search "<title>"`, `git ls-remote`), then run via `sidefx wrap -- <cmd>` only if it did not land |
| `sidefx reconcile` still reports `unknown` | Verify remote state read-only, then record it with `sidefx resolve --action-id <id> --landed --ref "<url-or-id>" --note "<how verified>"` (or `--not-landed`) |
| Output contains `Preflight check denied` (`-32001`) | Nothing was sent; do not retry unchanged. Fix the argument to satisfy `.sidefx.json` or ask the user |
| Output contains `Payload mismatch` (`-32004` or exit `2`) | The action ID was already used for a different payload. Use a new `--action-id` for a new write |
| Before summarizing external changes made in this session | `sidefx report --format markdown` — report side effects from stdout, not memory |
| Wiring Claude Code's `Stop` receipt hook | Run `sidefx doctor` first. Only run `sidefx init --claude-code --write` if `sidefx` is installed on `PATH` and hooks are not already wired by the plugin |

## Wrap External CLI Mutations

`sidefx wrap` defaults to a **30-second timeout** (kills the process tree and exits `124`). Pass `--timeout <seconds>` for slower deploys/pushes.

```bash
# git push and gh issue/pr create|comment auto-derive deterministic action IDs:
sidefx wrap -- git push origin feat/my-branch
sidefx wrap -- gh issue create -R owner/repo --title "Bug" --body "Details..."

# curl, webhooks, and deploy scripts need an explicit --action-id (and --timeout if >30s):
sidefx wrap --action-id webhook-order-42 -- curl -fsS -X POST https://api.example.com/hook -d '{"id":42}'
sidefx wrap --timeout 180 --action-id deploy-prod-v1.4.2 -- ./scripts/deploy.sh prod

# Only when the user explicitly asked for a second identical write: pass --random-action-id.
# Never use --random-action-id to get past OUTCOME UNKNOWN.
```

- **Auto-ID exceptions:** `gh` falls back silently to a random ID for `--fill`, `--web`, `--editor`, and `--body-file -` (and `--body-file` skips marker injection); prefer `--body` or pass `--action-id`. `git push` falls back to a random ID for `--tags`, `--all`, `--mirror`, `--follow-tags`, multi-refspec, and detached `HEAD`; pass `--action-id` for those.
- **Compound commands:** `wrap` executes one argv with no shell. In `npm test && git push`, wrap only the mutating segment: `npm test && sidefx wrap -- git push`.

## Recover from `OUTCOME UNKNOWN`

Trigger: stderr or an MCP error contains `OUTCOME UNKNOWN` (MCP `-32002`; `wrap` exits `124` on timeout, `75` when blocking a retry, or the child's own non-zero exit code / `128+sig`). Read the child's stderr first (a rejected push or `"PR already exists"` also lands here). Do **not** re-send, reword, or change the action ID.

1. Run the exact `sidefx reconcile ... --action-id <id>` command printed in the error (keep any `--db <path>`). Note: automatic read-only reconciliation covers `sidefx wrap` of `git push` and `gh` (not MCP calls, which have no injected markers). If `reconcile` stays `unknown` because the row is `pending` from a killed run and no process is still running it, add `--force-pending`.
2. If still `unknown`, check the remote service read-only, then attest:
   ```bash
   sidefx resolve --action-id <id> --landed --ref "<url-or-id>" --note "<how verified>"
   # or:
   sidefx resolve --action-id <id> --not-landed --note "<how verified>"
   ```
3. **If landed: stop.** The write already exists; do **not** run the command again (especially important when the original ID was random, which would duplicate the write). **If not landed:** re-run the exact original command line.

## Stdio MCP Proxy (MCP Config Only)

**Put this in your MCP client config (Claude Code, Cursor, Codex); do not run `mcp-proxy` in a shell**, because it reads JSON-RPC on stdin and will block:

```json
{
  "command": "uvx",
  "args": ["sidefx", "mcp-proxy", "--deterministic-ids", "--", "npx", "-y", "@modelcontextprotocol/server-github"]
}
```

- `--deterministic-ids` deduplicates identical calls in the same session and appends `[sidefx] Duplicate call not re-sent` to replayed results; pass a unique `_sidefx_action_id` argument (stripped before forwarding) only when a second identical write is truly intended.
- Opt into `--restart-on-timeout` only for single-threaded upstream MCP servers that wedge permanently after timeout.

## Receipts, Sessions & Exit Codes

```bash
sidefx report --format markdown   # ground-truth receipt for current session (--all for all sessions, --json)
sidefx status --json              # inspect pending / unknown / landed rows
sidefx session current            # show active session ID (`session new` rotates, unless SIDEFX_SESSION_ID or CLAUDE_CODE_SESSION_ID is set)
sidefx doctor --json              # check PATH, Python >= 3.11, .sidefx.json, journal DB, Claude hook
```

- Exit `0`: success or reconciled | `2`: usage or payload mismatch | `75`: blocked retry or 5-second retry cooldown (wait and re-run) | `124`: `wrap` timeout | child's non-zero code (or `128+sig`): wrapped command failed or signaled.

## Limits

- Run from the repo root, or run `sidefx init` once to create `.sidefx.json`; otherwise the journal defaults to `<cwd>/.sidefx/journal.db`, and a retry from another directory will miss earlier rows.
- Wrapped `gh` commands append an invisible `<!-- sidefx:id=... -->` HTML comment to the body for crash reconciliation.
- Do not wrap local-only commands (`git commit`, `pytest`, builds) or read-only lookups.
