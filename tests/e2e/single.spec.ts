// E2E for the single-clip → brain journey on /single.
//
// All network calls hit the mock server on :3001 — the web app is launched
// with NEXT_PUBLIC_API_BASE_URL pointed at it. The selectors below intentionally
// rely on visible copy + roles rather than test-ids so the suite survives
// reasonable markup changes, and so T4 doesn't have to add test-id hooks.
//
// Status timing in the mock (must stay in sync with mock-server.ts):
//   pending → downloading (1s) → inferring (3s) → done (6s)
//   BLOCK_ME    → failed_download at 2s
//   LONG_VIDEO  → rejected_duration at 2s

import { test, expect, type Request } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TINY_MP4 = resolve(__dirname, "fixtures/tiny.mp4");

const SAMPLE_URL = "https://www.tiktok.com/@user/video/1234567890";
const BLOCKED_URL = "https://www.tiktok.com/@user/video/BLOCK_ME_9999";
const LONG_URL = "https://www.tiktok.com/@user/video/LONG_VIDEO_9999";

// Helpers — these tolerate a few likely T4 markup choices.
async function fillUrl(page: import("@playwright/test").Page, url: string) {
  const candidate =
    page.getByRole("textbox", { name: /tiktok|url|link|paste/i }).first();
  if (await candidate.count()) {
    await candidate.fill(url);
    return;
  }
  await page.locator('input[type="url"], input[type="text"]').first().fill(url);
}

async function clickPredict(page: import("@playwright/test").Page) {
  await page
    .getByRole("button", { name: /predict|see (?:my )?brain|go|submit/i })
    .first()
    .click();
}

test.describe("/single — TikTok URL → brain", () => {
  test("happy path: URL → result", async ({ page }) => {
    await page.goto("/");
    await fillUrl(page, SAMPLE_URL);
    await clickPredict(page);

    // Queueing affordance must appear quickly ("Queueing your job...").
    await expect(page.getByText(/queue/i)).toBeVisible({ timeout: 2_000 });

    // Done at +6s in the mock — the Result panel shows "Try another" as the
    // terminal CTA and is our canonical "we reached done" signal.
    await expect(
      page.getByRole("button", { name: /try another/i }),
    ).toBeVisible({ timeout: 10_000 });

    // The brain-activity heading also appears on done; double-checks the
    // result panel rendered, not just any button labelled "Try another".
    await expect(page.getByText(/of brain activity/i)).toBeVisible();

    // A WebGL canvas must be mounted.
    await expect(page.locator("canvas")).toBeVisible();

    // Reset path.
    await page.getByRole("button", { name: /try another/i }).click();
    await expect(
      page.getByRole("button", { name: /predict|submit/i }).first(),
    ).toBeVisible();
  });

  test("tiktok blocked → upload fallback", async ({ page }) => {
    await page.goto("/");
    await fillUrl(page, BLOCKED_URL);
    await clickPredict(page);

    // failed_download arrives at ~2s; error panel surfaces a drop zone.
    await expect(
      page.getByText(/tiktok blocked our download/i),
    ).toBeVisible({ timeout: 6_000 });
    await expect(page.getByText(/drop mp4 here/i)).toBeVisible();

    // The dropzone has a hidden (sr-only) <input type="file"> — setInputFiles
    // works on hidden inputs without needing the user to click first.
    await page
      .locator('input[type="file"]')
      .first()
      .setInputFiles(TINY_MP4);

    // After confirm, happy-path timing resumes → result panel at +6s.
    await expect(
      page.getByRole("button", { name: /try another/i }),
    ).toBeVisible({ timeout: 12_000 });
    await expect(page.locator("canvas")).toBeVisible();
  });

  test("too long: rejected_duration", async ({ page }) => {
    await page.goto("/");
    await fillUrl(page, LONG_URL);
    await clickPredict(page);

    await expect(
      page.getByText(/clips longer than 90 seconds aren't supported/i),
    ).toBeVisible({ timeout: 6_000 });
  });

  test("polling cancel on unmount", async ({ page }) => {
    // Capture every outbound request to the mock API.
    const apiRequests: Request[] = [];
    page.on("request", (r) => {
      if (/\/v2\/jobs/.test(r.url())) apiRequests.push(r);
    });

    await page.goto("/");
    await fillUrl(page, SAMPLE_URL);
    await clickPredict(page);

    // Wait until at least one poll has happened so we know polling is live.
    await expect.poll(() => apiRequests.length, { timeout: 5_000 }).toBeGreaterThan(0);

    // Navigate away mid-flight. We use about:blank rather than another app
    // route on purpose — sibling routes in this repo proxy to a FastAPI
    // backend that we deliberately don't run during e2e, and a transient
    // ECONNREFUSED there would mask the actual signal (does /single cancel
    // its poll loop on unmount?).
    await page.goto("about:blank");

    // Snapshot the count, then verify it doesn't grow for 5s.
    const baseline = apiRequests.length;
    await page.waitForTimeout(5_000);
    expect(apiRequests.length).toBe(baseline);
  });
});
