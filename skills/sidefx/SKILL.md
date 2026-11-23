---
name: sidefx
description: Use sidefx to protect mutating CLI commands and MCP tool calls with write-ahead journaling, recover cleanly from OUTCOME UNKNOWN timeouts/crashes without duplicate writes, and produce ground-truth session receipts.
---

# sidefx — Outbound Effects Safety & Receipts

When this workspace uses `sidefx`, mutating MCP tool calls and wrapped shell commands are logged in a durable SQLite write-ahead journal (`.sidefx/journal.db`) *before* external writes run.

## Rules for Agents

### 1. Wrap mutating CLI commands
Run mutating shell commands through `sidefx wrap`:

```bash
# git push and gh issue/pr create/comment auto-derive deterministic action IDs:
sidefx wrap -- git push origin feat/my-branch
sidefx wrap -- gh issue create -R owner/repo --title "Bug" --body "Details"

# For other mutating commands (curl, deploy scripts), pass a stable --action-id:
sidefx wrap --action-id deploy-v1-4 -- ./scripts/deploy.sh

# To intentionally create a second identical write, pass --random-action-id (or a new --action-id):
sidefx wrap --random-action-id -- gh issue comment 42 --body "Bump"
```

### 2. Handle MCP duplicate suppression & `_sidefx_action_id`
- Pass an optional `_sidefx_action_id` string argument on mutating MCP tool calls (stripped before reaching the upstream server) to pin an explicit idempotency key.
- When `--deterministic-ids` is enabled, repeating an identical mutating call in the same session replays the cached result and appends `[sidefx] Duplicate call not re-sent`. Do not assume a second write occurred.
- If you genuinely intend to execute a second identical write, repeat the call with a unique `_sidefx_action_id`.

### 3. Recover from `OUTCOME UNKNOWN` (-32002 / exit 75 or 124)
Never blindly re-send or reword a call that returned `OUTCOME UNKNOWN`: the earlier write may already have landed on the remote service.
1. Run the exact `sidefx reconcile --action-id <id>` command shown in the error (covers `git push` and GitHub issues/PRs/comments read-only).
2. If `sidefx reconcile` reports no reconciler is available, inspect remote state with a read-only call, then record what you verified:
   - If the write exists: `sidefx resolve --action-id <id> --landed --ref <url-or-id> --note "<how verified>"`
   - If the write does not exist: `sidefx resolve --action-id <id> --not-landed --note "<how verified>"`
3. Repeat the original call unchanged: it replays the recorded result (if `--landed`) or executes cleanly (if `--not-landed`).

### 4. Respect Preflight Denials (`-32001`)
`Preflight check denied` means a deterministic `.sidefx.json` policy rule blocked the arguments before any write was dispatched. Never retry unchanged—fix the arguments to satisfy the rule or ask the user.

### 5. Sessions, Diagnostics & Ground-Truth Receipts
- Before summarizing completed work, run `sidefx report` (or `sidefx status`) and report external side effects from the durable journal rather than memory.
- Inspect or rotate the workspace session with `sidefx session current` and `sidefx session new`.
- Verify local setup anytime with `sidefx doctor`.
