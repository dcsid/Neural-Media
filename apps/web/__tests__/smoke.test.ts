import { describe, expect, it } from "vitest";

// Smoke test: proves the Vitest harness, jsdom environment, and the
// `@/` + `@shared/` path aliases all resolve before the real suites lean
// on them.
import { looksLikeYouTubeUrl } from "@/lib/api-v2";
import { REGION_IDS } from "@shared/types";

describe("vitest harness", () => {
  it("runs in a jsdom environment", () => {
    expect(typeof window).toBe("object");
    expect(typeof document).toBe("object");
  });

  it("resolves the @/ alias", () => {
    expect(
      looksLikeYouTubeUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ).toBe(true);
  });

  it("resolves the @shared/ alias", () => {
    expect(REGION_IDS.length).toBeGreaterThan(0);
  });
});
