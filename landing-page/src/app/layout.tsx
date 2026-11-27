import type { Metadata } from "next";
import "./globals.css";

const SITE_URL = "https://sidefx.harlo.dev";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "sidefx — Write-Ahead Outbound Effects Journal for AI Agents",
    template: "%s — sidefx",
  },
  description:
    "Durable SQLite WAL write-ahead journal, transparent stdio MCP proxy, CLI mutation wrapper, read-only crash reconciler, and ground-truth session receipts for AI agents. Pure Python 3.11+ stdlib, zero LLM calls.",
  alternates: {
    canonical: "/",
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    siteName: "sidefx",
    title: "sidefx — Write-Ahead Outbound Effects Journal for AI Agents",
    description:
      "Stop duplicate external writes after timeouts and crashes. Pure Python 3.11+ stdlib SQLite WAL journal, stdio MCP proxy, CLI wrapper, crash reconciler, and ground-truth receipts.",
  },
  twitter: {
    card: "summary_large_image",
    title: "sidefx — Write-Ahead Outbound Effects Journal for AI Agents",
    description:
      "Durable SQLite WAL journal, stdio MCP proxy, CLI mutation wrapper, crash reconciler, and ground-truth session receipts for AI agents.",
  },
};

const THEME_INIT_SCRIPT = `(function(){try{var d=document.documentElement;var p=new URLSearchParams(window.location.search);var qT=p.get('theme');var t=(qT==='dark'||qT==='light')?qT:localStorage.getItem('sidefx-theme');if(t!=='dark'&&t!=='light'){t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}d.dataset.tonalTheme=t;d.dataset.tonalMode='pill';}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Google+Sans:opsz,wght@17..18,400;17..18,500;17..18,700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
