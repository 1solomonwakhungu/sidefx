# AGENTS.md — sidefx

## Mission
Build **sidefx**, an outbound effects journal, transparent stdio MCP proxy, CLI mutation wrapper, crash/timeout reconciler, and ground-truth session side-effects receipt tool for AI agents.

## Tech Stack & Constraints
- **Language:** Python `>=3.11` managed with `uv`.
- **Runtime dependencies:** **Zero** third-party runtime dependencies (Python standard library only: `sqlite3`, `json`, `urllib`, `subprocess`, `hashlib`, `uuid`, `argparse`, `threading`) so startup is fast and installs cleanly across Python 3.11–3.15.
- **Dev dependencies:** `pytest`, `ruff`.
- **Package layout:** `src/sidefx/` with console entry point `sidefx = "sidefx.cli:main"`.
- **Safety first:** Write-ahead durable SQLite journaling (WAL mode + `synchronous=FULL`) BEFORE any mutating side effect is dispatched. Never blindly re-execute a `pending` action whose outcome is unknown.

## Quality Bar
- `uv run pytest` passes 100% with zero skipped tests.
- `uv run ruff check .` and `uv run ruff format --check .` pass cleanly.
- Chaos fault-injection tests prove **zero duplicate remote writes** across pre-commit crashes, post-commit response-loss crashes, and concurrent worker retries.
- Clean, human-scannable receipts built strictly from durable journal rows.

## Mandatory Worker PR, Review & Merge Workflow
Every implementation worker thread owns its PR end-to-end (the orchestrator does NOT review or merge PRs):
1. Work in your assigned git worktree branched from `origin/main`.
2. Implement your milestone cleanly with thorough unit + integration tests.
3. Run `uv run pytest && uv run ruff check . && uv run ruff format --check .`.
4. Commit with clear commit messages, push to `origin`, open a PR targeting `main` via `gh pr create`, and call `link_pull_request` with the full PR URL.
5. **Mandatory Fable 5.2 Review:**
   - Call `orchestrator_capabilities` once in your thread before your first `delegate_task`.
   - Start a review delegate using `delegate_task` with:
     - `role: "review"`
     - `mode: "wait"` (or `"async"` and wait for wakeup)
     - `target: { "providerInstanceId": "claudeAgent_claude_proxy", "model": "claude-fable-5-2", "options": { "effort": "high" } }`
   - Pass the PR URL/number, `git diff origin/main...HEAD` (or instructions to inspect it), and test output. Ask Fable 5.2 for an adversarial review covering concurrency/race conditions, crash windows, SQLite durability, JSON-RPC framing edge cases, security, and test coverage.
   - Fix all valid findings, re-run tests & ruff, and push updates. If changes were non-trivial, run a fresh `delegate_task` re-check with Fable 5.2 (`claudeAgent_claude_proxy` / `claude-fable-5-2`) until approved with no blocking issues.
6. **CI & Self-Merge:**
   - Check PR checks (`gh pr view <num> --json statusCheckRollup`). If GitHub Actions CI is still running, call `watch_pull_request` and end your turn to wake on completion (do NOT use `sleep` or `gh --watch`).
   - If `origin/main` moved while you worked and there are conflicts, `git fetch origin && git rebase origin/main`, resolve conflicts, re-run tests, and push.
   - Once Fable 5.2 approves and CI is green, merge your PR yourself:
     `gh pr merge <num> --squash --delete-branch`
   - Report the merged PR URL, Fable 5.2 review summary, and final test results in your final message.
