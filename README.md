# sidefx

**Outbound effects journal, transparent MCP proxy, crash reconciler, and ground-truth session receipts for AI agents.**

When an AI agent changes the outside world (`gh issue create`, `git push`, MCP `tools/call`, webhooks, payments), two things go wrong:

1. **Timeouts & crashes cause duplicate writes** because the agent retries without knowing if the first call landed.
2. **Handoff summaries miss or hallucinate side effects** because they are written from compressed memory.

`sidefx` puts a durable SQLite write-ahead journal in front of mutating MCP and CLI calls:
- Logs intent before dispatch and blocks blind retries with `OUTCOME UNKNOWN — reconcile before retrying`.
- Recovers lost URLs/IDs via read-only reconcilers (GitHub issues/PRs, `git push`) instead of double-posting.
- Enforces optional deterministic preflight guardrails (numeric caps, allowlists/denylists).
- Prints a ground-truth `sidefx report` receipt at session end from the log, not model prose.

Zero LLM calls. Zero third-party runtime dependencies.

*MVP currently under active build — see [PLAN.md](PLAN.md).*
