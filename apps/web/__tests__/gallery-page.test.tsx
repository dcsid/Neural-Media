import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

// The brain mesh is irrelevant here — stub next/dynamic so R3F never loads.
vi.mock("next/dynamic", () => ({ default: () => () => null }));

import DemoGalleryPage from "@/app/single/gallery/page";

afterEach(() => cleanup());

function stubManifestFetch(manifest: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>().mockResolvedValue({
      ok: true,
      json: async () => manifest,
    } as Response),
  );
}

describe("demo gallery (P2.5)", () => {
  it("renders the demo banner and labels cards via the tribe-v2-mock prefix", async () => {
    stubManifestFetch({
      entries: [
        {
          slug: "m",
          label: "Mock gallery clip",
          url: "u",
          durationSec: 10,
          modelVersion: "tribe-v2-mock-gallery",
        },
        {
          slug: "r",
          label: "Real model clip",
          url: "u",
          durationSec: 12,
          modelVersion: "tribe-v2",
        },
        {
          // Contains "mock" but is NOT a tribe-v2-mock build: the old
          // .includes("mock") check mislabeled this as mock; the contract
          // prefix check correctly treats it as real.
          slug: "x",
          label: "Foreign mock clip",
          url: "u",
          durationSec: 8,
          modelVersion: "experimental-mock",
        },
      ],
    });

    render(<DemoGalleryPage />);

    expect(
      await screen.findByText(/precomputed and ship with the build/i),
    ).toBeInTheDocument();

    const mockCard = (await screen.findByText("Mock gallery clip")).closest(
      "button",
    )!;
    expect(within(mockCard).getByText(/mock prediction/i)).toBeInTheDocument();

    const realCard = screen.getByText("Real model clip").closest("button")!;
    expect(within(realCard).getByText(/real prediction/i)).toBeInTheDocument();

    const foreignCard = screen.getByText("Foreign mock clip").closest("button")!;
    expect(
      within(foreignCard).getByText(/real prediction/i),
    ).toBeInTheDocument();
  });
});
