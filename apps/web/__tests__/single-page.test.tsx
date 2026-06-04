import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { REGION_IDS } from "@shared/types";

// The brain mesh is an R3F/three.js component irrelevant to the page's state
// machine — stub next/dynamic so its lazy import never runs under jsdom.
vi.mock("next/dynamic", () => ({ default: () => () => null }));

// Keep the real URL validator / error class / status helpers; stub only the
// network functions so we can drive the state machine deterministically.
vi.mock("@/lib/api-v2", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-v2")>();
  return {
    ...actual,
    createUrlJob: vi.fn(),
    createUploadJob: vi.fn(),
    putUpload: vi.fn(),
    confirmUpload: vi.fn(),
    getJob: vi.fn(),
    fetchActivation: vi.fn(),
  };
});

import SingleVideoPage from "@/app/page";
import * as apiV2 from "@/lib/api-v2";

const api = vi.mocked(apiV2);

const URL_A = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";

function activation(): apiV2.ActivationPayload {
  const timestamps = [0, 0.5, 1];
  const byRegion = Object.fromEntries(
    REGION_IDS.map((r) => [r, [0.1, 0.2, 0.3]]),
  ) as apiV2.ActivationPayload["byRegion"];
  return { videoDurationSec: 1.5, timestamps, byRegion, modelVersion: "tribe-v2-mock" };
}

afterEach(() => cleanup());

describe("/single state machine", () => {
  it("URL submit → tracking → done → result, and the header acknowledges the result (P3.9)", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      resultUrl: "https://cdn/job-1.json",
      createdAt: 0,
      elapsedSec: 1,
    });
    api.fetchActivation.mockResolvedValue(activation());

    const user = userEvent.setup();
    render(<SingleVideoPage />);

    await user.type(screen.getByLabelText(/youtube url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(await screen.findByText(/of brain activity/i)).toBeInTheDocument();
    expect(api.createUrlJob).toHaveBeenCalledWith(URL_A, {
      startSec: 0,
      endSec: 30,
    });
    expect(api.fetchActivation).toHaveBeenCalledWith(
      "https://cdn/job-1.json",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    // header switches from the CTA to an acknowledgement once results land.
    expect(
      screen.getByRole("heading", { name: /your brain on that video/i }),
    ).toBeInTheDocument();
  });

  it("surfaces a submit error, then clears it on a successful retry (P1.2)", async () => {
    api.createUrlJob
      .mockRejectedValueOnce(
        new apiV2.ApiV2Error({
          kind: "http",
          url: "/v2/jobs",
          status: 400,
          message: "bad",
        }),
      )
      .mockResolvedValueOnce({ jobId: "job-1" });
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      resultUrl: "https://cdn/job-1.json",
      createdAt: 0,
      elapsedSec: 1,
    });
    api.fetchActivation.mockResolvedValue(activation());

    const user = userEvent.setup();
    render(<SingleVideoPage />);
    const input = screen.getByLabelText(/youtube url/i);

    await user.type(input, URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /couldn't start the job/i,
    );

    // Fix the URL and resubmit — the stale error must not survive into the
    // successful run.
    await user.clear(input);
    await user.type(input, "https://www.youtube.com/watch?v=anotherVid1");
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(await screen.findByText(/of brain activity/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers the upload fallback on download_blocked and recovers via upload", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "download_blocked",
      createdAt: 0,
      elapsedSec: 1,
    });

    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(
      await screen.findByText(/youtube blocked our download/i),
    ).toBeInTheDocument();

    // Upload an MP4 from the error panel → new job, then keep it in-flight.
    api.createUploadJob.mockResolvedValue({
      jobId: "job-2",
      uploadUrl: "https://s3/put",
      uploadKey: "k",
    });
    api.putUpload.mockResolvedValue(undefined);
    api.confirmUpload.mockResolvedValue(undefined);
    api.getJob.mockResolvedValue({
      jobId: "job-2",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });

    const file = new File([new Uint8Array([1, 2, 3])], "clip.mp4", {
      type: "video/mp4",
    });
    await user.upload(screen.getByLabelText(/upload an mp4 video file/i), file);

    expect(
      await screen.findByText(/predicting brain activity/i),
    ).toBeInTheDocument();
    expect(api.createUploadJob).toHaveBeenCalledWith("clip.mp4", "video/mp4");
    expect(api.putUpload).toHaveBeenCalled();
    expect(api.confirmUpload).toHaveBeenCalledWith("job-2");
  });

  it("aborts the in-flight poll when unmounted (abort mid-fetch)", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    let captured: AbortSignal | undefined;
    api.getJob.mockImplementation((_id, opts) => {
      captured = opts?.signal;
      return new Promise(() => {}); // never resolves — stays in flight
    });

    const user = userEvent.setup();
    const { unmount } = render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    await vi.waitFor(() => expect(api.getJob).toHaveBeenCalled());
    expect(captured?.aborted).toBe(false);
    unmount();
    expect(captured?.aborted).toBe(true);
  });

  it("exposes a11y attributes on the URL input and the file input (P2.6)", async () => {
    const user = userEvent.setup();
    render(<SingleVideoPage />);

    const url = screen.getByLabelText(/youtube url/i);
    expect(url).toBeRequired();
    expect(url).toHaveAttribute("aria-required", "true");

    await user.click(
      screen.getByRole("button", { name: /upload an mp4 directly/i }),
    );
    expect(
      screen.getByLabelText(/upload an mp4 video file/i),
    ).toHaveAttribute("type", "file");
  });
});

describe("/single segment picker (Phase 2)", () => {
  it("validates the segment inline and gates Predict on it", async () => {
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);

    const end = screen.getByLabelText(/end \(s\)/i);
    const predict = () => screen.getByRole("button", { name: /^predict$/i });

    expect(predict()).toBeEnabled(); // default 0–30s is valid

    await user.clear(end);
    await user.type(end, "0"); // start 0 >= end 0 → bad_segment
    expect(predict()).toBeDisabled();
    expect(screen.getByText(/start before the end/i)).toBeInTheDocument();

    await user.clear(end);
    await user.type(end, "200"); // 200s window > 90s cap → segment_too_long
    expect(predict()).toBeDisabled();
    expect(screen.getByText(/90 seconds or less/i)).toBeInTheDocument();

    await user.clear(end);
    await user.type(end, "45");
    expect(predict()).toBeEnabled();
  });

  it("threads the chosen [startSec, endSec) into createUrlJob", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-x" });
    api.getJob.mockResolvedValue({
      jobId: "job-x",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });

    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);

    const start = screen.getByLabelText(/start \(s\)/i);
    const end = screen.getByLabelText(/end \(s\)/i);
    await user.clear(start);
    await user.type(start, "12");
    await user.clear(end);
    await user.type(end, "78");
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    await vi.waitFor(() =>
      expect(api.createUrlJob).toHaveBeenCalledWith(URL_A, {
        startSec: 12,
        endSec: 78,
      }),
    );
  });
});

describe("/single timeout boundary (P2.7)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("flips to the timeout error after the 180s budget with no terminal status", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "downloading",
      createdAt: 0,
      elapsedSec: 5,
    });

    render(<SingleVideoPage />);
    const input = screen.getByLabelText(/youtube url/i);
    fireEvent.change(input, { target: { value: URL_A } });
    fireEvent.click(screen.getByRole("button", { name: /^predict$/i }));

    // Flush the createUrlJob microtask → tracking + immediate poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByText(/fetching that segment/i)).toBeInTheDocument();

    // Cross the 180s wall-clock cap (check fires on the tick past it).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(183_000);
    });
    expect(
      screen.getByText(/taking longer than expected/i),
    ).toBeInTheDocument();
  });
});

describe("/single error resilience (unbreakable demo)", () => {
  async function predictDefault(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));
  }

  it("segment_out_of_bounds → calm message, no upload fallback", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "segment_out_of_bounds",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(
      await screen.findByText(/past the end of the video/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/upload an mp4 video file/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /try another/i }),
    ).toBeInTheDocument();
  });

  it("inference failure → calm message, no stack trace", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_inference",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("done with a malformed result → error panel, no crash", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
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
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("done with no resultUrl → error panel", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "done",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(
      await screen.findByText(/couldn't finish that prediction/i),
    ).toBeInTheDocument();
  });

  it("maps a server create-400 error_code to friendly copy", async () => {
    api.createUrlJob.mockRejectedValue(
      new apiV2.ApiV2Error({
        kind: "http",
        url: "/v2/jobs",
        status: 400,
        message: "bad",
        body: { error_code: "segment_out_of_bounds" },
      }),
    );
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /past the end of the video/i,
    );
  });

  it("never renders a raw error code or status string", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "download_blocked",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);

    expect(
      await screen.findByText(/youtube blocked our download/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/code:/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/failed_download/)).not.toBeInTheDocument();
    expect(screen.queryByText(/download_blocked/)).not.toBeInTheDocument();
  });

  it("does not double-submit when Predict is mashed", async () => {
    let resolveCreate: (v: { jobId: string }) => void = () => {};
    api.createUrlJob.mockImplementation(
      () => new Promise<{ jobId: string }>((r) => { resolveCreate = r; }),
    );
    api.getJob.mockResolvedValue({
      jobId: "job-1",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/youtube url/i), URL_A);
    const btn = screen.getByRole("button", { name: /^predict$/i });
    await user.click(btn);
    await user.click(btn).catch(() => {}); // disabled + guarded → no-op
    resolveCreate({ jobId: "job-1" });

    await vi.waitFor(() => expect(api.createUrlJob).toHaveBeenCalledTimes(1));
  });

  it("upload from the blocked panel can fail, then succeed (bulletproof)", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "download_blocked",
      createdAt: 0,
      elapsedSec: 1,
    });
    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await predictDefault(user);
    expect(
      await screen.findByText(/youtube blocked our download/i),
    ).toBeInTheDocument();

    const file = new File([new Uint8Array([1])], "clip.mp4", {
      type: "video/mp4",
    });

    // First attempt fails — the panel stays usable with an inline retry note.
    api.createUploadJob.mockRejectedValueOnce(
      new apiV2.ApiV2Error({
        kind: "offline",
        url: "/v2/jobs/upload",
        message: "down",
      }),
    );
    await user.upload(screen.getByLabelText(/upload an mp4 video file/i), file);
    expect(await screen.findByText(/didn't go through/i)).toBeInTheDocument();
    expect(
      screen.getByText(/youtube blocked our download/i),
    ).toBeInTheDocument();

    // Retry succeeds → tracking.
    api.createUploadJob.mockResolvedValue({
      jobId: "job-2",
      uploadUrl: "https://s3/put",
      uploadKey: "k",
    });
    api.putUpload.mockResolvedValue(undefined);
    api.confirmUpload.mockResolvedValue(undefined);
    api.getJob.mockResolvedValue({
      jobId: "job-2",
      status: "inferring",
      createdAt: 0,
      elapsedSec: 1,
    });
    // A fresh File so the input's change event re-fires (re-uploading the same
    // File object is a no-op in jsdom).
    const retryFile = new File([new Uint8Array([2])], "clip-retry.mp4", {
      type: "video/mp4",
    });
    await user.upload(
      screen.getByLabelText(/upload an mp4 video file/i),
      retryFile,
    );
    expect(
      await screen.findByText(/predicting brain activity/i),
    ).toBeInTheDocument();
  });
});

describe("/single poll resilience (fake timers)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("recovers from a transient poll failure mid-flight", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob
      .mockRejectedValueOnce(
        new apiV2.ApiV2Error({
          kind: "offline",
          url: "/v2/jobs/job-1",
          message: "blip",
        }),
      )
      .mockResolvedValue({
        jobId: "job-1",
        status: "done",
        resultUrl: "https://cdn/x.json",
        createdAt: 0,
        elapsedSec: 1,
      });
    api.fetchActivation.mockResolvedValue(activation());

    render(<SingleVideoPage />);
    fireEvent.change(screen.getByLabelText(/youtube url/i), {
      target: { value: URL_A },
    });
    fireEvent.click(screen.getByRole("button", { name: /^predict$/i }));

    // tracking + immediate poll (fails once) — no error surfaced.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.queryByText(/lost the connection/i)).not.toBeInTheDocument();

    // next tick polls again, succeeds → result.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2600);
    });
    expect(screen.getByText(/of brain activity/i)).toBeInTheDocument();
  });

  it("surfaces a connection error only after a sustained failure streak", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockRejectedValue(
      new apiV2.ApiV2Error({
        kind: "http",
        url: "/v2/jobs/job-1",
        status: 503,
        message: "down",
      }),
    );

    render(<SingleVideoPage />);
    fireEvent.change(screen.getByLabelText(/youtube url/i), {
      target: { value: URL_A },
    });
    fireEvent.click(screen.getByRole("button", { name: /^predict$/i }));

    // Settle into tracking + the immediate (failing) poll, then let the
    // streak build past the threshold.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12_000);
    });
    expect(screen.getByText(/lost the connection/i)).toBeInTheDocument();
  });
});
