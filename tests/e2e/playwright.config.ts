import { defineConfig, devices } from "@playwright/test";

const WEB_PORT = 3000;
const MOCK_PORT = 3001;

// We deliberately do NOT depend on the user's real .env. The mock server on
// :3001 owns the entire /v2/jobs* surface; the web app is launched with
// NEXT_PUBLIC_API_BASE_URL pointed at it.
export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false, // sequential — the polling-cancel test inspects global request traffic
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    actionTimeout: 10_000,
    // 30s rather than 15s — Next dev's first compile of /single can land
    // close to 13s on a cold cache (BrainMesh + GLB + r3f) and a tight
    // budget here flakes on slower machines. Tests still finish well
    // under the per-test timeout above.
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // Mock first so the web app sees it ready.
      command: "node mock-server.ts",
      url: `http://localhost:${MOCK_PORT}/__health`,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 30_000,
    },
    {
      command: "pnpm --filter @neural-media/web dev",
      // Gate readiness on /single — the page this suite actually exercises.
      // The dashboard at `/` is a `force-dynamic` server component that
      // awaits the v1 FastAPI (:8000) during SSR; the e2e harness only boots
      // the mock (:3001), so probing `/` never returned healthy and the job
      // sat until the start budget elapsed. /single is a client component
      // ("use client") and returns its 200 shell with no backend.
      url: `http://localhost:${WEB_PORT}/single`,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 180_000,
      env: {
        // T4's api-v2 lib reads NEXT_PUBLIC_API_BASE_V2 at build time
        // (Next inlines NEXT_PUBLIC_* into the client bundle). It also
        // defaults to http://localhost:3001 when unset, but we set it
        // explicitly so the suite isn't quietly dependent on that default.
        NEXT_PUBLIC_API_BASE_V2: `http://localhost:${MOCK_PORT}`,
      },
    },
  ],
});
