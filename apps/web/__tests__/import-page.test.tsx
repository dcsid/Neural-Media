import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ImportJob } from "@shared/types";

// Capture router.push so we can assert the post-import redirect.
const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

// motion/react: render children, drop the animation-only props (jsdom has no
// real animation loop and we only care about the content appearing).
vi.mock("motion/react", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  return {
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => children,
    motion: new Proxy({}, { get: () => Passthrough }),
  };
});

// Keep ApiError/serverBaseUrl real; stub the three endpoints the page calls.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      capabilities: vi.fn(),
      importStart: vi.fn(),
      importJob: vi.fn(),
    },
  };
});

import ImportPage from "@/app/import/page";
import { api } from "@/lib/api";

const mockApi = vi.mocked(api);

function job(over: Partial<ImportJob> = {}): ImportJob {
  return {
    id: "job-1",
    status: "running",
    mode: "mock",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
    completed_at: null,
    progress: { current: 0, total: 10, phase: "parsing" },
    error: null,
    source_filename: "watch_history.json",
    ...over,
  };
}

function dropFile(name: string, type: string) {
  const dropzone = screen.getByRole("region", { name: /import drop zone/i });
  const file = new File(["{}"], name, { type });
  fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });
}

beforeEach(() => {
  mockApi.capabilities.mockResolvedValue({
    mock: true,
    real: false,
    real_blockers: ["missing-gpu"],
  });
});

afterEach(() => cleanup());

describe("import flow (P2.7)", () => {
  it("drop → upload → poll → complete redirects to the dashboard", async () => {
    mockApi.importStart.mockResolvedValue(job({ status: "running" }));
    mockApi.importJob.mockResolvedValue(
      job({
        status: "complete",
        completed_at: "2026-01-01T00:00:09Z",
        progress: { current: 10, total: 10, phase: null },
      }),
    );

    render(<ImportPage />);
    dropFile("watch_history.json", "application/json");

    expect(await screen.findByText(/import complete/i)).toBeInTheDocument();
    expect(mockApi.importStart).toHaveBeenCalledOnce();
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"), {
      timeout: 2000,
    });
  });

  it("rejects an unsupported file type without uploading", async () => {
    render(<ImportPage />);
    dropFile("vacation.png", "image/png");

    expect(
      await screen.findByText(/\.json, \.txt, or \.zip/i),
    ).toBeInTheDocument();
    expect(mockApi.importStart).not.toHaveBeenCalled();
  });
});
