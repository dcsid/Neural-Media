import type { NextConfig } from "next";

// The v1 dashboard (mounted at /, /import, /v/[id], etc.) talks to a
// FastAPI backend via relative /api/v1/* URLs. We rewrite those to a
// configurable base so the same client bundle works in dev and prod.
//
// The rewrite is registered ONLY when NEURAL_MEDIA_API_BASE is set. In
// environments without a v1 backend running — Playwright e2e (which
// only exercises /single and the demo gallery), CI smoke checks, the
// Tier-3 static-gallery deploy — leaving it unset silences the Next dev
// proxy's "Failed to proxy http://127.0.0.1:8000/api/v1/... ECONNREFUSED"
// noise that otherwise floods the reporter every time any v1 route
// briefly mounts. If you're working on the v1 dashboard, export
// NEURAL_MEDIA_API_BASE=http://127.0.0.1:8000 (or run via make dev-api,
// which sets it implicitly through the runbook).
const API_BASE = process.env.NEURAL_MEDIA_API_BASE;

const config: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    if (!API_BASE) return [];
    return [
      {
        source: "/api/v1/:path*",
        destination: `${API_BASE}/api/v1/:path*`,
      },
    ];
  },
  // Tighten security headers — local-first app, no third-party scripts.
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
    ];
  },
};

export default config;
