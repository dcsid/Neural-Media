// E2E for the single-clip → brain journey at `/` (YouTube URL + segment).
//
// All network calls hit the mock server on :3001. The segment picker defaults
// to a valid 0–30s window, so the happy path is just: fill a YouTube URL →
// Predict. Selectors rely on visible copy + roles (not test-ids) so the suite
// survives reasonable markup changes.
//
// Status timing in the mock (must stay in sync with mock-server.ts):
//   pending → downloading (1s) → inferring (3s) → done (6s)
//   BLOCK_ME → failed_download / download_blocked at 2s

import { test, expect, type Page, type Request } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TINY_MP4 = resolve(__dirname, "fixtures/tiny.mp4");

const SAMPLE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
// A valid YouTube URL whose id carries the mock's BLOCK_ME marker, so it
// passes the client's YouTube validator but the mock fails the download.
const BLOCKED_URL = "https://www.youtube.com/watch?v=BLOCK_ME_9999";

async function fillUrl(page: Page, url: string) {
  await page.getByLabel(/youtube url/i).fill(url);
}

async function clickPredict(page: Page) {
  await page.getByRole("button", { name: /^predict$/i }).click();
}

test.describe("/ — YouTube URL + segment → brain", () => {
  test("happy path: URL → result", async ({ page }) => {
    await page.goto("/");
    await fillUrl(page, SAMPLE_URL);
    // The default 0–30s segment is valid, so Predict is enabled.
    await clickPredict(page);

    // Done at +6s in the mock — the Result panel's "Try another" CTA is our
    // canonical "we reached done" signal.
    await expect(
      page.getByRole("button", { name: /try another/i }),
    ).toBeVisible({ timeout: 12_000 });
    await expect(page.getByText(/of brain activity/i)).toBeVisible();
    await expect(page.locator("canvas")).toBeVisible();

    // Reset path.
    await page.getByRole("button", { name: /try another/i }).click();
    await expect(
      page.getByRole("button", { name: /^predict$/i }),
    ).toBeVisible();
  });

  test("download blocked → upload fallback", async ({ page }) => {
    await page.goto("/");
    await fillUrl(page, BLOCKED_URL);
    await clickPredict(page);

    // failed_download (download_blocked) arrives ~2s; the error panel surfaces
    // an upload drop zone.
    await expect(
      page.getByText(/youtube blocked our download/i),
    ).toBeVisible({ timeout: 8_000 });

    // The dropzone has a hidden <input type="file"> — setInputFiles works on
    // hidden inputs without a prior click.
    await page.locator('input[type="file"]').first().setInputFiles(TINY_MP4);

    // After confirm, happy-path timing resumes → result panel at +6s.
    await expect(
      page.getByRole("button", { name: /try another/i }),
    ).toBeVisible({ timeout: 14_000 });
    await expect(page.locator("canvas")).toBeVisible();
  });

  test("polling stops on unmount", async ({ page }) => {
    const apiRequests: Request[] = [];
    page.on("request", (r) => {
      if (/\/v2\/jobs/.test(r.url())) apiRequests.push(r);
    });

    await page.goto("/");
    await fillUrl(page, SAMPLE_URL);
    await clickPredict(page);

    // Wait until at least one poll has fired so we know polling is live.
    await expect
      .poll(() => apiRequests.length, { timeout: 5_000 })
      .toBeGreaterThan(0);

    // Navigate away mid-flight (about:blank avoids re-triggering app fetches).
    await page.goto("about:blank");

    // Snapshot the count, then verify it doesn't grow for 5s.
    const baseline = apiRequests.length;
    await page.waitForTimeout(5_000);
    expect(apiRequests.length).toBe(baseline);
  });
});
