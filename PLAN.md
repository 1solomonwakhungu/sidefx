# MVP Plan — sidefx

Build a production-quality, user-ready MVP across 4 sequenced/parallel PRs.

## PR 1 (Foundation — land first on `main`)
**Branch:** `feat/foundation-journal`
- Create `pyproject.toml` (`sidefx`, Python `>=3.11`, pure stdlib runtime, `pytest` + `ruff` dev deps, `src/` layout).
- Implement durable SQLite WAL journal in `src/sidefx/journal.py`:
  - Schema: `actions` table (`action_id`, `session_id`, `actor`, `surface` [`mcp`|`cli`|`http`], `tool_name`, `target`, `payload_hash`, `marker`, `status` [`pending`|`confirmed`|`failed`|`reconciled`|`unknown`], `result_ref`, `summary`, `error`, `created_at`, `updated_at`).
  - Unique constraint on `(actor, target, action_id)` so concurrent workers racing on the same action cannot both dispatch.
  - Strict state transitions:
    - `begin_action(...)`: atomically inserts `pending` row; if row already exists:
      - If `payload_hash` differs -> raise `PayloadMismatchError` (reject mutation under reused ID).
      - If `status in ('confirmed', 'reconciled')` -> return cached result without re-executing.
      - If `status in ('pending', 'unknown')` -> return `OutcomeUnknown` decision (block blind retry until reconciled).
    - `mark_confirmed`, `mark_failed`, `mark_reconciled`, `mark_unknown`.
- Implement `.sidefx.json` config loader (`src/sidefx/config.py`) for tool classification (`read` vs `mutate`) and deterministic preflight rules.
- Implement CLI skeleton (`src/sidefx/cli.py`): `sidefx status`, `sidefx report`, `sidefx reconcile`, `sidefx wrap`, `sidefx mcp-proxy`, `sidefx init`, `--version`.
- Unit + concurrency tests (multi-process / multi-thread atomic claim tests, payload mismatch tests, crash-left-pending tests).

## PR 2 (Transparent Stdio MCP Proxy + Deterministic Preflight — parallel after PR 1)
**Branch:** `feat/mcp-proxy-preflight`
- Implement deterministic preflight evaluator in `src/sidefx/preflight.py`:
  - Numeric argument caps (e.g., `amount <= 50`, `max_rows <= 100`).
  - String allowlists / denylists (e.g., allowed email domains, allowed repos, blocked branches like `main`/`master`).
  - Read vs mutate classifier: explicit config rules + sensible default heuristics (`get_*`, `list_*`, `read_*`, `search_*` = read; `create_*`, `update_*`, `delete_*`, `post_*`, `send_*`, `push_*`, `refund_*` = mutate).
- Implement transparent stdio JSON-RPC 2.0 MCP proxy in `src/sidefx/mcp_proxy.py`:
  - Spawns wrapped upstream MCP server (`sidefx mcp-proxy -- <cmd ...>`), forwards newline-delimited JSON-RPC messages bidirectionally.
  - Passes read-only calls and protocol methods (`initialize`, `tools/list`, `resources/list`, etc.) straight through.
  - Intercepts mutating `tools/call` requests: runs preflight checks -> writes durable `pending` intent row -> forwards with configurable timeout -> records `confirmed`/`failed` on response, or leaves `pending`/`unknown` on timeout/upstream crash and returns an explicit JSON-RPC error telling the agent `OUTCOME UNKNOWN — reconcile before retrying` with the `action_id`.
  - Supports deterministic action ID derivation (explicit `_sidefx_action_id` / `idempotency_key` if present, or session-scoped hash when configured) and automatic reconciler hook on retry.
- Integration tests using a real subprocess mock stdio MCP server (normal calls, preflight denials, upstream timeout, upstream crash mid-write, retry interception).

## PR 3 (CLI Wrapper, Reconciler Adapters & Ground-Truth Receipts — parallel after PR 1)
**Branch:** `feat/reconcilers-and-receipts`
- Implement reconciler interface & adapters in `src/sidefx/reconcilers/`:
  - `base.py`: `Reconciler` protocol returning `ReconcileResult(state='found'|'not_found'|'unknown', result_ref=..., details=...)`.
  - `github.py`: GitHub Issues/PRs/Comments adapter. Injects a compact deterministic marker `<!-- sidefx:id=<marker> -->` into outgoing body when wrapping `gh issue create` / `gh pr create` / `gh issue comment` / GitHub REST POST. On timeout/crash recovery, queries GitHub API (using `urllib` + token or `gh api`, configurable base URL for testing!) across open AND closed items by exact marker + author to recover the exact created URL/number; returns `unknown` if lookup fails or is incomplete (never falsely claims not-found!).
  - `git_push.py`: Wraps `git push`, captures local target SHA before push, and reconciles timed-out pushes via read-only `git ls-remote` comparing remote ref SHA to expected local SHA.
- Implement `sidefx wrap` (`src/sidefx/wrapper.py`): wraps arbitrary CLI mutations (`gh`, `git push`, `curl`) with `--action-id`, `--target`, `--timeout`, write-ahead journaling, and automatic reconciliation on retry.
- Implement `sidefx report` & Claude Code `Stop` hook formatter (`src/sidefx/receipt.py`):
  - Renders compact, uneditable Markdown / text / JSON side-effects receipt grouped by `confirmed`, `reconciled`, `failed`, and `unknown`, straight from SQLite.
  - `sidefx init --claude-code` outputs/merges a `Stop` hook snippet for displaying the session receipt.
- Unit + integration tests against local mock HTTP GitHub server and local bare git remote!

## PR 4 (Chaos Fault-Injection Eval Harness & User Docs — final PR after PR 2 & PR 3)
**Branch:** `feat/chaos-eval-and-docs`
- Build automated chaos evaluation harness in `evals/chaos_suite.py` (exposed via `sidefx eval` and `pytest`):
  - Local mock GitHub HTTP server + mock stdio MCP server + local bare git repo.
  - Injects faults across all critical failure windows:
    1. Crash/timeout *before* remote write lands -> retry safely reconciles or flags `unknown`, never corrupts state.
    2. Crash/timeout *after* remote write lands but *before* HTTP/MCP response returns -> retry recovers exact remote URL/ID with **0 duplicate writes**.
    3. Concurrent duplicate dispatch from two workers with same `action_id` -> exactly 1 remote write.
    4. Payload mutation under reused `action_id` -> rejected before dispatch.
    5. Preflight cap/denylist violation -> blocked before dispatch, 0 remote writes.
    6. Session-end receipt completeness -> 100% match against remote server ground truth.
- Finalize `README.md` with install (`uvx`/`pip`), drop-in MCP proxy config example (Claude Code / Cursor / Codex), `sidefx wrap` examples, `.sidefx.json` preflight rules, `sidefx report` sample output, and chaos benchmark results.
- End-to-end smoke tests confirming every CLI subcommand works cleanly out of the box.
