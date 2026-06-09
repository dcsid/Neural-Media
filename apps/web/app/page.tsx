"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";
import {
  ApiV2Error,
  confirmUpload,
  createUploadJob,
  createUrlJob,
  fetchActivation,
  getJob,
  isFailureStatus,
  looksLikeYouTubeUrl,
  MAX_SEGMENT_SEC,
  putUpload,
  validateSegment,
  type ActivationPayload,
  type JobStatus,
  type JobStatusResponse,
  type TerminalFailureStatus,
} from "@/lib/api-v2";

// Re-uses the existing BrainMesh component. Dynamic with ssr:false
// matches BrainMeshSlot/AutoPlayingBrain — R3F's runtime touches browser-
// only globals and cannot enter the server render path.
const BrainMeshLazy = dynamic(
  () =>
    import("@/components/brain/BrainMesh").then((mod) => ({
      default: mod.BrainMesh,
    })),
  {
    ssr: false,
    loading: () => (
      <div
        aria-hidden
        className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(245,165,36,0.05),transparent_60%)]"
      />
    ),
  },
);

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type SubmitMode = "url" | "upload";

interface IdleState {
  kind: "idle";
  mode: SubmitMode;
  url: string;
  // Segment window as raw input strings so the number fields can be cleared
  // / partially typed; parsed + validated (CONTRACTS.md §13.2) at submit.
  // Applies to the URL path only — uploads are analyzed in full (§13.4).
  startInput: string;
  endInput: string;
  // Submission-time errors (e.g. POST /v2/jobs returned 4xx, file too large).
  // Re-renders the idle form with the error inline.
  submitError?: string;
  // Set while POST /v2/jobs (or the create-upload chain) is in flight, so
  // we can disable the Predict button without dropping the user's input.
  submitting?: boolean;
}

interface TrackingState {
  kind: "tracking";
  jobId: string;
  status: JobStatus;
  elapsedSec: number;
  // Wall-clock ms at which polling started. Used to enforce the 180s
  // overall cap independently of whatever elapsedSec the server reports
  // (which might lag or reset).
  startedAtMs: number;
}

// Specific failure modes the UI cares about. "network" covers polling
// fetches that died with no actionable server status (e.g. server went
// down mid-job). "timeout" fires when the 180s budget elapses without
// reaching a terminal status.
type ErrorKind = TerminalFailureStatus | "network" | "timeout";

interface ErrorState {
  kind: "error";
  errorKind: ErrorKind;
  // Server-supplied machine-readable error code (e.g. "download_blocked",
  // "segment_out_of_bounds"). Drives the upload-fallback / out-of-bounds
  // branches — never shown raw in the UI.
  errorCode?: string;
  // Set while an upload from the download-blocked fallback is in flight; holds
  // an inline message if that upload itself fails (so the panel stays usable).
  uploading?: boolean;
  uploadError?: string;
}

interface ResultState {
  kind: "result";
  jobId: string;
  activation: ActivationPayload;
}

type Phase = IdleState | TrackingState | ResultState | ErrorState;

// ---------------------------------------------------------------------------
// Status copy
// ---------------------------------------------------------------------------

const STATUS_COPY: Record<JobStatus, string> = {
  pending: "Queueing your job...",
  downloading:
    "Fetching that segment from YouTube... (this can fail — we'll let you know)",
  inferring: "Predicting brain activity...",
  done: "Loading results...",
  failed_download: "We couldn't fetch that video.",
  failed_inference: "The model couldn't process that segment.",
  rejected_duration: "Segment too long.",
};

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 180_000;
const TYPICAL_LATENCY_SEC = 30;

// A single failed status poll (a 5xx blip, a dropped packet) shouldn't kill
// the run — keep ticking and recover on the next success. Surface a connection
// error only after this many in a row; the 180s cap is the ultimate backstop.
const MAX_CONSECUTIVE_POLL_FAILURES = 5;

// User-facing copy for the contract error_codes (CONTRACTS.md §13.2). Shared
// by the picker's inline validation, the create-time 400 fallback, and the
// error panel.
const ERROR_CODE_COPY: Record<string, string> = {
  invalid_url: "That doesn't look like a YouTube URL.",
  bad_segment: "Pick a start and end with the start before the end.",
  segment_too_long: `Keep the window to ${MAX_SEGMENT_SEC} seconds or less.`,
  segment_out_of_bounds:
    "That window runs past the end of the video — pick an earlier one.",
  download_blocked: "YouTube blocked the download for that video.",
};

// Read the contract `error_code` off an ApiV2Error's parsed 400 body.
function errorCodeFromBody(body: unknown): string | undefined {
  if (typeof body === "object" && body !== null) {
    const code = (body as Record<string, unknown>).error_code;
    if (typeof code === "string") return code;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SingleVideoPage() {
  const [phase, setPhase] = useState<Phase>({
    kind: "idle",
    mode: "url",
    url: "",
    startInput: "0",
    endInput: "30",
  });

  // Top-level AbortController for any in-flight fetch (polling loop,
  // result download). Refreshed each time we enter a new phase that
  // owns requests; aborted on unmount or on phase transition.
  const abortRef = useRef<AbortController | null>(null);

  // Guards double-submit / resubmit-while-in-flight (a recruiter mashing
  // Predict or dropping two files): any create chain in progress short-circuits
  // a second until it settles. A ref so it updates synchronously, before React
  // re-renders the disabled button.
  const inFlightRef = useRef(false);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // ----- IDLE: submit handlers -------------------------------------------

  const submitUrl = useCallback(
    async (url: string, startSec: number, endSec: number) => {
      if (inFlightRef.current) return; // double-submit / resubmit-in-flight
      const trimmed = url.trim();
      if (!looksLikeYouTubeUrl(trimmed)) {
        setPhase((p) =>
          p.kind === "idle"
            ? { ...p, submitError: ERROR_CODE_COPY.invalid_url }
            : p,
        );
        return;
      }
      const segErr = validateSegment(startSec, endSec);
      if (segErr) {
        setPhase((p) =>
          p.kind === "idle" ? { ...p, submitError: ERROR_CODE_COPY[segErr] } : p,
        );
        return;
      }
      inFlightRef.current = true;
      setPhase((p) =>
        p.kind === "idle"
          ? { ...p, submitting: true, submitError: undefined }
          : p,
      );
      try {
        const { jobId } = await createUrlJob(trimmed, { startSec, endSec });
        setPhase({
          kind: "tracking",
          jobId,
          status: "pending",
          elapsedSec: 0,
          startedAtMs: Date.now(),
        });
      } catch (err) {
        // Server re-validates (§13.2): map a known error_code to friendly copy;
        // otherwise a calm, non-technical fallback (never a raw code/status).
        const code =
          err instanceof ApiV2Error ? errorCodeFromBody(err.body) : undefined;
        const msg =
          (code && ERROR_CODE_COPY[code]) ??
          "Couldn't start the job. Check your connection and try again.";
        setPhase((p) =>
          p.kind === "idle"
            ? { ...p, submitting: false, submitError: msg }
            : p,
        );
      } finally {
        inFlightRef.current = false;
      }
    },
    [],
  );

  const submitUpload = useCallback(async (file: File) => {
    if (inFlightRef.current) return; // double-drop / resubmit-in-flight
    inFlightRef.current = true;
    // Mark busy without losing the user's place: from idle, show the form
    // spinner; from the error panel, show the dropzone's uploading state.
    setPhase((p) => {
      if (p.kind === "idle") {
        return { ...p, submitting: true, submitError: undefined };
      }
      if (p.kind === "error") {
        return { ...p, uploading: true, uploadError: undefined };
      }
      return p;
    });
    try {
      const created = await createUploadJob(
        file.name,
        file.type || "video/mp4",
      );
      await putUpload(created.uploadUrl, file);
      await confirmUpload(created.jobId);
      setPhase({
        kind: "tracking",
        jobId: created.jobId,
        status: "pending",
        elapsedSec: 0,
        startedAtMs: Date.now(),
      });
    } catch {
      // Stay where the user is and offer a clean retry — from idle, inline on
      // the form; from the download-blocked panel, inline on the dropzone.
      setPhase((p) => {
        if (p.kind === "idle") {
          return { ...p, submitting: false, submitError: "Upload failed. Try again." };
        }
        if (p.kind === "error") {
          return {
            ...p,
            uploading: false,
            uploadError: "That upload didn't go through — try again.",
          };
        }
        return p;
      });
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    inFlightRef.current = false;
    setPhase({
      kind: "idle",
      mode: "url",
      url: "",
      startInput: "0",
      endInput: "30",
    });
  }, []);

  // ----- TRACKING: polling -----------------------------------------------

  // Hoisted, referentially-stable primitives so the polling effect depends
  // on exactly the job identity (jobId + startedAtMs). The per-tick status /
  // elapsed updates keep the same identity, so they must NOT re-run the
  // effect and restart polling; pulling these out of the dependency array
  // (rather than inlining ternaries) also keeps the deps statically
  // verifiable by react-hooks/exhaustive-deps.
  const trackingJobId = phase.kind === "tracking" ? phase.jobId : null;
  const trackingStartedAtMs =
    phase.kind === "tracking" ? phase.startedAtMs : null;

  useEffect(() => {
    if (trackingJobId === null || trackingStartedAtMs === null) return;
    const jobId = trackingJobId;
    const startedAtMs = trackingStartedAtMs;
    const controller = new AbortController();
    abortRef.current = controller;

    let cancelled = false;
    let consecutiveFailures = 0;

    async function poll() {
      let res: JobStatusResponse;
      try {
        res = await getJob(jobId, { signal: controller.signal });
        consecutiveFailures = 0; // a successful poll clears the streak
      } catch {
        if (controller.signal.aborted) return;
        // Transient failure (offline, 5xx, parse): don't kill the run on a
        // blip — the next tick retries. Surface a connection error only after
        // a sustained streak; the 180s cap is the ultimate backstop.
        consecutiveFailures += 1;
        if (consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES && !cancelled) {
          setPhase({ kind: "error", errorKind: "network" });
        }
        return;
      }
      if (cancelled) return;

      if (res.status === "done") {
        if (!res.resultUrl) {
          setPhase({ kind: "error", errorKind: "failed_inference" });
          return;
        }
        try {
          const activation = await fetchActivation(res.resultUrl, {
            signal: controller.signal,
          });
          if (cancelled) return;
          setPhase({ kind: "result", jobId, activation });
        } catch (err) {
          if (controller.signal.aborted) return;
          if (!cancelled) {
            setPhase({
              kind: "error",
              errorKind: "failed_inference",
              errorCode:
                err instanceof ApiV2Error ? `result_${err.kind}` : undefined,
            });
          }
        }
        return;
      }

      if (isFailureStatus(res.status)) {
        setPhase({
          kind: "error",
          errorKind: res.status,
          errorCode: res.error,
        });
        return;
      }

      // Still in flight — update status + elapsed. Prefer the server's
      // elapsedSec if it's monotonically newer; otherwise compute from
      // wall clock so the meter ticks while we wait for the next poll.
      const wallSec = Math.floor((Date.now() - startedAtMs) / 1000);
      setPhase((p) =>
        p.kind === "tracking" && p.jobId === jobId
          ? {
              ...p,
              status: res.status,
              elapsedSec: Math.max(res.elapsedSec ?? 0, wallSec),
            }
          : p,
      );
    }

    // Fire immediately, then on an interval. Wrap the wall-clock cap
    // check inside the interval so we don't fire-and-leak a final poll
    // past the cap.
    poll();
    const interval = window.setInterval(() => {
      if (cancelled || controller.signal.aborted) return;
      if (Date.now() - startedAtMs > POLL_TIMEOUT_MS) {
        cancelled = true;
        window.clearInterval(interval);
        setPhase({ kind: "error", errorKind: "timeout" });
        return;
      }
      poll();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(interval);
    };
  }, [trackingJobId, trackingStartedAtMs]);

  // ----- Render -----------------------------------------------------------

  return (
    <main className="mx-auto max-w-[1280px] px-8 pb-16 pt-12">
      <header className="motion-fade-in">
        <p className="eyebrow">Single video</p>
        <h1 className="mt-2 font-serif text-[28px] leading-tight tracking-tightish text-ink-50">
          {phase.kind === "result"
            ? "Your brain on that video"
            : "See your brain on a YouTube clip"}
        </h1>
        <p className="mt-3 max-w-[64ch] text-[14px] leading-relaxed text-ink-200">
          {phase.kind === "result"
            ? "Predicted average cortical response for your segment, rendered on the fsaverage5 surface below."
            : `Paste a YouTube link, choose up to ${MAX_SEGMENT_SEC} seconds, and watch the predicted cortical response light up the 3D brain.`}
        </p>
      </header>

      <section className="mt-10 grid gap-8 md:grid-cols-[minmax(0,1fr)_minmax(0,460px)]">
        <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas">
          {phase.kind === "result" ? (
            <BrainMeshLazy
              activation={meanFromActivation(phase.activation)}
              keyframeVertices={phase.activation.byRegion}
              timestamps={phase.activation.timestamps}
              playheadSec={0}
            />
          ) : (
            <BrainMeshLazy activation={0} />
          )}
        </div>

        <div className="flex min-h-full flex-col">
          {phase.kind === "idle" && (
            <IdlePanel
              state={phase}
              onChange={(next) => setPhase(next)}
              onSubmitUrl={submitUrl}
              onSubmitUpload={submitUpload}
            />
          )}

          {phase.kind === "tracking" && <TrackingPanel state={phase} />}

          {phase.kind === "result" && (
            <ResultPanel state={phase} onReset={reset} />
          )}

          {phase.kind === "error" && (
            <ErrorPanel
              state={phase}
              onReset={reset}
              onSubmitUpload={submitUpload}
            />
          )}
        </div>
      </section>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Idle panel
// ---------------------------------------------------------------------------

interface IdlePanelProps {
  state: IdleState;
  onChange: (next: IdleState) => void;
  onSubmitUrl: (url: string, startSec: number, endSec: number) => void;
  onSubmitUpload: (file: File) => void;
}

function IdlePanel({
  state,
  onChange,
  onSubmitUrl,
  onSubmitUpload,
}: IdlePanelProps) {
  const urlValid = looksLikeYouTubeUrl(state.url);
  const startSec = Number.parseFloat(state.startInput);
  const endSec = Number.parseFloat(state.endInput);
  const segmentError = validateSegment(startSec, endSec);
  const canSubmit = urlValid && segmentError === null && !state.submitting;

  const onUrlChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange({ ...state, url: e.target.value, submitError: undefined });
  };
  const onStartChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange({ ...state, startInput: e.target.value, submitError: undefined });
  };
  const onEndChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange({ ...state, endInput: e.target.value, submitError: undefined });
  };

  const onUrlSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmitUrl(state.url, startSec, endSec);
  };

  const switchToUpload = () =>
    onChange({ ...state, mode: "upload", submitError: undefined });
  const switchToUrl = () =>
    onChange({ ...state, mode: "url", submitError: undefined });

  return (
    <div className="motion-fade-in flex h-full flex-col gap-6 border border-line bg-surface/40 p-6">
      {state.mode === "url" ? (
        <>
          <form onSubmit={onUrlSubmit} className="space-y-3">
            <label className="eyebrow block" htmlFor="single-youtube-url">
              YouTube URL
            </label>
            <input
              id="single-youtube-url"
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              required
              aria-required="true"
              placeholder="https://www.youtube.com/watch?v=..."
              value={state.url}
              onChange={onUrlChange}
              disabled={state.submitting}
              className={[
                "w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px]",
                "text-ink-100 placeholder:text-ink-400",
                "focus-visible:border-accent focus-visible:outline-none",
              ].join(" ")}
            />
            {state.url.length > 0 && !urlValid && (
              <p role="alert" className="text-[12px] text-accent">
                {ERROR_CODE_COPY.invalid_url}
              </p>
            )}

            <fieldset className="space-y-2" disabled={state.submitting}>
              <legend className="eyebrow">
                Segment to analyze (≤{MAX_SEGMENT_SEC}s)
              </legend>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label
                    htmlFor="single-start-sec"
                    className="block text-[11px] text-ink-300"
                  >
                    Start (s)
                  </label>
                  <input
                    id="single-start-sec"
                    type="number"
                    min={0}
                    step={1}
                    inputMode="decimal"
                    value={state.startInput}
                    onChange={onStartChange}
                    aria-describedby="single-segment-hint"
                    className="mt-1 w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px] tabular-nums text-ink-100 focus-visible:border-accent focus-visible:outline-none"
                  />
                </div>
                <div>
                  <label
                    htmlFor="single-end-sec"
                    className="block text-[11px] text-ink-300"
                  >
                    End (s)
                  </label>
                  <input
                    id="single-end-sec"
                    type="number"
                    min={0}
                    step={1}
                    inputMode="decimal"
                    value={state.endInput}
                    onChange={onEndChange}
                    aria-describedby="single-segment-hint"
                    className="mt-1 w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px] tabular-nums text-ink-100 focus-visible:border-accent focus-visible:outline-none"
                  />
                </div>
              </div>
              <p
                id="single-segment-hint"
                className="font-mono text-[11px] text-ink-400"
              >
                {segmentError ? (
                  <span role="alert" className="text-accent">
                    {ERROR_CODE_COPY[segmentError]}
                  </span>
                ) : (
                  `${(endSec - startSec).toFixed(0)}s selected · up to ${MAX_SEGMENT_SEC}s`
                )}
              </p>
            </fieldset>

            <button
              type="submit"
              disabled={!canSubmit}
              className={[
                "w-full border px-4 py-2 font-mono text-[12px]",
                "uppercase tracking-[0.08em] transition-colors",
                canSubmit
                  ? "border-accent bg-accent/10 text-accent hover:bg-accent/20"
                  : "cursor-not-allowed border-line bg-surface/40 text-ink-500",
              ].join(" ")}
            >
              {state.submitting ? "Submitting..." : "Predict"}
            </button>
            {state.submitError && (
              <p role="alert" className="text-[12px] text-accent">
                {state.submitError}
              </p>
            )}
          </form>

          <div className="text-[12px] leading-relaxed text-ink-300">
            Or{" "}
            <button
              type="button"
              onClick={switchToUpload}
              className="text-accent underline underline-offset-2 transition-opacity hover:opacity-80"
            >
              upload an MP4 directly
            </button>{" "}
            — useful when YouTube blocks the download.
          </div>
          <div className="text-[12px] leading-relaxed text-ink-300">
            No clip handy?{" "}
            <Link
              href="/gallery"
              className="text-accent underline underline-offset-2 transition-opacity hover:opacity-80"
            >
              Try a demo
            </Link>{" "}
            from the precomputed gallery.
          </div>
        </>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <p className="eyebrow">Upload MP4</p>
            <button
              type="button"
              onClick={switchToUrl}
              className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300 hover:text-accent"
            >
              ← back to URL
            </button>
          </div>
          <UploadDropzone
            disabled={state.submitting === true}
            onFile={onSubmitUpload}
          />
          {state.submitError && (
            <p role="alert" className="text-[12px] text-accent">
              {state.submitError}
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tracking panel
// ---------------------------------------------------------------------------

function TrackingPanel({ state }: { state: TrackingState }) {
  // A vague progress hint — we don't actually know how far through the
  // job is, but anchoring against TYPICAL_LATENCY_SEC keeps the bar
  // moving even when status doesn't change. Cap at 95% so it never
  // visually finishes ahead of the actual result.
  const progress = useMemo(
    () => Math.min(95, Math.round((state.elapsedSec / TYPICAL_LATENCY_SEC) * 100)),
    [state.elapsedSec],
  );
  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-line bg-surface/40 p-6">
      <p className="eyebrow">Job {shortId(state.jobId)}</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">
        {STATUS_COPY[state.status]}
      </p>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
        className="relative h-[3px] w-full overflow-hidden bg-line"
      >
        <div
          className="absolute inset-y-0 left-0 bg-accent transition-[width] duration-500 ease-out"
          style={{ width: `${progress}%` }}
        />
      </div>
      <p className="font-mono text-[11px] tabular-nums text-ink-300">
        {state.elapsedSec}s elapsed · usually ~{TYPICAL_LATENCY_SEC}s
      </p>
      <p className="mt-auto max-w-[40ch] text-[12px] leading-relaxed text-ink-400">
        The brain on the left is the placeholder. It animates in with the
        prediction the moment results land.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result panel
// ---------------------------------------------------------------------------

function ResultPanel({
  state,
  onReset,
}: {
  state: ResultState;
  onReset: () => void;
}) {
  const { activation } = state;
  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-line bg-surface/40 p-6">
      <p className="eyebrow">Result · {shortId(state.jobId)}</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">
        {activation.videoDurationSec.toFixed(1)}s of brain activity
      </p>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[12px]">
        <dt className="text-ink-400">Duration</dt>
        <dd className="font-mono tabular-nums text-ink-100">
          {activation.videoDurationSec.toFixed(2)}s
        </dd>
        <dt className="text-ink-400">Timepoints</dt>
        <dd className="font-mono tabular-nums text-ink-100">
          {activation.timestamps.length}
        </dd>
        <dt className="text-ink-400">Model</dt>
        <dd className="font-mono text-ink-100">{activation.modelVersion}</dd>
      </dl>
      <p className="text-[12px] leading-relaxed text-ink-300">
        Predicted average BOLD response across {Object.keys(activation.byRegion).length}
        {" "}regions of the fsaverage5 cortical surface — not your individual brain.
      </p>
      <button
        type="button"
        onClick={onReset}
        className="mt-auto w-full border border-line px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-200 transition-colors hover:border-accent hover:text-accent"
      >
        Try another
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error panel
// ---------------------------------------------------------------------------

interface ErrorPanelProps {
  state: ErrorState;
  onReset: () => void;
  onSubmitUpload: (file: File) => void;
}

function ErrorPanel({ state, onReset, onSubmitUpload }: ErrorPanelProps) {
  const isDownloadBlocked =
    state.errorKind === "failed_download" &&
    state.errorCode === "download_blocked";
  const isOutOfBounds = state.errorCode === "segment_out_of_bounds";
  const isRejectedDuration = state.errorKind === "rejected_duration";
  const isTimeout = state.errorKind === "timeout";
  const isNetwork = state.errorKind === "network";

  let heading = "Something went wrong";
  let body = "Something tripped up on our end. Start over and give it another go.";
  if (isDownloadBlocked) {
    heading = "YouTube blocked our download";
    body =
      "Save the video to your device and drop it here — we'll run the prediction on the upload.";
  } else if (isOutOfBounds) {
    heading = "That window is past the end of the video";
    body =
      "Your segment runs past where the video ends. Pick an earlier window and try again.";
  } else if (isRejectedDuration) {
    heading = "Segment too long";
    body = `Segments longer than ${MAX_SEGMENT_SEC} seconds aren't supported. Pick a shorter window.`;
  } else if (state.errorKind === "failed_download") {
    heading = "We couldn't fetch that video";
    body =
      "YouTube wouldn't serve it. If you have the file locally, upload it and we'll run the prediction directly.";
  } else if (state.errorKind === "failed_inference") {
    heading = "We couldn't finish that prediction";
    body =
      "The run didn't complete — usually a transient hiccup. Start over and try again.";
  } else if (isNetwork) {
    heading = "We lost the connection";
    body =
      "We couldn't reach the prediction service. Check your connection and try again.";
  } else if (isTimeout) {
    heading = "This is taking longer than expected";
    body =
      "We stopped checking after 3 minutes. The job may still finish — try again, or come back in a moment.";
  }

  // Upload fallback only helps when the download itself was blocked — not for
  // an out-of-bounds segment (a timestamp fix) or a dropped connection.
  const showUploadFallback =
    state.errorKind === "failed_download" && !isOutOfBounds;
  const tryAnother = isRejectedDuration || isTimeout || isOutOfBounds;

  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-accent/40 bg-surface/40 p-6">
      <p className="eyebrow text-accent">Error</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">{heading}</p>
      <p className="max-w-[44ch] text-[13px] leading-relaxed text-ink-200">
        {body}
      </p>

      {showUploadFallback && (
        <>
          <UploadDropzone
            disabled={state.uploading === true}
            onFile={onSubmitUpload}
            prominent
          />
          {state.uploading && (
            <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300">
              Uploading…
            </p>
          )}
          {state.uploadError && (
            <p role="alert" className="text-[12px] text-accent">
              {state.uploadError}
            </p>
          )}
        </>
      )}

      <button
        type="button"
        onClick={onReset}
        className="mt-auto w-full border border-line px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-200 transition-colors hover:border-accent hover:text-accent"
      >
        {tryAnother ? "Try another" : "Start over"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Upload dropzone
// ---------------------------------------------------------------------------

interface UploadDropzoneProps {
  disabled: boolean;
  onFile: (file: File) => void;
  prominent?: boolean;
}

function UploadDropzone({ disabled, onFile, prominent }: UploadDropzoneProps) {
  const [hover, setHover] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = (file: File | undefined) => {
    if (!file || disabled) return;
    onFile(file);
  };

  const onDrop = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setHover(false);
    handleFile(e.dataTransfer.files?.[0]);
  };
  const onDragOver = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    if (!hover) setHover(true);
  };
  const onDragLeave = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    if (hover) setHover(false);
  };

  return (
    <label
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      className={[
        "flex cursor-pointer flex-col items-center justify-center gap-2 border border-dashed text-center transition-colors",
        prominent ? "min-h-[180px] px-6 py-8" : "min-h-[140px] px-4 py-6",
        hover
          ? "border-accent bg-accent/5 text-accent"
          : "border-line text-ink-200 hover:border-accent hover:text-accent",
        // The real <input> is visually hidden (sr-only), so surface keyboard
        // focus on the label itself.
        "focus-within:border-accent focus-within:text-accent",
        disabled ? "cursor-not-allowed opacity-50" : "",
      ].join(" ")}
    >
      <input
        ref={inputRef}
        type="file"
        accept="video/mp4,video/*"
        aria-label="Upload an MP4 video file"
        disabled={disabled}
        onChange={(e) => handleFile(e.target.files?.[0] ?? undefined)}
        className="sr-only"
      />
      <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300">
        Drop MP4 here
      </span>
      <span className="text-[13px] text-ink-100">
        or click to choose a file
      </span>
      <span className="font-mono text-[10px] text-ink-400">
        up to 90s, MP4 preferred
      </span>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function shortId(id: string): string {
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}

function meanFromActivation(a: ActivationPayload): number {
  let sum = 0;
  let count = 0;
  for (const series of Object.values(a.byRegion)) {
    if (series.length === 0) continue;
    sum += series[0] ?? 0;
    count += 1;
  }
  if (count === 0) return 0;
  // Clamp to [0,1] for BrainMesh's scalar activation prop. The brief
  // says playheadSec=0, so use the first frame's mean across regions
  // as the global scalar — it's the honest single-number rendering of
  // the moment the mesh is showing.
  return Math.max(0, Math.min(1, sum / count));
}
