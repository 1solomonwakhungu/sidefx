import type { SurfaceTone } from "@/components/tonal/surface-tile";
import type { TonalChipProps } from "@/components/tonal/chip";
import type { KpiMetric } from "@/components/tonal/kpi-grid";
import type { AccordionItemData } from "@/components/tonal/accordion";

export const SITE_META = {
  version: "v0.1.2",
  repoUrl: "https://github.com/1solomonwakhungu/sidefx",
  pypiUrl: "https://pypi.org/project/sidefx/",
  harloUrl: "https://harlo.dev",
  tonalUrl: "https://tonal.harlo.dev",
  skillsUrl: "https://skills.sh",
};

export const NAV_LINKS = [
  { href: "#architecture", label: "Architecture" },
  { href: "#setup", label: "Setup" },
  { href: "#contract", label: "Contract" },
  { href: "#chaos-evals", label: "Chaos Evals" },
  { href: "#benchmarks", label: "Benchmarks" },
  { href: "#faq", label: "FAQ" },
] as const;

export type InstallMethodId = "uv-pip" | "uvx" | "claude-plugin" | "agent-skill";

export interface InstallMethod {
  id: InstallMethodId;
  label: string;
  caption: string;
  command: string;
  copyText: string;
}

export const INSTALL_METHODS: InstallMethod[] = [
  {
    id: "uv-pip",
    label: "Install (uv / pip)",
    caption:
      "Install globally with uv tool (or pip install sidefx), then run sidefx init inside your repo.",
    command: `uv tool install sidefx && sidefx init`,
    copyText: "uv tool install sidefx && sidefx init",
  },
  {
    id: "uvx",
    label: "Zero-Install (uvx)",
    caption:
      "Run the self-contained chaos evaluation suite (or uvx sidefx --help) without installing globally.",
    command: `uvx sidefx eval --verbose`,
    copyText: "uvx sidefx eval --verbose",
  },
  {
    id: "claude-plugin",
    label: "Claude Plugin",
    caption: "Ships with .claude-plugin/plugin.json, Stop hook, SKILL.md, and zero-dep bin/sidefx launcher.",
    command: `/plugin marketplace add 1solomonwakhungu/sidefx\n/plugin install sidefx@sidefx`,
    copyText: "/plugin marketplace add 1solomonwakhungu/sidefx\n/plugin install sidefx@sidefx",
  },
  {
    id: "agent-skill",
    label: "Agent Skill",
    caption: "Install via skills.sh into Claude Code, Codex, Cursor, Antigravity, and OpenCode.",
    command: `npx skills add 1solomonwakhungu/sidefx`,
    copyText: "npx skills add 1solomonwakhungu/sidefx",
  },
];

export type SetupTabId =
  | "claude-code"
  | "cursor"
  | "codex"
  | "cli-wrap"
  | "preflight"
  | "receipt";

export interface SetupExample {
  id: SetupTabId;
  label: string;
  filename: string;
  language: string;
  docsAnchor: string;
  copyLabel: string;
  summary: string;
  note: string;
  code: string;
}

export const SETUP_EXAMPLES: SetupExample[] = [
  {
    id: "claude-code",
    label: "Claude Code",
    filename: ".mcp.json",
    language: "json",
    docsAnchor: "#drop-in-mcp-proxy-setup",
    copyLabel: "Copy",
    summary:
      "Wrap any stdio MCP server in Claude Code (.mcp.json or ~/.claude.json). Read-only calls pass straight through; mutating calls journal write-ahead in SQLite.",
    note: "Tip: If you have not installed sidefx globally, set \"command\": \"uvx\" and prepend \"sidefx\" to args.",
    code: `{
  "mcpServers": {
    "github": {
      "command": "sidefx",
      "args": [
        "mcp-proxy",
        "--target",
        "mcp:github",
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
        "GITHUB_PERSONAL_ACCESS_TOKEN": "\${GITHUB_PERSONAL_ACCESS_TOKEN}"
      }
    }
  }
}`,
  },
  {
    id: "cursor",
    label: "Cursor",
    filename: ".cursor/mcp.json",
    language: "json",
    docsAnchor: "#drop-in-mcp-proxy-setup",
    copyLabel: "Copy",
    summary:
      "Prefix any Cursor stdio MCP server with sidefx mcp-proxy --deterministic-ids -- for session-scoped deduplication and automatic initialize protocol guidance.",
    note: "When --target is omitted, wrapper launchers like npx, uvx, node, python3, and docker run are automatically unwrapped into readable target names.",
    code: `{
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
}`,
  },
  {
    id: "codex",
    label: "Codex",
    filename: "~/.codex/config.toml",
    language: "toml",
    docsAnchor: "#drop-in-mcp-proxy-setup",
    copyLabel: "Copy",
    summary:
      "Drop-in TOML configuration for Codex CLI MCP servers with deterministic action IDs and 30-second fail-closed timeouts.",
    note: "Session IDs persist in .sidefx/current-session across proxy restarts; inspect or rotate them with sidefx session current and sidefx session new.",
    code: `[mcp_servers.github]
command = "sidefx"
args = [
  "mcp-proxy",
  "--target",
  "mcp:github",
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
]`,
  },
  {
    id: "cli-wrap",
    label: "CLI Wrap",
    filename: "gh / git push / curl",
    language: "bash",
    docsAnchor: "#cli-mutation-wrapper-sidefx-wrap",
    copyLabel: "Copy",
    summary:
      "Wrap mutating shell commands directly. git push and gh issue/pr create|comment derive deterministic action IDs automatically and reconcile read-only on timeout.",
    note: "For gh writes and GitHub REST curl POSTs, sidefx injects a hidden <!-- sidefx:id=<marker> --> marker so retries recover the exact URL without double-posting.",
    code: `# GitHub issues / PRs / comments (auto-derives session-scoped action ID)
sidefx wrap \\
  --timeout 15 \\
  -- gh issue create -R acme/app --title "Fix retry loop" --body "Repro steps..."

# Git push (auto-derives ID from remote, ref, and local SHA; reconciles via git ls-remote)
sidefx wrap \\
  --timeout 20 \\
  -- git push origin feat/retries

# Webhooks & REST POSTs (pin --action-id and reuse it on retries)
sidefx wrap \\
  --action-id notify-deploy-v1-4 \\
  --target https://hooks.example.com/deploys \\
  --timeout 10 \\
  -- curl -fsS -X POST https://hooks.example.com/deploys \\
       -H "Content-Type: application/json" \\
       -d '{"release":"v1.4.0"}'`,
  },
  {
    id: "preflight",
    label: "Preflight",
    filename: ".sidefx.json",
    language: "json",
    docsAnchor: "#deterministic-preflight-guardrails-sidefxjson",
    copyLabel: "Copy",
    summary:
      "Scaffold with sidefx init to configure read/mutate tool classification and deterministic preflight guardrails evaluated before any upstream tool runs.",
    note: "Violations fail closed with JSON-RPC -32001 and never reach the upstream server. Setting required: true prevents omitted-argument bypasses.",
    code: `{
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
        "required": true,
        "min": 0.01,
        "max": 50,
        "reason": "Refunds require an amount <= $50 (higher refunds require human approval)"
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
}`,
  },
  {
    id: "receipt",
    label: "Session Receipt",
    filename: "sidefx report --format markdown",
    language: "markdown",
    docsAnchor: "#ground-truth-session-receipts-sidefx-report",
    copyLabel: "Copy sample",
    summary:
      "Render compact Markdown, plain-text, or JSON receipts strictly from SQLite rows opened in read-only URI mode (mode=ro, PRAGMA query_only=ON).",
    note: "Wire Claude Code's Stop hook with sidefx init --claude-code --write: silent when 0 writes occurred, loud warning whenever pending/unknown actions remain.",
    code: `## sidefx Session Receipt
- **Session:** \`sess-2026-11-22\`
- **Journal:** \`/path/to/repo/.sidefx/journal.db\`
- **Totals:** 4 total (1 confirmed, 1 reconciled, 1 failed, 1 unknown)

### Confirmed (1)
- \`gh-issue-42\` · \`cli\` · \`gh_issue_create\` · target \`acme/app\` -> \`https://github.com/acme/app/issues/42\`

### Reconciled (1)
- \`git-push-feat\` · \`cli\` · \`git_push\` · target \`origin:refs/heads/feat/retries\` -> \`origin refs/heads/feat/retries@9f3c2a1b4d8e\`

### Failed (1)
- \`mcp-ticket-9\` · \`mcp\` · \`create_ticket\` · target \`mcp:linear\` — error: \`validation error: missing team_id\`

### Unknown / Unresolved (1)
- \`mcp-webhook-7\` · [unknown] · \`mcp\` · \`post_webhook\` · target \`mcp:hooks\` — error: \`upstream MCP server timed out after 30s\` (marker: \`2f88db28d824e145511b6f9c\`)`,
  },
];

export type ArchitectureIconKey =
  | "database"
  | "arrow-left-right"
  | "refresh-cw"
  | "shield-check"
  | "file-check"
  | "lock";

export interface ArchitectureCard {
  id: string;
  icon: ArchitectureIconKey;
  tone: SurfaceTone;
  chipLabel: string;
  chipTone: TonalChipProps["tone"];
  title: string;
  description: string;
  highlights: string[];
}

export const ARCHITECTURE_CARDS: ArchitectureCard[] = [
  {
    id: "sqlite-wal",
    icon: "database",
    tone: "sky",
    chipLabel: "Durability",
    chipTone: "blue",
    title: "Write-Ahead SQLite WAL Journal",
    description:
      "Atomically claims every mutating intent as pending before dispatching any external call, preventing race-condition double writes across concurrent workers.",
    highlights: [
      "PRAGMA journal_mode=WAL + synchronous=FULL (+ fullfsync=ON on macOS)",
      "Attempt-fenced BEGIN IMMEDIATE state transitions",
      "UNIQUE (actor, target, action_id) deduplication key",
    ],
  },
  {
    id: "mcp-proxy",
    icon: "arrow-left-right",
    tone: "cool",
    chipLabel: "JSON-RPC 2.0",
    chipTone: "neutral",
    title: "Transparent Stdio MCP Proxy",
    description:
      "Drops in front of any stdio MCP server in Claude Code, Cursor, or Codex. Read-only calls pass straight through while mutations gain idempotency and timeout recovery.",
    highlights: [
      "--deterministic-ids & automatic initialize protocol guidance",
      "Stable workspace session in .sidefx/current-session across restarts",
      "Opt-in --restart-on-timeout process-group recovery & handshake replay",
    ],
  },
  {
    id: "reconciler",
    icon: "refresh-cw",
    tone: "mint",
    chipLabel: "Fail-Closed",
    chipTone: "mint",
    title: "Read-Only Crash Reconciler",
    description:
      "Recovers lost URLs and commit refs after response-loss crashes or timeouts instead of posting duplicates, and never guesses on HTTP or pagination errors.",
    highlights: [
      "git push reconciled via read-only git ls-remote",
      "GitHub Issues/PRs/Comments via <!-- sidefx:id=<marker> -->",
      "Manual attestation via sidefx resolve --landed | --not-landed",
    ],
  },
  {
    id: "preflight",
    icon: "shield-check",
    tone: "sand",
    chipLabel: "Guardrails",
    chipTone: "warning",
    title: "Deterministic Preflight Rules",
    description:
      "Evaluates .sidefx.json argument constraints locally before any upstream server is touched, returning structured JSON-RPC -32001 errors on policy denials.",
    highlights: [
      "required: true stops omitted-argument bypasses",
      "Numeric min/max caps and glob allow/deny lists",
      "Hardened allow_domains / deny_domains for URLs and emails",
    ],
  },
  {
    id: "receipts",
    icon: "file-check",
    tone: "warm",
    chipLabel: "Ground Truth",
    chipTone: "neutral",
    title: "Session Receipts & Claude Code Stop Hook",
    description:
      "Renders Markdown, plain-text, or JSON summaries directly from durable SQLite rows rather than compressed model memory.",
    highlights: [
      "Strict read-only SQLite URI (mode=ro + PRAGMA query_only=ON)",
      "sidefx hook claude-code stays completely silent at 0 writes",
      "Surfaces loud warnings whenever pending or unknown rows exist",
    ],
  },
  {
    id: "security",
    icon: "lock",
    tone: "neutral",
    chipLabel: "Hardening",
    chipTone: "mint",
    title: "Privacy & Security Hardening",
    description:
      "Built for real credentials and untrusted remote output: never persists raw tool arguments and strips secrets before writing targets, snippets, or errors.",
    highlights: [
      "Stores SHA-256 argument/argv hashes instead of raw payloads",
      "Redacts URL credentials, webhook tokens, Bearer/Basic & API keys",
      "ANSI/BiDi/Markdown sanitization + 0700/0600 perms & auto-.gitignore",
    ],
  },
];

export interface ContractRow {
  error: string;
  code: string;
  codeTone: TonalChipProps["tone"];
  meaning: string;
  action: string;
}

export const CONTRACT_ROWS: ContractRow[] = [
  {
    error: "OUTCOME UNKNOWN — reconcile before retrying",
    code: "-32002 · exit 75 / 124 / child code",
    codeTone: "danger",
    meaning:
      "A write was sent and the reply was lost or ambiguous. It may have landed on the remote service.",
    action:
      "Do not re-send or reword the call. Run the sidefx reconcile command from the message. If still unknown, verify with a read-only call and run sidefx resolve --landed or --not-landed. If landed, stop; if not landed, re-run the original command.",
  },
  {
    error: "Preflight check denied tool ...",
    code: "-32001",
    codeTone: "warning",
    meaning: "A .sidefx.json policy rule rejected an argument. Nothing was sent.",
    action:
      "Do not retry unchanged. Update the argument to satisfy the rule, or ask the user for approval.",
  },
  {
    error: "Payload mismatch for action ...",
    code: "-32004 · exit 2",
    codeTone: "warning",
    meaning:
      "The action ID was already used for a different payload. Nothing was sent.",
    action:
      "Use a new action ID for a new write, or repeat the original payload to receive the original result.",
  },
  {
    error: "[sidefx] Duplicate call not re-sent",
    code: "none (appended to result)",
    codeTone: "blue",
    meaning:
      "With --deterministic-ids, an identical call already ran in this session.",
    action:
      "Do nothing unless a second write was explicitly intended — then repeat with a unique _sidefx_action_id argument.",
  },
];

export const OUTCOME_UNKNOWN_EXAMPLE = `{
  "kind": "outcome_unknown",
  "action_id": "det-1aec4c3caa608cc3782451d3",
  "marker": "2f88db28d824e145511b6f9c",
  "tool": "create_ticket",
  "status": "unknown",
  "detail": "upstream MCP server timed out after 30s",
  "journal": "/path/to/repo/.sidefx/journal.db",
  "reconcile_command": "sidefx reconcile --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3",
  "resolve_commands": {
    "landed": "sidefx resolve --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3 --landed",
    "not_landed": "sidefx resolve --db /path/to/repo/.sidefx/journal.db --action-id det-1aec4c3caa608cc3782451d3 --not-landed"
  },
  "next_steps": [
    "Do not re-send or reword this call: the earlier attempt may have landed."
  ]
}`;

export const AGENTS_MD_SNIPPET = `## sidefx (outbound write journal)

Mutating MCP calls and \`sidefx wrap\` commands in this repo are journaled by sidefx.

- \`OUTCOME UNKNOWN\`: the write may have landed. Never re-send or reword the call. Run the
  \`sidefx reconcile --action-id <id>\` command from the error. If it still reports \`unknown\`,
  check with a read-only call whether the write exists, then run
  \`sidefx resolve --action-id <id> --landed --ref <url>\` (and stop) or \`--not-landed\` (then
  re-run the original command).
- \`Preflight check denied\`: a policy rule in \`.sidefx.json\` blocked the call. Do not retry
  unchanged; fix the argument or ask the user.
- \`Payload mismatch\`: an action ID was reused for a different write. Use a new ID.
- \`[sidefx] Duplicate call not re-sent\`: the identical call already ran this session. To run
  it again on purpose, add a unique \`_sidefx_action_id\` string argument.
- Wrap mutating shell commands: \`sidefx wrap -- <command>\` (\`git push\` and \`gh issue/pr
  create|comment\` auto-derive stable IDs; pass \`--action-id <stable-id>\` for \`curl\`/scripts
  and reuse the same ID on retries, or use a new ID for a new write).
- Before summarizing work, run \`sidefx report\` and list side effects from it, not from memory.`;

export const HEADLINE_KPIS: KpiMetric[] = [
  {
    id: "chaos-passed",
    title: "Chaos Scenarios",
    value: "6/6",
    delta: "PASS",
    tone: "mint",
    subtitle: "13.1s total runtime (uv run sidefx eval)",
  },
  {
    id: "duplicate-writes",
    title: "Duplicate Remote Writes",
    value: "0",
    delta: "Verified",
    tone: "mint",
    subtitle: "Across crashes, timeouts & 4-worker races",
  },
  {
    id: "receipt-accuracy",
    title: "Receipt Accuracy",
    value: "100.0%",
    delta: "Ground truth",
    tone: "blue",
    subtitle: "Verified against remote state (JSON & MD)",
  },
  {
    id: "mcp-read-overhead",
    title: "MCP Read Passthrough",
    value: "~+0.02 ms",
    delta: "No WAL write",
    tone: "blue",
    subtitle: "~0.033 ms proxy vs ~0.014 ms direct stdio",
  },
  {
    id: "duplicate-replay",
    title: "Duplicate Replay",
    value: "~0.044 ms",
    delta: "0 upstream",
    tone: "neutral",
    subtitle: "Answered from journal (--deterministic-ids)",
  },
];

export interface ChaosScenarioRow {
  scenario: string;
  injects: string;
  status: "PASS";
  duplicateWrites: string;
  remoteWrites: string;
  receiptAccuracy: string;
}

export const CHAOS_SCENARIOS: ChaosScenarioRow[] = [
  {
    scenario: "pre_commit_crash_and_timeout",
    injects:
      "Pre-write timeout & crash across GitHub CLI, git push (pre-receive), and MCP proxy; plus HTTP 503 uncertain reconcile",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "3/3",
    receiptAccuracy: "—",
  },
  {
    scenario: "post_commit_response_loss",
    injects:
      "Post-write timeout & crash before reply across GitHub CLI, git push (slow post-receive), and MCP proxy",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "5/5",
    receiptAccuracy: "—",
  },
  {
    scenario: "concurrent_duplicate_dispatch",
    injects:
      "4 concurrent OS worker processes racing on sidefx wrap with the same action_id",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "1/1",
    receiptAccuracy: "—",
  },
  {
    scenario: "payload_mutation_reused_id",
    injects:
      "Modified arguments/command under an existing action_id across CLI wrap and mcp-proxy",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "3/3",
    receiptAccuracy: "—",
  },
  {
    scenario: "preflight_policy_enforcement",
    injects:
      "Numeric cap (amount > 50), protected branch denylist (main), and email domain allow/deny rules through mcp-proxy",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "2/2",
    receiptAccuracy: "—",
  },
  {
    scenario: "session_receipt_ground_truth",
    injects:
      "Mixed CLI + MCP session (confirmed, reconciled, failed, unknown) verified against remote server state (JSON & Markdown)",
    status: "PASS",
    duplicateWrites: "0",
    remoteWrites: "5/5",
    receiptAccuracy: "100.0%",
  },
];

export interface BenchmarkRow {
  operation: string;
  n: number;
  median: string;
  p95: string;
  notes: string;
}

export const BENCHMARK_ROWS: BenchmarkRow[] = [
  {
    operation: "Journal get_action lookup",
    n: 300,
    median: "~0.005 ms",
    p95: "~0.006 ms",
    notes: "Indexed SQLite read",
  },
  {
    operation: "Journal begin_action replay hit",
    n: 300,
    median: "~0.011 ms",
    p95: "~0.012 ms",
    notes: "Hash check + cached status hit",
  },
  {
    operation: "Journal begin_action + mark_confirmed",
    n: 100,
    median: "~8.0 ms",
    p95: "~8.2 ms",
    notes: "Two durable fullfsync WAL commits",
  },
  {
    operation: "MCP direct stdio baseline (get_item)",
    n: 80,
    median: "~0.014 ms",
    p95: "~0.032 ms",
    notes: "Unwrapped local Python MCP server",
  },
  {
    operation: "MCP proxy read-only passthrough",
    n: 80,
    median: "~0.033 ms",
    p95: "~0.058 ms",
    notes: "~+0.02 ms median overhead (no SQLite write)",
  },
  {
    operation: "MCP proxy mutating write-ahead (create_item)",
    n: 80,
    median: "~8.0 ms",
    p95: "~9.9 ms",
    notes: "~+8.0 ms median overhead (preflight + 2 durable commits)",
  },
  {
    operation: "MCP proxy duplicate replay (--deterministic-ids)",
    n: 80,
    median: "~0.044 ms",
    p95: "~0.18 ms",
    notes: "Answered from journal; zero upstream calls",
  },
  {
    operation: "CLI bare /usr/bin/true baseline",
    n: 20,
    median: "~2.1 ms",
    p95: "~2.4 ms",
    notes: "Baseline process spawn",
  },
  {
    operation: "CLI sidefx wrap (new action)",
    n: 20,
    median: "~69 ms",
    p95: "~78 ms",
    notes: "~+67 ms median overhead (Python startup + WAL commits)",
  },
  {
    operation: "CLI sidefx wrap (replayed hit)",
    n: 20,
    median: "~55 ms",
    p95: "~59 ms",
    notes: "Replayed from journal without spawning child",
  },
];

export const FAQ_ITEMS: AccordionItemData[] = [
  {
    question: "How do built-in reconcilers differ from sidefx resolve?",
    answer:
      "Built-in read-only reconcilers cover git push (via git ls-remote) and GitHub issues, pull requests, and comments (via GitHub REST GET looking for the embedded <!-- sidefx:id=<marker> --> marker across both open and closed items). They fail closed: any HTTP error, auth error, or pagination truncation keeps the action unknown. For any tool without a built-in reconciler (custom MCP servers, deploy scripts, or arbitrary webhooks), sidefx reconcile reports 'No reconciler available' and keeps the call blocked until you check the remote system yourself and run sidefx resolve --action-id <id> --landed --ref <url> (marks reconciled and replays on retries) or --not-landed (marks failed and re-opens the call). Pending rows require --force-pending so an active worker is not clobbered.",
  },
  {
    question: "How does --deterministic-ids work, and how do I intentionally run a call twice?",
    answer:
      "With --deterministic-ids enabled on sidefx mcp-proxy, each mutating call derives its action ID from (session, tool, arguments). An identical repeat inside the same session is answered from SQLite in ~0.044 ms with zero upstream calls and appends a '[sidefx] Duplicate call not re-sent' notice so the model knows no second write happened. Session IDs persist in .sidefx/current-session across proxy restarts (rotate with sidefx session new). When you intentionally want a second write with identical arguments, pass a unique _sidefx_action_id string argument — sidefx strips _sidefx_action_id before forwarding to the upstream MCP server.",
  },
  {
    question: "When should I enable --restart-on-timeout on sidefx mcp-proxy?",
    answer:
      "Leave it off by default for slow-but-healthy serial servers that may still finish and deliver a late reply. Opt into --restart-on-timeout for single-threaded stdio MCP servers that wedge permanently after hitting --timeout: sidefx marks the timed-out action unknown, probes the server on the next request, terminates the full process group if unresponsive, synthesizes clean errors for any orphaned in-flight requests, and transparently replays the cached initialize + notifications/initialized handshake so later unrelated calls succeed.",
  },
  {
    question: "Does sidefx store raw payloads or secrets in .sidefx/journal.db?",
    answer:
      "No. The journal stores SHA-256 hashes of tool arguments and CLI argv rather than raw payloads. Before persisting targets, MCP snippets, or stderr diagnostics, sidefx redacts inline URL credentials across RFC 3986 schemes, query strings, token-like webhook path segments, Bearer/Basic/token headers, known API key prefixes (ghp_, github_pat_, xoxb-, sk-/sk_, AKIA, whsec_), and *_TOKEN/*_SECRET/*_KEY/*_PASSWORD pairs. On POSIX systems, .sidefx/ is created with 0700 permissions, journal.db, -wal/-shm sidecars, and current-session with 0600, symlinks at journal.db are rejected, and sidefx writes a .gitignore inside .sidefx/ so git add -A never picks up the journal.",
  },
];
