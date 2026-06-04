import type { NextConfig } from "next";

// Static export for S3 + CloudFront hosting (the deploy target — see
// docs/single-video-deploy.md). Gated behind STATIC_EXPORT so the DEFAULT
// build still runs as a Next server: `next start` (the Playwright e2e harness),
// local preview, and the `frontend` CI build all exercise the same client
// bundle without emitting ./out/. The deploy build sets STATIC_EXPORT=1.
const isExport = process.env.STATIC_EXPORT === "1";

const config: NextConfig = {
  reactStrictMode: true,
  ...(isExport ? { output: "export" } : {}),
};

// Security headers apply only when Next actually serves the app (dev /
// `next start`). They are NOT emitted in static-export mode — Next disallows
// headers() with output:"export" — so for the S3/CloudFront deploy they move
// to a CloudFront response-headers policy (see docs/single-video-deploy.md).
if (!isExport) {
  config.headers = async () => [
    {
      source: "/(.*)",
      headers: [
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "no-referrer" },
        { key: "X-Content-Type-Options", value: "nosniff" },
      ],
    },
  ];
}

export default config;
