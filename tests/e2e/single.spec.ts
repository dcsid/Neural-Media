// E2E for the single-clip → brain journey at `/predict` (upload → segment → result).
//
// All network calls hit the mock server on :3001. Upload a tiny MP4, the
// browser reads its duration and the segment picker defaults to a valid window,
// then Predict. Selectors rely on visible copy + roles (not test-ids) so the
// suite survives reasonable markup changes.
//
// Status timing in the mock (must stay in sync with mock-server.ts):
//   confirm → pending → downloading (1s) → inferring (3s) → done (6s)

import { test, expect, type Page, type Request } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TINY_MP4 = resolve(__dirname, "fixtures/tiny.mp4");

async function uploadClip(page: Page) {
  // The dropzone wraps a hidden <input type="file"> — setInputFiles works on
  // hidden inputs without a prior click. The browser then reads the clip's
  // duration and the segment picker appears with Predict enabled on the default
  // window.
  await page.locator('input[type="file"]').first().setInputFiles(TINY_MP4);
  await expect(
    page.getByRole("button", { name: /^predict$/i }),
  ).toBeEnabled({ timeout: 10_000 });
}

async function clickPredict(page: Page) {
  await page.getByRole("button", { name: /^predict$/i }).click();
}

test.describe("/predict — upload + segment → brain", () => {
  test("happy path: upload → result", async ({ page }) => {
    await page.goto("/predict");
    await uploadClip(page);
    await clickPredict(page);

    // Done at +6s in the mock — the result header + "Try another" CTA are our
    // canonical "we reached done" signals; the brain renders into a <canvas>.
    await expect(
      page.getByRole("heading", { name: /your brain on that video/i }),
    ).toBeVisible({ timeout: 14_000 });
    await expect(
      page.getByRole("button", { name: /try another/i }),
    ).toBeVisible();
    await expect(page.locator("canvas")).toBeVisible();

    // Reset path → back to the upload dropzone.
    await page.getByRole("button", { name: /try another/i }).click();
    await expect(page.getByText(/drop mp4 here/i)).toBeVisible();
  });

  test("polling stops on unmount", async ({ page }) => {
    const apiRequests: Request[] = [];
    page.on("request", (r) => {
      if (/\/v2\/jobs/.test(r.url())) apiRequests.push(r);
    });

    await page.goto("/predict");
    await uploadClip(page);
    await clickPredict(page);

    // Wait until requests are flowing (upload + confirm + the status poll), so
    // we know the tracking loop is live.
    await expect
      .poll(() => apiRequests.length, { timeout: 8_000 })
      .toBeGreaterThan(0);

    // Navigate away mid-flight (about:blank avoids re-triggering app fetches).
    await page.goto("about:blank");

    // Snapshot the count, then verify polls don't keep growing for 5s.
    const baseline = apiRequests.length;
    await page.waitForTimeout(5_000);
    expect(apiRequests.length).toBe(baseline);
  });
});
