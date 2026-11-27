import { ImageResponse } from "next/og";

export const alt =
  "sidefx — Write-Ahead Outbound Effects Journal for AI Agents";
export const size = {
  width: 1200,
  height: 630,
};
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: "#FFFFFF",
          padding: "56px 64px",
          fontFamily: "sans-serif",
          color: "#202124",
        }}
      >
        {/* Top brand bar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
            <div
              style={{
                width: 44,
                height: 44,
                borderRadius: 13,
                background: "#131314",
                border: "1px solid rgba(255,255,255,0.1)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <svg width="30" height="30" viewBox="0 0 32 32" fill="none">
                <path
                  d="M6 9L12.5 16L6 23"
                  stroke="#E3E3E3"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <circle cx="22.5" cy="16" r="5.25" fill="#8AB4F8" />
              </svg>
            </div>
            <span
              style={{
                fontSize: 32,
                fontWeight: 600,
                letterSpacing: "-0.025em",
              }}
            >
              sidefx
            </span>
          </div>

          <div
            style={{
              background: "#E8F0FE",
              color: "#174EA6",
              fontSize: 19,
              fontWeight: 600,
              padding: "10px 22px",
              borderRadius: 9999,
            }}
          >
            v0.1.2 · Zero-LLM · Python 3.11+ stdlib
          </div>
        </div>

        {/* Center headline */}
        <div style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          <div
            style={{
              fontSize: 60,
              fontWeight: 600,
              lineHeight: 1.08,
              letterSpacing: "-0.035em",
              maxWidth: 980,
            }}
          >
            Write-ahead SQLite safety for AI agent side effects.
          </div>
          <div
            style={{
              fontSize: 24,
              color: "#5F6368",
              lineHeight: 1.45,
              maxWidth: 900,
            }}
          >
            Transparent stdio MCP proxy, CLI mutation wrapper, read-only crash
            reconciler, and ground-truth session receipts.
          </div>
        </div>

        {/* Bottom tonal metric tiles */}
        <div style={{ display: "flex", gap: "16px", alignItems: "stretch" }}>
          {[
            { value: "6/6", label: "Chaos evals passed", bg: "#E6F4EA", accent: "#137333" },
            { value: "0", label: "Duplicate writes", bg: "#E8F0FE", accent: "#174EA6" },
            { value: "100.0%", label: "Receipt accuracy", bg: "#F0F4F9", accent: "#202124" },
            { value: "~+0.02 ms", label: "MCP read overhead", bg: "#F4F2EE", accent: "#3C4043" },
          ].map((tile) => (
            <div
              key={tile.label}
              style={{
                display: "flex",
                flexDirection: "column",
                flex: 1,
                background: tile.bg,
                borderRadius: 20,
                padding: "18px 22px",
                gap: "4px",
              }}
            >
              <div
                style={{
                  fontSize: 28,
                  fontWeight: 700,
                  letterSpacing: "-0.02em",
                  color: tile.accent,
                }}
              >
                {tile.value}
              </div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 500,
                  color: "#3C4043",
                }}
              >
                {tile.label}
              </div>
            </div>
          ))}
        </div>
      </div>
    ),
    { ...size }
  );
}
