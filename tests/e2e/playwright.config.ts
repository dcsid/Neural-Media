import { defineConfig, devices } from "@playwright/test";

const WEB_PORT = 3000;
const MOCK_PORT = 3001;

// We deliberately do NOT depend on the user's real .env. The mock server on
// :3001 owns the entire /v2/jobs* surface; the web app is launched with
// NEXT_PUBLIC_API_BASE_V2 pointed at it.
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
      // Run the e2e suite against a PRODUCTION build, not `next dev`. The
      // /single route's dev-mode first compile is enormous (r3f + three + the
      // fsaverage5 GLB — measured ~75s cold / ~10s warm locally), which blew
      // past every per-navigation timeout and hung the job. A production build
      // pre-compiles every route so serving is sub-second — and it's what
      // actually ships. `next build` is already proven green by the `frontend`
      // CI job.
      command:
        "pnpm --filter @neural-media/web build && pnpm --filter @neural-media/web start",
      // TCP port check: `next start` binds :3000 as soon as it's up (after the
      // build), with no HTTP-status dependency on a backend the harness omits.
      port: WEB_PORT,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      // Generous budget: covers `next build` (~1-2 min in CI) + `next start`.
      timeout: 240_000,
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
