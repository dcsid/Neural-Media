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

import SingleVideoPage from "@/app/single/page";
import * as apiV2 from "@/lib/api-v2";

const api = vi.mocked(apiV2);

const URL_A = "https://www.tiktok.com/@nasa/video/123";

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

    await user.type(screen.getByLabelText(/tiktok url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(await screen.findByText(/of brain activity/i)).toBeInTheDocument();
    expect(api.createUrlJob).toHaveBeenCalledWith(URL_A);
    expect(api.fetchActivation).toHaveBeenCalledWith(
      "https://cdn/job-1.json",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    // P3.9: header switches from the CTA to an acknowledgement.
    expect(
      screen.getByRole("heading", { name: /your brain on that tiktok/i }),
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
    const input = screen.getByLabelText(/tiktok url/i);

    await user.type(input, URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /couldn't start the job/i,
    );

    // Fix the URL and resubmit — the stale error must not survive into the
    // successful run.
    await user.clear(input);
    await user.type(input, "https://www.tiktok.com/@nasa/video/999");
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(await screen.findByText(/of brain activity/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers the upload fallback on tiktok_blocked and recovers via upload", async () => {
    api.createUrlJob.mockResolvedValue({ jobId: "job-1" });
    api.getJob.mockResolvedValueOnce({
      jobId: "job-1",
      status: "failed_download",
      error: "tiktok_blocked",
      createdAt: 0,
      elapsedSec: 1,
    });

    const user = userEvent.setup();
    render(<SingleVideoPage />);
    await user.type(screen.getByLabelText(/tiktok url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    expect(
      await screen.findByText(/tiktok blocked our download/i),
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
    await user.type(screen.getByLabelText(/tiktok url/i), URL_A);
    await user.click(screen.getByRole("button", { name: /^predict$/i }));

    await vi.waitFor(() => expect(api.getJob).toHaveBeenCalled());
    expect(captured?.aborted).toBe(false);
    unmount();
    expect(captured?.aborted).toBe(true);
  });

  it("exposes a11y attributes on the URL input and the file input (P2.6)", async () => {
    const user = userEvent.setup();
    render(<SingleVideoPage />);

    const url = screen.getByLabelText(/tiktok url/i);
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
    const input = screen.getByLabelText(/tiktok url/i);
    fireEvent.change(input, { target: { value: URL_A } });
    fireEvent.click(screen.getByRole("button", { name: /^predict$/i }));

    // Flush the createUrlJob microtask → tracking + immediate poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByText(/fetching the tiktok/i)).toBeInTheDocument();

    // Cross the 180s wall-clock cap (check fires on the tick past it).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(183_000);
    });
    expect(
      screen.getByText(/taking longer than expected/i),
    ).toBeInTheDocument();
  });
});
