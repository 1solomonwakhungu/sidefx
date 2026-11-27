"use client";

import * as React from "react";
import {
  ArrowLeftRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  Copy,
  Database,
  FileCheck2,
  Lock,
  Menu,
  Moon,
  RefreshCw,
  ShieldCheck,
  Sun,
  Terminal,
  X,
} from "lucide-react";
import { TonalButton } from "@/components/tonal/button";
import { SurfaceTile } from "@/components/tonal/surface-tile";
import { TonalChip } from "@/components/tonal/chip";
import { ChevronLink } from "@/components/tonal/chevron-link";
import { TonalAccordion } from "@/components/tonal/accordion";
import { TonalSegmentedControl } from "@/components/tonal/segmented-control";
import { KpiGrid } from "@/components/tonal/kpi-grid";
import {
  AGENTS_MD_SNIPPET,
  ARCHITECTURE_CARDS,
  BENCHMARK_ROWS,
  CHAOS_SCENARIOS,
  CONTRACT_ROWS,
  FAQ_ITEMS,
  HEADLINE_KPIS,
  INSTALL_METHODS,
  NAV_LINKS,
  OUTCOME_UNKNOWN_EXAMPLE,
  SETUP_EXAMPLES,
  SITE_META,
  type ArchitectureIconKey,
  type InstallMethodId,
  type SetupTabId,
} from "@/lib/sidefx-data";

const ARCH_ICONS: Record<
  ArchitectureIconKey,
  React.ComponentType<{ className?: string }>
> = {
  database: Database,
  "arrow-left-right": ArrowLeftRight,
  "refresh-cw": RefreshCw,
  "shield-check": ShieldCheck,
  "file-check": FileCheck2,
  lock: Lock,
};

function SidefxLogoMark({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <rect width="32" height="32" rx="10" fill="var(--t-ink)" />
      <rect x="7" y="8" width="12" height="4.5" rx="2.25" fill="var(--t-bg)" />
      <rect
        x="7"
        y="14.25"
        width="18"
        height="4.5"
        rx="2.25"
        fill="var(--t-blue)"
      />
      <rect
        x="7"
        y="20.5"
        width="9"
        height="4.5"
        rx="2.25"
        fill="var(--t-success)"
      />
      <circle cx="22.5" cy="22.75" r="2.25" fill="var(--t-success)" />
    </svg>
  );
}

function GithubIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className={className}
    >
      <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.53 1.032 1.53 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0 1 12 6.844a9.59 9.59 0 0 1 2.504.337c1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.02 10.02 0 0 0 22 12.017C22 6.484 17.522 2 12 2Z" />
    </svg>
  );
}

export function SidefxLanding() {
  const [theme, setTheme] = React.useState<"light" | "dark">("light");
  const [mobileMenuOpen, setMobileMenuOpen] = React.useState(false);
  const [installId, setInstallId] = React.useState<InstallMethodId>("uv-pip");
  const [setupTabId, setSetupTabId] = React.useState<SetupTabId>("claude-code");
  const [agentsSnippetOpen, setAgentsSnippetOpen] = React.useState(false);
  const [errorDataOpen, setErrorDataOpen] = React.useState(false);
  const [copiedKey, setCopiedKey] = React.useState<string | null>(null);
  const copyTimerRef = React.useRef<number | null>(null);
  const mobileMenuBtnRef = React.useRef<HTMLButtonElement | null>(null);

  React.useEffect(() => {
    try {
      const ds = document.documentElement.dataset;
      if (ds.tonalTheme === "dark" || ds.tonalTheme === "light") {
        setTheme(ds.tonalTheme);
      } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
        document.documentElement.dataset.tonalTheme = "dark";
        setTheme("dark");
      }
      ds.tonalMode = "pill";
    } catch {
      // Ignore storage/media errors
    }
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
    };
  }, []);

  React.useEffect(() => {
    if (!mobileMenuOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMobileMenuOpen(false);
        mobileMenuBtnRef.current?.focus();
      }
    };

    const mql = window.matchMedia?.("(min-width: 1024px)");
    const handleMediaChange = (e: MediaQueryListEvent) => {
      if (e.matches) {
        setMobileMenuOpen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    mql?.addEventListener?.("change", handleMediaChange);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      mql?.removeEventListener?.("change", handleMediaChange);
    };
  }, [mobileMenuOpen]);

  const toggleTheme = React.useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      document.documentElement.dataset.tonalTheme = next;
      try {
        window.localStorage.setItem("sidefx-theme", next);
      } catch {
        // Ignore storage errors
      }
      return next;
    });
  }, []);

  const copyText = React.useCallback(async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
      copyTimerRef.current = window.setTimeout(() => {
        setCopiedKey((current) => (current === key ? null : current));
      }, 1800);
    } catch {
      // Clipboard unavailable
    }
  }, []);

  const activeInstall =
    INSTALL_METHODS.find((m) => m.id === installId) ?? INSTALL_METHODS[0];
  const activeSetup =
    SETUP_EXAMPLES.find((s) => s.id === setupTabId) ?? SETUP_EXAMPLES[0];

  const installOptions = React.useMemo(
    () => INSTALL_METHODS.map((m) => ({ value: m.id, label: m.label })),
    []
  );

  const setupOptions = React.useMemo(
    () => SETUP_EXAMPLES.map((s) => ({ value: s.id, label: s.label })),
    []
  );

  return (
    <div className="min-h-screen bg-[var(--t-bg)] text-[var(--t-ink)] flex flex-col transition-colors duration-200">
      {/* Screen-reader live region for copy confirmations */}
      <div aria-live="polite" className="sr-only">
        {copiedKey ? "Copied to clipboard" : ""}
      </div>

      {/* Sticky Header */}
      <header className="sticky top-0 z-40 bg-[var(--t-bg)]/90 backdrop-blur-md border-b border-[var(--t-border-subtle)] transition-colors duration-200">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 h-16 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <a
              href="#"
              className="flex items-center gap-2.5 text-[17px] font-medium tracking-[-0.02em] text-[var(--t-ink)] shrink-0 rounded-[8px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <SidefxLogoMark className="h-6 w-6 shrink-0" />
              <span>sidefx</span>
            </a>

            <span
              aria-hidden="true"
              className="hidden sm:inline-block h-4 w-px bg-[var(--t-border-subtle)]"
            />

            <a
              href={SITE_META.harloUrl}
              className="hidden sm:inline-flex items-center gap-1 text-[12.5px] font-medium text-[var(--t-ink-muted)] hover:text-[var(--t-ink)] transition-colors rounded-[6px] px-1.5 py-0.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <span>harlo.dev</span>
              <ArrowUpRight className="h-3 w-3 opacity-70" />
            </a>
          </div>

          {/* Desktop Anchor Navigation */}
          <nav
            aria-label="Primary sections"
            className="hidden lg:flex items-center gap-1"
          >
            {NAV_LINKS.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className="rounded-full px-3 py-1.5 text-[13px] font-medium text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface)] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
              >
                {link.label}
              </a>
            ))}
          </nav>

          {/* Right Actions */}
          <div className="flex items-center gap-2 shrink-0">
            <a
              href={SITE_META.repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-full bg-[var(--t-surface)] hover:bg-[var(--t-surface-muted)] border border-[var(--t-border-subtle)] px-3.5 h-8 sm:h-9 text-[12.5px] font-medium text-[var(--t-ink)] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <GithubIcon className="h-3.5 w-3.5 shrink-0" />
              <span className="hidden sm:inline">GitHub</span>
            </a>

            <button
              type="button"
              onClick={toggleTheme}
              aria-label={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              title={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              className="inline-flex h-8 w-8 sm:h-9 sm:w-9 shrink-0 items-center justify-center rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface-muted)] cursor-pointer transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              {theme === "dark" ? (
                <Sun className="h-4 w-4" />
              ) : (
                <Moon className="h-4 w-4" />
              )}
            </button>

            <button
              ref={mobileMenuBtnRef}
              type="button"
              onClick={() => setMobileMenuOpen((open) => !open)}
              aria-expanded={mobileMenuOpen}
              aria-controls="mobile-nav"
              aria-label="Toggle section menu"
              className="lg:hidden inline-flex h-8 w-8 sm:h-9 sm:w-9 shrink-0 items-center justify-center rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface-muted)] cursor-pointer transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              {mobileMenuOpen ? (
                <X className="h-4 w-4" />
              ) : (
                <Menu className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>

        {/* Mobile Dropdown Navigation */}
        {mobileMenuOpen && (
          <nav
            id="mobile-nav"
            aria-label="Mobile sections"
            className="lg:hidden border-t border-[var(--t-border-subtle)] bg-[var(--t-bg)] px-5 py-3"
          >
            <div className="flex flex-wrap gap-1.5">
              {NAV_LINKS.map((link) => (
                <a
                  key={link.href}
                  href={link.href}
                  onClick={() => setMobileMenuOpen(false)}
                  className="rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] px-3.5 py-1.5 text-[13px] font-medium text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] transition-colors"
                >
                  {link.label}
                </a>
              ))}
              <a
                href={SITE_META.harloUrl}
                className="sm:hidden inline-flex items-center gap-1 rounded-full bg-[var(--t-surface-cool)] border border-[var(--t-border-subtle)] px-3.5 py-1.5 text-[13px] font-medium text-[var(--t-blue)]"
              >
                harlo.dev
                <ArrowUpRight className="h-3.5 w-3.5" />
              </a>
            </div>
          </nav>
        )}
      </header>

      <main className="flex-1 mx-auto w-full max-w-6xl px-5 sm:px-8 pt-12 sm:pt-18 pb-20 flex flex-col gap-20 sm:gap-24">
        {/* Hero Section */}
        <section className="flex flex-col items-center text-center max-w-4xl mx-auto w-full">
          <TonalChip tone="blue" className="mb-6">
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 rounded-full bg-[var(--t-success)]"
            />
            {SITE_META.version} · Zero-LLM · Python 3.11+ stdlib
          </TonalChip>

          <h1 className="t-display text-[38px] sm:text-[54px] lg:text-[62px] mb-5 max-w-3xl">
            Stop AI agents from double-posting after timeouts.
          </h1>

          <p className="t-body text-[16px] sm:text-[18px] text-[var(--t-ink-secondary)] max-w-2xl mb-8">
            <strong className="font-medium text-[var(--t-ink)]">sidefx</strong>{" "}
            puts a durable SQLite WAL write-ahead journal, transparent stdio MCP
            proxy, CLI mutation wrapper, read-only crash reconciler, and
            ground-truth session receipts in front of external writes — with
            zero runtime dependencies.
          </p>

          {/* Primary & Secondary CTAs */}
          <div className="flex flex-wrap items-center justify-center gap-3 mb-10">
            <a
              href="#setup"
              className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] rounded-[var(--t-radius-btn)] px-7 py-3.5 text-[15px] bg-[var(--t-cta-bg)] text-[var(--t-cta-fg)] hover:bg-[var(--t-cta-hover)] transition-all duration-200 [transition-timing-function:var(--t-ease)] active:scale-[0.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              Get started
            </a>
            <a
              href={SITE_META.repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] rounded-[var(--t-radius-btn)] px-7 py-3.5 text-[15px] bg-transparent text-[var(--t-ink)] border border-[var(--t-ink-muted)] hover:bg-[var(--t-hover-overlay)] transition-all duration-200 [transition-timing-function:var(--t-ease)] active:scale-[0.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <GithubIcon className="h-4 w-4" />
              View on GitHub
            </a>
          </div>

          {/* Interactive Install Command Switcher */}
          <SurfaceTile
            tone="cool"
            bordered
            padding="md"
            className="w-full max-w-3xl text-left flex flex-col gap-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <TonalSegmentedControl
                ariaLabel="Choose installation method"
                options={installOptions}
                value={installId}
                onValueChange={setInstallId}
                size="sm"
                className="max-w-full"
              />

              <TonalButton
                type="button"
                variant="tonal"
                size="sm"
                onClick={() => copyText(`install-${activeInstall.id}`, activeInstall.copyText)}
                aria-label={`Copy ${activeInstall.label} command`}
                className="shrink-0"
              >
                {copiedKey === `install-${activeInstall.id}` ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    Copy
                  </>
                )}
              </TonalButton>
            </div>

            <div className="rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-4 py-3.5 overflow-x-auto">
              <pre className="font-mono text-[13px] leading-[1.65] text-[var(--t-ink)] whitespace-pre">
                <code>{activeInstall.command}</code>
              </pre>
            </div>

            <p className="text-[13px] text-[var(--t-ink-muted)] leading-[1.5]">
              {activeInstall.caption}
            </p>
          </SurfaceTile>
        </section>

        {/* Two Failure Modes / Problem Summary Strip */}
        <section aria-label="Problems solved" className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <SurfaceTile tone="warm" bordered padding="md" className="flex flex-col gap-2.5">
            <TonalChip tone="warning" className="self-start">
              Failure Mode 01
            </TonalChip>
            <h2 className="t-headline text-[21px]">
              Timeouts and crashes cause duplicate writes
            </h2>
            <p className="t-body text-[14.5px]">
              When <code className="font-mono text-[13px] text-[var(--t-ink)]">gh issue create</code>,{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">git push</code>, or an MCP{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">tools/call</code> loses its
              reply after landing, agents retry blindly.{" "}
              <strong className="font-medium text-[var(--t-ink)]">sidefx</strong>{" "}
              records <code className="font-mono text-[13px] text-[var(--t-ink)]">pending</code>{" "}
              in SQLite WAL first, blocks retries with{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">OUTCOME UNKNOWN</code>,
              and reconciles read-only.
            </p>
          </SurfaceTile>

          <SurfaceTile tone="neutral" bordered padding="md" className="flex flex-col gap-2.5">
            <TonalChip tone="blue" className="self-start">
              Failure Mode 02
            </TonalChip>
            <h2 className="t-headline text-[21px]">
              Handoff summaries miss or hallucinate side effects
            </h2>
            <p className="t-body text-[14.5px]">
              Wrap-ups written from compressed conversation context drop pushes
              or claim failed writes succeeded.{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">sidefx report</code>{" "}
              and Claude Code&apos;s{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">Stop</code>{" "}
              hook read{" "}
              <code className="font-mono text-[13px] text-[var(--t-ink)]">.sidefx/journal.db</code>{" "}
              strictly read-only to report what actually happened.
            </p>
          </SurfaceTile>
        </section>

        {/* Interactive Setup & Output Explorer */}
        <section id="setup" className="flex flex-col gap-6">
          <div className="flex flex-col gap-2 max-w-2xl">
            <TonalChip tone="blue" className="self-start">
              Drop-In Setup
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px]">
              Configure once across MCP clients and shell commands
            </h2>
            <p className="t-body text-[15.5px]">
              Switch between real configurations and outputs straight from the
              repository — zero wrapper lock-in, works with any stdio MCP server
              or shell command.
            </p>
          </div>

          <SurfaceTile tone="neutral" bordered padding="md" className="flex flex-col gap-5">
            {/* Full-width wrapping tab switcher — all 6 tabs visible with zero clipping */}
            <div className="w-full">
              <TonalSegmentedControl
                ariaLabel="Select setup configuration or sample output"
                options={setupOptions}
                value={setupTabId}
                onValueChange={setSetupTabId}
                size="sm"
                className="max-w-full"
              />
            </div>

            {/* Dedicated metadata & copy toolbar */}
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1 border-t border-[var(--t-border-subtle)]/70">
              <div className="flex flex-wrap items-center gap-2.5 min-w-0">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-3 py-1 text-[12px] font-mono text-[var(--t-ink-secondary)]">
                  <Terminal className="h-3 w-3 text-[var(--t-ink-muted)] shrink-0" />
                  <span>{activeSetup.filename}</span>
                </span>
              </div>

              <TonalButton
                type="button"
                variant="tonal"
                size="sm"
                onClick={() => copyText(`setup-${activeSetup.id}`, activeSetup.code)}
                aria-label={`${activeSetup.copyLabel} ${activeSetup.label} snippet`}
                className="shrink-0"
              >
                {copiedKey === `setup-${activeSetup.id}` ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    {activeSetup.copyLabel}
                  </>
                )}
              </TonalButton>
            </div>

            <p className="t-body text-[14.5px] max-w-3xl">
              {activeSetup.summary}
            </p>

            <div className="rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4 sm:p-5 overflow-x-auto">
              <pre className="font-mono text-[12.5px] sm:text-[13px] leading-[1.65] text-[var(--t-ink)] whitespace-pre">
                <code>{activeSetup.code}</code>
              </pre>
            </div>

            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 pt-1 text-[13px] text-[var(--t-ink-muted)]">
              <p className="leading-[1.5]">{activeSetup.note}</p>
              <ChevronLink
                href={`${SITE_META.repoUrl}${activeSetup.docsAnchor}`}
                target="_blank"
                rel="noopener noreferrer"
                className="shrink-0"
              >
                Full docs on GitHub
              </ChevronLink>
            </div>
          </SurfaceTile>
        </section>

        {/* 6-Card Architecture Bento */}
        <section id="architecture" className="flex flex-col gap-6">
          <div className="flex flex-col gap-2 max-w-2xl">
            <TonalChip tone="mint" className="self-start">
              Architecture & Guarantees
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px]">
              Six deterministic layers, zero third-party dependencies
            </h2>
            <p className="t-body text-[15.5px]">
              Pure Python 3.11+ standard library (<code className="font-mono text-[14px] text-[var(--t-ink)]">sqlite3</code>,{" "}
              <code className="font-mono text-[14px] text-[var(--t-ink)]">json</code>,{" "}
              <code className="font-mono text-[14px] text-[var(--t-ink)]">urllib</code>,{" "}
              <code className="font-mono text-[14px] text-[var(--t-ink)]">subprocess</code>,{" "}
              <code className="font-mono text-[14px] text-[var(--t-ink)]">hashlib</code>) — fast startup, no native compilation, CI-tested on Python 3.11, 3.12, and 3.13.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
            {ARCHITECTURE_CARDS.map((card) => {
              const Icon = ARCH_ICONS[card.icon];
              return (
                <SurfaceTile
                  key={card.id}
                  tone={card.tone}
                  bordered
                  padding="md"
                  className="flex flex-col justify-between gap-5"
                >
                  <div className="flex flex-col gap-4">
                    <div className="flex items-center justify-between gap-3">
                      <div className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] text-[var(--t-ink)]">
                        <Icon className="h-5 w-5" />
                      </div>
                      <TonalChip
                        tone={card.chipTone}
                        className={
                          card.tone === "neutral"
                            ? "border border-[var(--t-border-subtle)]"
                            : "bg-[var(--t-elevated)] border border-[var(--t-border-subtle)]"
                        }
                      >
                        {card.chipLabel}
                      </TonalChip>
                    </div>

                    <div>
                      <h3 className="t-headline text-[20px] mb-2">
                        {card.title}
                      </h3>
                      <p className="t-body text-[14px]">{card.description}</p>
                    </div>
                  </div>

                  <ul className="flex flex-col gap-2 pt-3 border-t border-[var(--t-border-subtle)]/80">
                    {card.highlights.map((item) => (
                      <li
                        key={item}
                        className="flex items-start gap-2 text-[12.5px] leading-[1.45] text-[var(--t-ink-secondary)]"
                      >
                        <span
                          aria-hidden="true"
                          className="mt-1.5 h-1.5 w-1.5 rounded-full bg-[var(--t-blue)] shrink-0"
                        />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </SurfaceTile>
              );
            })}
          </div>
        </section>

        {/* Agent Error & Recovery Contract */}
        <section id="contract" className="flex flex-col gap-6">
          <div className="flex flex-col gap-2 max-w-2xl">
            <TonalChip tone="warning" className="self-start">
              Agent Recovery Contract
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px]">
              What the agent sees, and what it should do
            </h2>
            <p className="t-body text-[15.5px]">
              Every blocking response embeds exact recovery steps directly in
              the JSON-RPC <code className="font-mono text-[14px] text-[var(--t-ink)]">message</code>{" "}
              (since most MCP clients only show the text to the model) alongside
              structured fields in <code className="font-mono text-[14px] text-[var(--t-ink)]">error.data</code>.
            </p>
          </div>

          <SurfaceTile tone="white" bordered padding="none" className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[740px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-[var(--t-border-subtle)] bg-[var(--t-surface)]">
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Response / Error
                    </th>
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      JSON-RPC / Exit
                    </th>
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Meaning
                    </th>
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Required Next Step
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--t-border-subtle)]">
                  {CONTRACT_ROWS.map((row) => (
                    <tr
                      key={row.error}
                      className="align-top hover:bg-[var(--t-surface)]/50 transition-colors"
                    >
                      <td className="py-4 px-5 font-mono text-[12.5px] font-medium text-[var(--t-ink)] w-[24%]">
                        {row.error}
                      </td>
                      <td className="py-4 px-5 w-[18%]">
                        <TonalChip tone={row.codeTone} className="font-mono text-[11.5px]">
                          {row.code}
                        </TonalChip>
                      </td>
                      <td className="py-4 px-5 text-[13.5px] leading-[1.55] text-[var(--t-ink-secondary)] w-[24%]">
                        {row.meaning}
                      </td>
                      <td className="py-4 px-5 text-[13.5px] leading-[1.55] text-[var(--t-ink-secondary)] w-[34%]">
                        {row.action}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SurfaceTile>

          {/* Expandable AGENTS.md / CLAUDE.md Snippet & error.data Payload */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <SurfaceTile tone="warm" bordered padding="md" className="flex flex-col gap-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="t-headline text-[19px] mb-1">
                    Paste into AGENTS.md / CLAUDE.md
                  </h3>
                  <p className="text-[13.5px] text-[var(--t-ink-secondary)]">
                    Drop-in instructions so coding agents follow the exact
                    reconcile, resolve, and receipt workflow.
                  </p>
                </div>
                <TonalButton
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={() => copyText("agents-md", AGENTS_MD_SNIPPET)}
                  aria-label="Copy AGENTS.md snippet"
                  className="shrink-0"
                >
                  {copiedKey === "agents-md" ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      Copy
                    </>
                  )}
                </TonalButton>
              </div>

              <button
                type="button"
                onClick={() => setAgentsSnippetOpen((open) => !open)}
                aria-expanded={agentsSnippetOpen}
                className="inline-flex items-center gap-1.5 self-start text-[13.5px] font-medium text-[var(--t-blue)] hover:text-[var(--t-blue-hover)] cursor-pointer rounded-[6px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
              >
                <span>
                  {agentsSnippetOpen ? "Hide snippet" : "Inspect full snippet"}
                </span>
                <ChevronDown
                  className={`h-4 w-4 transition-transform duration-200 ${
                    agentsSnippetOpen ? "rotate-180" : ""
                  }`}
                />
              </button>

              {agentsSnippetOpen && (
                <div className="rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4 overflow-x-auto">
                  <pre className="font-mono text-[12px] leading-[1.6] text-[var(--t-ink)] whitespace-pre">
                    <code>{AGENTS_MD_SNIPPET}</code>
                  </pre>
                </div>
              )}
            </SurfaceTile>

            <SurfaceTile tone="neutral" bordered padding="md" className="flex flex-col gap-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="t-headline text-[19px] mb-1">
                    Structured <code className="font-mono text-[17px]">error.data</code> payload
                  </h3>
                  <p className="text-[13.5px] text-[var(--t-ink-secondary)]">
                    Example <code className="font-mono text-[12.5px]">-32002 OUTCOME UNKNOWN</code>{" "}
                    JSON payload returned to programmatic MCP clients.
                  </p>
                </div>
                <TonalButton
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={() => copyText("error-data", OUTCOME_UNKNOWN_EXAMPLE)}
                  aria-label="Copy OUTCOME UNKNOWN error.data JSON"
                  className="shrink-0"
                >
                  {copiedKey === "error-data" ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      Copy
                    </>
                  )}
                </TonalButton>
              </div>

              <button
                type="button"
                onClick={() => setErrorDataOpen((open) => !open)}
                aria-expanded={errorDataOpen}
                className="inline-flex items-center gap-1.5 self-start text-[13.5px] font-medium text-[var(--t-blue)] hover:text-[var(--t-blue-hover)] cursor-pointer rounded-[6px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
              >
                <span>
                  {errorDataOpen ? "Hide JSON example" : "Inspect JSON example"}
                </span>
                <ChevronDown
                  className={`h-4 w-4 transition-transform duration-200 ${
                    errorDataOpen ? "rotate-180" : ""
                  }`}
                />
              </button>

              {errorDataOpen && (
                <div className="rounded-[var(--t-radius-sm)] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4 overflow-x-auto">
                  <pre className="font-mono text-[12px] leading-[1.6] text-[var(--t-ink)] whitespace-pre">
                    <code>{OUTCOME_UNKNOWN_EXAMPLE}</code>
                  </pre>
                </div>
              )}
            </SurfaceTile>
          </div>
        </section>

        {/* Chaos Fault-Injection Suite */}
        <section id="chaos-evals" className="flex flex-col gap-6">
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4">
            <div className="flex flex-col gap-2 max-w-2xl">
              <TonalChip tone="mint" className="self-start">
                Chaos Fault-Injection Suite
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px]">
                Verified against real faults, not mocks of mocks
              </h2>
              <p className="t-body text-[15.5px]">
                <code className="font-mono text-[14px] text-[var(--t-ink)]">sidefx eval</code>{" "}
                spins up a local HTTP mock GitHub server, a bare git repo with
                fault-injecting <code className="font-mono text-[14px] text-[var(--t-ink)]">pre-receive</code>/
                <code className="font-mono text-[14px] text-[var(--t-ink)]">post-receive</code>{" "}
                hooks, a subprocess stdio MCP server with an{" "}
                <code className="font-mono text-[14px] text-[var(--t-ink)]">fsync</code>{" "}
                ledger, and 4 concurrent OS workers.
              </p>
            </div>

            <button
              type="button"
              onClick={() => copyText("eval-cmd", "uvx sidefx eval --verbose")}
              aria-label="Copy uvx sidefx eval --verbose"
              className="inline-flex items-center gap-2 self-start sm:self-auto rounded-full bg-[var(--t-surface)] hover:bg-[var(--t-surface-muted)] border border-[var(--t-border-subtle)] px-4 py-2 text-[12.5px] font-mono text-[var(--t-ink)] cursor-pointer transition-colors shrink-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <span>uvx sidefx eval --verbose</span>
              {copiedKey === "eval-cmd" ? (
                <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
              ) : (
                <Copy className="h-3.5 w-3.5 text-[var(--t-ink-muted)]" />
              )}
            </button>
          </div>

          <KpiGrid items={HEADLINE_KPIS} columns={5} />

          <SurfaceTile tone="white" bordered padding="none" className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-[var(--t-border-subtle)] bg-[var(--t-surface)]">
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Scenario
                    </th>
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      What It Injects
                    </th>
                    <th className="py-3.5 px-4 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Status
                    </th>
                    <th className="py-3.5 px-4 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Duplicates
                    </th>
                    <th className="py-3.5 px-4 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Remote Writes
                    </th>
                    <th className="py-3.5 px-5 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Receipt
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--t-border-subtle)]">
                  {CHAOS_SCENARIOS.map((row) => (
                    <tr
                      key={row.scenario}
                      className="align-top hover:bg-[var(--t-surface)]/50 transition-colors"
                    >
                      <td className="py-3.5 px-5 font-mono text-[12.5px] font-medium text-[var(--t-ink)] whitespace-nowrap">
                        {row.scenario}
                      </td>
                      <td className="py-3.5 px-5 text-[13.5px] leading-[1.5] text-[var(--t-ink-secondary)]">
                        {row.injects}
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        <TonalChip tone="mint" className="text-[11px] px-2.5 py-0.5">
                          {row.status}
                        </TonalChip>
                      </td>
                      <td className="py-3.5 px-4 text-right font-mono text-[13px] font-semibold text-[var(--t-success)] tabular-nums">
                        {row.duplicateWrites}
                      </td>
                      <td className="py-3.5 px-4 text-right font-mono text-[13px] text-[var(--t-ink)] tabular-nums">
                        {row.remoteWrites}
                      </td>
                      <td className="py-3.5 px-5 text-right font-mono text-[13px] font-medium text-[var(--t-ink)] tabular-nums">
                        {row.receiptAccuracy}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="border-t border-[var(--t-border-subtle)] bg-[var(--t-surface)] px-5 py-3 flex flex-wrap items-center justify-between gap-2 text-[12.5px] text-[var(--t-ink-secondary)]">
              <span>
                <strong className="font-medium text-[var(--t-ink)]">
                  Totals:
                </strong>{" "}
                6/6 passed · 0 duplicate remote writes · 100.0% receipt accuracy
                · 13.1s total runtime
              </span>
              <span className="font-mono text-[12px] text-[var(--t-ink-muted)]">
                uv run sidefx eval
              </span>
            </div>
          </SurfaceTile>
        </section>

        {/* Performance & Overhead */}
        <section id="benchmarks" className="flex flex-col gap-6">
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4">
            <div className="flex flex-col gap-2 max-w-2xl">
              <TonalChip tone="neutral" className="self-start">
                Performance & Overhead
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px]">
                Single-run latency with full durability enabled
              </h2>
              <p className="t-body text-[15px]">
                Representative single-run measurements from a git clone via{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  uv run python evals/benchmark.py
                </code>{" "}
                on macOS (<code className="font-mono text-[13.5px] text-[var(--t-ink)]">Darwin arm64</code>,
                Python 3.13.14, SQLite 3.53.1) with{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">journal_mode=WAL</code>,{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">synchronous=FULL</code>, and{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">fullfsync=ON</code>;
                exact timings vary by disk and OS.
              </p>
            </div>

            <button
              type="button"
              onClick={() =>
                copyText("bench-cmd", "uv run python evals/benchmark.py")
              }
              aria-label="Copy benchmark command"
              className="inline-flex items-center gap-2 self-start sm:self-auto rounded-full bg-[var(--t-surface)] hover:bg-[var(--t-surface-muted)] border border-[var(--t-border-subtle)] px-4 py-2 text-[12.5px] font-mono text-[var(--t-ink)] cursor-pointer transition-colors shrink-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-ring)]"
            >
              <span>uv run python evals/benchmark.py</span>
              {copiedKey === "bench-cmd" ? (
                <Check className="h-3.5 w-3.5 text-[var(--t-success)]" />
              ) : (
                <Copy className="h-3.5 w-3.5 text-[var(--t-ink-muted)]" />
              )}
            </button>
          </div>

          <SurfaceTile tone="white" bordered padding="none" className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[680px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-[var(--t-border-subtle)] bg-[var(--t-surface)]">
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Operation
                    </th>
                    <th className="py-3.5 px-4 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      N
                    </th>
                    <th className="py-3.5 px-4 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Median
                    </th>
                    <th className="py-3.5 px-4 text-right text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      p95
                    </th>
                    <th className="py-3.5 px-5 text-[12px] font-medium uppercase tracking-[0.04em] text-[var(--t-ink-muted)]">
                      Notes
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--t-border-subtle)]">
                  {BENCHMARK_ROWS.map((row) => (
                    <tr
                      key={row.operation}
                      className="hover:bg-[var(--t-surface)]/50 transition-colors"
                    >
                      <td className="py-3.5 px-5 text-[13.5px] font-medium text-[var(--t-ink)]">
                        {row.operation}
                      </td>
                      <td className="py-3.5 px-4 text-right font-mono text-[13px] text-[var(--t-ink-muted)] tabular-nums">
                        {row.n}
                      </td>
                      <td className="py-3.5 px-4 text-right font-mono text-[13px] font-semibold text-[var(--t-ink)] tabular-nums whitespace-nowrap">
                        {row.median}
                      </td>
                      <td className="py-3.5 px-4 text-right font-mono text-[13px] text-[var(--t-ink-secondary)] tabular-nums whitespace-nowrap">
                        {row.p95}
                      </td>
                      <td className="py-3.5 px-5 text-[13px] text-[var(--t-ink-secondary)]">
                        {row.notes}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SurfaceTile>
        </section>

        {/* FAQ */}
        <section id="faq" className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
          <div className="lg:col-span-4 flex flex-col gap-3">
            <TonalChip tone="blue" className="self-start">
              FAQ
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[34px]">
              Operational details
            </h2>
            <p className="t-body text-[14.5px]">
              How reconciliation, deterministic action IDs, timeout restarts,
              and secret redaction work in practice.
            </p>
            <div className="pt-1">
              <ChevronLink
                href={SITE_META.repoUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Read full README on GitHub
              </ChevronLink>
            </div>
          </div>

          <div className="lg:col-span-8">
            <TonalAccordion items={FAQ_ITEMS} defaultOpenIndex={0} />
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-[var(--t-border-subtle)] mt-auto">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 py-8 flex flex-col sm:flex-row items-center justify-between gap-4 text-[13px] text-[var(--t-ink-muted)]">
          <div className="flex items-center gap-2.5">
            <SidefxLogoMark className="h-5 w-5 shrink-0" />
            <span className="font-medium text-[var(--t-ink)]">sidefx</span>
            <span>·</span>
            <span>MIT License</span>
            <span>·</span>
            <span>{SITE_META.version}</span>
          </div>

          <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2">
            <a
              href={SITE_META.repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              GitHub
            </a>
            <a
              href={SITE_META.pypiUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              PyPI
            </a>
            <a
              href={SITE_META.harloUrl}
              className="hover:text-[var(--t-ink)] transition-colors"
            >
              harlo.dev
            </a>
            <span>
              Built with{" "}
              <a
                href={SITE_META.tonalUrl}
                className="text-[var(--t-blue)] hover:underline underline-offset-4"
              >
                Tonal
              </a>
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
