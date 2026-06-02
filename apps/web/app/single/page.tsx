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
  looksLikeTikTokUrl,
  putUpload,
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
  // Server-supplied machine-readable error code (e.g. "tiktok_blocked").
  // Drives the upload-fallback branch when errorKind=failed_download.
  errorCode?: string;
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
    "Fetching the TikTok... (this sometimes fails — we'll let you know)",
  inferring: "Predicting brain activity...",
  done: "Loading results...",
  failed_download: "We couldn't fetch that video.",
  failed_inference: "The model couldn't process that clip.",
  rejected_duration: "Clip too long.",
};

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 180_000;
const TYPICAL_LATENCY_SEC = 30;

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SingleVideoPage() {
  const [phase, setPhase] = useState<Phase>({
    kind: "idle",
    mode: "url",
    url: "",
  });

  // Top-level AbortController for any in-flight fetch (polling loop,
  // result download). Refreshed each time we enter a new phase that
  // owns requests; aborted on unmount or on phase transition.
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // ----- IDLE: submit handlers -------------------------------------------

  const submitUrl = useCallback(
    async (url: string) => {
      const trimmed = url.trim();
      if (!looksLikeTikTokUrl(trimmed)) {
        setPhase((p) =>
          p.kind === "idle"
            ? { ...p, submitError: "That doesn't look like a TikTok URL." }
            : p,
        );
        return;
      }
      setPhase((p) =>
        p.kind === "idle"
          ? { ...p, submitting: true, submitError: undefined }
          : p,
      );
      try {
        const { jobId } = await createUrlJob(trimmed);
        setPhase({
          kind: "tracking",
          jobId,
          status: "pending",
          elapsedSec: 0,
          startedAtMs: Date.now(),
        });
      } catch (err) {
        const msg =
          err instanceof ApiV2Error
            ? `Couldn't start the job (${err.kind}${err.status ? ` ${err.status}` : ""}).`
            : "Couldn't start the job.";
        setPhase((p) =>
          p.kind === "idle"
            ? { ...p, submitting: false, submitError: msg }
            : p,
        );
      }
    },
    [],
  );

  const submitUpload = useCallback(async (file: File) => {
    // Set a busy flag if we're still in idle, so the form shows progress.
    setPhase((p) =>
      p.kind === "idle"
        ? { ...p, submitting: true, submitError: undefined }
        : p,
    );
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
    } catch (err) {
      const msg =
        err instanceof ApiV2Error
          ? `Upload failed (${err.kind}${err.status ? ` ${err.status}` : ""}).`
          : "Upload failed.";
      // We may already be in the error phase (uploading FROM the
      // tiktok_blocked screen) — in that case stay in error but surface
      // the inline message via errorCode. From idle, fall back to idle.
      setPhase((p) => {
        if (p.kind === "idle") {
          return { ...p, submitting: false, submitError: msg };
        }
        if (p.kind === "error") {
          return { ...p, errorCode: "upload_failed" };
        }
        return p;
      });
    }
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase({ kind: "idle", mode: "url", url: "" });
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

    async function poll() {
      let res: JobStatusResponse;
      try {
        res = await getJob(jobId, { signal: controller.signal });
      } catch (err) {
        if (controller.signal.aborted) return;
        // One transient failure shouldn't kill the loop — log and keep
        // ticking. If we hit the timeout we'll surface it then.
        if (err instanceof ApiV2Error && err.kind === "offline") {
          return;
        }
        // For HTTP/parse errors, treat as a network failure so the user
        // sees something rather than an indefinite spinner.
        if (!cancelled) {
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
            ? "Your brain on that TikTok"
            : "See your brain on a TikTok"}
        </h1>
        <p className="mt-3 max-w-[64ch] text-[14px] leading-relaxed text-ink-200">
          {phase.kind === "result"
            ? "Predicted average cortical response for your clip, rendered on the fsaverage5 surface below."
            : "Paste a TikTok URL. Watch your brain predict its way through the video."}
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
  onSubmitUrl: (url: string) => void;
  onSubmitUpload: (file: File) => void;
}

function IdlePanel({
  state,
  onChange,
  onSubmitUrl,
  onSubmitUpload,
}: IdlePanelProps) {
  const valid = looksLikeTikTokUrl(state.url);

  const onUrlChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange({ ...state, url: e.target.value, submitError: undefined });
  };

  const onUrlSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!valid || state.submitting) return;
    onSubmitUrl(state.url);
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
            <label className="eyebrow block" htmlFor="single-tiktok-url">
              TikTok URL
            </label>
            <input
              id="single-tiktok-url"
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              required
              aria-required="true"
              placeholder="https://www.tiktok.com/@user/video/..."
              value={state.url}
              onChange={onUrlChange}
              disabled={state.submitting}
              className={[
                "w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px]",
                "text-ink-100 placeholder:text-ink-400",
                "focus-visible:border-accent focus-visible:outline-none",
              ].join(" ")}
            />
            <button
              type="submit"
              disabled={!valid || state.submitting}
              className={[
                "w-full border px-4 py-2 font-mono text-[12px]",
                "uppercase tracking-[0.08em] transition-colors",
                valid && !state.submitting
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
              className="underline-offset-2 hover:text-accent hover:underline focus-visible:text-accent focus-visible:underline"
            >
              upload an MP4 directly
            </button>{" "}
            — useful when TikTok blocks the fetch.
          </div>
          <div className="text-[12px] leading-relaxed text-ink-300">
            No clip handy?{" "}
            <Link
              href="/single/gallery"
              className="underline-offset-2 hover:text-accent hover:underline focus-visible:text-accent focus-visible:underline"
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
  const isTikTokBlocked =
    state.errorKind === "failed_download" && state.errorCode === "tiktok_blocked";
  const isRejectedDuration = state.errorKind === "rejected_duration";
  const isTimeout = state.errorKind === "timeout";

  let heading = "Something went wrong";
  let body =
    "We hit an error while predicting. Try the URL again, or upload the MP4 directly.";
  if (isTikTokBlocked) {
    heading = "TikTok blocked our download";
    body =
      "Save the video to your device and drop it here — we'll run the prediction on the upload.";
  } else if (isRejectedDuration) {
    heading = "Clip too long";
    body =
      "Clips longer than 90 seconds aren't supported. Try a shorter one.";
  } else if (state.errorKind === "failed_download") {
    heading = "We couldn't fetch that video";
    body =
      "TikTok wouldn't serve it. If you have the file locally, upload it and we'll run the prediction directly.";
  } else if (state.errorKind === "failed_inference") {
    heading = "The model couldn't process that clip";
    body =
      "Inference failed partway through. This is usually a transient issue — try again, or upload a different clip.";
  } else if (isTimeout) {
    heading = "This is taking longer than expected";
    body =
      "We stopped polling after 3 minutes. The job may still finish — refresh the page to check, or try again.";
  }

  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-accent/40 bg-surface/40 p-6">
      <p className="eyebrow text-accent">Error · {state.errorKind}</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">{heading}</p>
      <p className="max-w-[44ch] text-[13px] leading-relaxed text-ink-200">
        {body}
      </p>

      {(isTikTokBlocked || state.errorKind === "failed_download") && (
        <UploadDropzone disabled={false} onFile={onSubmitUpload} prominent />
      )}

      {state.errorCode && (
        <p className="font-mono text-[11px] text-ink-400">
          code: {state.errorCode}
        </p>
      )}

      <button
        type="button"
        onClick={onReset}
        className="mt-auto w-full border border-line px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-200 transition-colors hover:border-accent hover:text-accent"
      >
        {isRejectedDuration || isTimeout ? "Try another" : "Start over"}
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
