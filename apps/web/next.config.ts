import type { NextConfig } from "next";

const API_BASE =
  process.env.NEURAL_MEDIA_API_BASE ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // Proxy /api/v1/* to the FastAPI service so the frontend can use relative
  // URLs in production builds and dev alike. Keeps everything single-origin.
  async rewrites() {
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
