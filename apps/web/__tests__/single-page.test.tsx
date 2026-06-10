import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { REGION_IDS } from "@shared/types";

// The product is upload-only: drop an MP4, pick a ≤90s window, Predict. These
// tests drive that state machine (idle → tracking → result | error).

// BrainMesh is R3F/three — irrelevant here. Stub next/dynamic so its lazy
// import never runs under jsdom.
vi.mock("next/dynamic", () => ({ default: () => () => null }));

// The duration probe drives a real <video> + loadedmetadata, which jsdom can't
// run — it lives in its own module precisely so we can mock it here.
vi.mock("@/lib/video", () => ({ readVideoDuration: vi.fn() }));

// The synced result viewer (video + WebGL brain) is covered separately; stub it
// so a reached "result" phase is observable without a jsdom media pipeline.
vi.mock("@/components/brain/LiveResultViewer", () => ({
  LiveResultViewer: ({ jobId }: { jobId: string }) =>
    createElement("div", { "data-testid": "result-viewer" }, `result ${jobId}`),
}));

// Keep the real validators / error class / status helpers; stub only the
// network functions so we can drive the upload chain deterministically.
vi.mock("@/lib/api-v2", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-v2")>();
  return {
    ...actual,
    createUploadJob: vi.fn(),
    putUpload: vi.fn(),
    confirmUpload: vi.fn(),
    getJob: vi.fn(),
    fetchActivation: vi.fn(),
  };
});

import SingleVideoPage from "@/app/page";
import { SessionProvider } from "@/lib/session";
import * as apiV2 from "@/lib/api-v2";
import { readVideoDuration } from "@/lib/video";

const api = vi.mocked(apiV2);
const mockDuration = vi.mocked(readVideoDuration);

function activation(): apiV2.ActivationPayload {
  const byRegion = Object.fromEntries(
    REGION_IDS.map((r) => [r, [0.1, 0.2, 0.3]]),
  ) as apiV2.ActivationPayload["byRegion"];
  return {
    videoDurationSec: 10,
    timestamps: [0, 0.5, 1],
    byRegion,
    modelVersion: "tribe-v2-mock",
  };
}

function mp4(name = "clip.mp4") {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "video/mp4" });
}

const fileInput = () => screen.getByLabelText(/upload an mp4 video file/i);
const predictBtn = () => screen.getByRole("button", { name: /^predict$/i });

// The page reads its phase from the layout-level session store, so every render
// must be wrapped in the provider (as it is in app/layout.tsx).
const renderPage = () => render(<SingleVideoPage />, { wrapper: SessionProvider });

// Drop a file + wait for the duration probe → the segment picker to appear.
async function pickClip(user: ReturnType<typeof userEvent.setup>, file = mp4()) {
  await user.upload(fileInput(), file);
  await screen.findByLabelText(/start \(s\)/i);
}

beforeEach(() => {
  // Default: a readable 60s clip + a happy upload chain. Tests override
  // getJob / fetchActivation to steer the outcome.
  mockDuration.mockResolvedValue(60);
  api.createUploadJob.mockResolvedValue({
    jobId: "job-1",
    uploadUrl: "https://s3/put",
    uploadKey: "k",
  });
  api.putUpload.mockResolvedValue(undefined);
  api.confirmUpload.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("/ upload → segment → result", () => {
  it("uploads, threads the chosen window into confirm, reaches the result", async () => {
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      resultUrl: "https://cdn/job-1.json",
      createdAt: 0,
      elapsedSec: 1,
    });
    api.fetchActivation.mockResolvedValue(activation());

    const user = userEvent.setup();
    renderPage();

    await pickClip(user); // 60s clip → default 0–30 window
    await user.click(predictBtn());

    // Reached the result: the header acknowledges it + the viewer mounts.
    expect(
      await screen.findByRole("heading", { name: /your brain on that video/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("result-viewer")).toBeInTheDocument();

    expect(api.createUploadJob).toHaveBeenCalledWith("clip.mp4", "video/mp4");
    expect(api.putUpload).toHaveBeenCalled();
    expect(api.confirmUpload).toHaveBeenCalledWith("job-1", {
      startSec: 0,
      endSec: 30,
    });
    expect(api.fetchActivation).toHaveBeenCalledWith(
      "https://cdn/job-1.json",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("threads a custom [startSec, endSec) into confirmUpload", async () => {
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });

    const user = userEvent.setup();
    renderPage();
    await pickClip(user);

    const start = screen.getByLabelText(/start \(s\)/i);
    const end = screen.getByLabelText(/end \(s\)/i);
    await user.clear(start);
    await user.type(start, "12");
    await user.clear(end);
    await user.type(end, "50");
    await user.click(predictBtn());

    await vi.waitFor(() =>
      expect(api.confirmUpload).toHaveBeenCalledWith("job-1", {
        startSec: 12,
        endSec: 50,
      }),
    );
  });
});

describe("/ segment picker", () => {
  it("gates Predict on a valid window (≤90s, within the clip)", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickClip(user); // 60s clip
    const end = screen.getByLabelText(/end \(s\)/i);

    expect(predictBtn()).toBeEnabled(); // default 0–30 valid

    await user.clear(end);
    await user.type(end, "0"); // start 0 >= end 0 → bad_segment
    expect(predictBtn()).toBeDisabled();
    expect(screen.getByText(/start before the end/i)).toBeInTheDocument();

    await user.clear(end);
    await user.type(end, "200"); // > 90s cap → segment_too_long
    expect(predictBtn()).toBeDisabled();
    expect(screen.getByText(/90 seconds or less/i)).toBeInTheDocument();

    await user.clear(end);
    await user.type(end, "75"); // past the 60s clip → out of bounds
    expect(predictBtn()).toBeDisabled();
    expect(screen.getByText(/past the end of your clip/i)).toBeInTheDocument();

    await user.clear(end);
    await user.type(end, "45"); // 0–45, valid (≤90 and ≤60)
    expect(predictBtn()).toBeEnabled();
  });

  it("shows a friendly error when the file can't be decoded", async () => {
    mockDuration.mockRejectedValue(new Error("decode error"));
    const user = userEvent.setup();
    renderPage();
    await user.upload(fileInput(), mp4());

    expect(
      await screen.findByText(/couldn't read that video/i),
    ).toBeInTheDocument();
    // Back to the dropzone — no segment picker for an unreadable file.
    expect(screen.queryByLabelText(/start \(s\)/i)).not.toBeInTheDocument();
  });

  it("the file input is a labelled file picker (a11y)", () => {
    renderPage();
    expect(fileInput()).toHaveAttribute("type", "file");
  });
});

describe("/ error resilience (calm, never raw)", () => {
  async function uploadPredict(user: ReturnType<typeof userEvent.setup>) {
    await pickClip(user);
    await user.click(predictBtn());
  }

  it("inference failure → calm message, no stack trace", async () => {
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_inference",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    renderPage();
    await uploadPredict(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("a Space cold-boot timeout reads as 'waking up', not a file problem", async () => {
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "hf_space_unreachable: read timed out",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    renderPage();
    await uploadPredict(user);

    expect(
      await screen.findByText(/the model was waking up/i),
    ).toBeInTheDocument();
    // Never leaks the raw error string.
    expect(screen.queryByText(/hf_space_unreachable/)).not.toBeInTheDocument();
  });

  it("done with no resultUrl → error panel", async () => {
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    renderPage();
    await uploadPredict(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("done with a malformed result → error panel, no crash", async () => {
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      resultUrl: "https://cdn/x.json",
      createdAt: 0,
      elapsedSec: 1,
    });
    api.fetchActivation.mockRejectedValue(
      new apiV2.ApiV2Error({
        kind: "validation",
        url: "https://cdn/x.json",
        message: "byRegion missing",
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await uploadPredict(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("a failed upload surfaces inline and retries cleanly", async () => {
    api.createUploadJob
      .mockRejectedValueOnce(
        new apiV2.ApiV2Error({
          kind: "offline",
          url: "/v2/jobs/upload",
          message: "down",
        }),
      )
      .mockResolvedValue({ jobId: "job-2", uploadUrl: "https://s3/put", uploadKey: "k" });
    api.getJob.mockResolvedValue({
      jobId: "job-2",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });

    const user = userEvent.setup();
    renderPage();
    await pickClip(user);

    await user.click(predictBtn());
    expect(await screen.findByText(/upload failed/i)).toBeInTheDocument();

    // The file's still picked — just hit Predict again.
    await user.click(predictBtn());
    expect(await screen.findByText(/running on the gpu/i)).toBeInTheDocument();
  });

  it("does not double-submit when Predict is mashed", async () => {
    let resolveCreate: (v: apiV2.CreateUploadJobResponse) => void = () => {};
    api.createUploadJob.mockImplementation(
      () =>
        new Promise<apiV2.CreateUploadJobResponse>((r) => {
          resolveCreate = r;
        }),
    );
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    renderPage();
    await pickClip(user);

    const btn = predictBtn();
    await user.click(btn);
    // Now "Submitting…" + disabled — a second mash is a guarded no-op.
    await user.click(btn).catch(() => {});
    resolveCreate({ jobId: "job-1", uploadUrl: "https://s3/put", uploadKey: "k" });

    await vi.waitFor(() =>
      expect(api.createUploadJob).toHaveBeenCalledTimes(1),
    );
  });

  it("aborts the in-flight poll when unmounted", async () => {
    let captured: AbortSignal | undefined;
    api.getJob.mockImplementation((_id, opts) => {
      captured = opts?.signal;
      return new Promise(() => {}); // never resolves — stays in flight
    });
    const user = userEvent.setup();
    const { unmount } = renderPage();
    await pickClip(user);
    await user.click(predictBtn());

    await vi.waitFor(() => expect(api.getJob).toHaveBeenCalled());
    expect(captured?.aborted).toBe(false);
    unmount();
    expect(captured?.aborted).toBe(true);
  });
});

describe("/ timeout + poll resilience (fake timers)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // fireEvent (synchronous) rather than userEvent — userEvent's internal delays
  // deadlock against fake timers. Set the file via defineProperty since jsdom's
  // input.files is read-only.
  async function reachTracking() {
    renderPage();
    const input = fileInput();
    Object.defineProperty(input, "files", { value: [mp4()], configurable: true });
    fireEvent.change(input);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20); // flush duration probe → picker
    });
    fireEvent.click(predictBtn());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50); // flush upload chain → first poll
    });
  }

  it("flips to the timeout error after the 10-minute budget", async () => {
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 5,
    });
    await reachTracking();
    expect(screen.getByText(/running on the gpu/i)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(601_000); // past the 600s cap
    });
    expect(
      screen.getByText(/taking longer than expected/i),
    ).toBeInTheDocument();
  });

  it("recovers from a transient poll failure, then resolves", async () => {
    api.getJob
      .mockRejectedValueOnce(
        new apiV2.ApiV2Error({ kind: "offline", url: "/v2/jobs/job-1", message: "blip" }),
      )
      .mockResolvedValue({
        jobId: "job-1",
        status: "done",
        resultUrl: "https://cdn/x.json",
        createdAt: 0,
        elapsedSec: 1,
      });
    api.fetchActivation.mockResolvedValue(activation());

    await reachTracking();
    expect(screen.queryByText(/lost the connection/i)).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2600); // next tick polls, succeeds
    });
    expect(
      screen.getByRole("heading", { name: /your brain on that video/i }),
    ).toBeInTheDocument();
  });

  it("surfaces a connection error only after a sustained streak", async () => {
    api.getJob.mockRejectedValue(
      new apiV2.ApiV2Error({ kind: "http", url: "/v2/jobs/job-1", status: 503, message: "down" }),
    );
    await reachTracking();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(12_000); // build past the failure threshold
    });
    expect(screen.getByText(/lost the connection/i)).toBeInTheDocument();
  });
});
