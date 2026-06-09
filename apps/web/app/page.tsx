"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import {
  useCallback,
  useEffect,
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
  fetchActivation,
  getJob,
  isFailureStatus,
  MAX_SEGMENT_SEC,
  putUpload,
  validateSegment,
  type ActivationPayload,
  type JobStatus,
  type JobStatusResponse,
  type TerminalFailureStatus,
} from "@/lib/api-v2";
import { LiveResultViewer } from "@/components/brain/LiveResultViewer";

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
//
// The product is upload-only: YouTube-by-URL is blocked from datacenter IPs
// (yt-dlp → 403 from AWS/HF), so the live path is "drop an MP4, pick a ≤90s
// window, watch the synced video+brain". The URL form is retired (the lib
// keeps createUrlJob/looksLikeYouTubeUrl dormant for a future re-add).
// ---------------------------------------------------------------------------

interface IdleState {
  kind: "idle";
  // The uploaded MP4 (null until the user picks one → dropzone shown).
  file: File | null;
  // Duration read from the file's own metadata (null while probing / no file).
  // Bounds the picker's End so we never request a window past the file's end.
  fileDurationSec: number | null;
  // Inline message if the picked file's metadata couldn't be read.
  fileError?: string;
  // Segment window as raw input strings so the number fields can be cleared /
  // partially typed; parsed + validated (CONTRACTS §13.2/§13.4) at submit.
  startInput: string;
  endInput: string;
  // Submission-time error (create/put/confirm 4xx, network). Shown inline.
  submitError?: string;
  // Set while the create→put→confirm chain is in flight.
  submitting?: boolean;
}

interface TrackingState {
  kind: "tracking";
  jobId: string;
  status: JobStatus;
  elapsedSec: number;
  // Wall-clock ms at which polling started. Enforces the overall cap
  // independently of whatever elapsedSec the server reports.
  startedAtMs: number;
  // Carried from the upload so the result can play the LOCAL file (never
  // re-downloaded), trimmed to the analyzed [startSec, endSec) window.
  file: File;
  startSec: number;
  endSec: number;
}

// Specific failure modes the UI cares about. "network" covers polling fetches
// that died with no actionable server status; "timeout" fires when the overall
// budget elapses without reaching a terminal status.
type ErrorKind = TerminalFailureStatus | "network" | "timeout";

interface ErrorState {
  kind: "error";
  errorKind: ErrorKind;
  // Server-supplied machine-readable error code — drives copy, never shown raw.
  errorCode?: string;
}

interface ResultState {
  kind: "result";
  jobId: string;
  activation: ActivationPayload;
  // The local upload + where its analyzed window starts, for the synced player.
  file: File;
  startSec: number;
}

type Phase = IdleState | TrackingState | ResultState | ErrorState;

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

const POLL_INTERVAL_MS = 2000;
// Real TRIBE forward pass is ~12s/segment on the A10G, but the FIRST request
// after the Space has been idle pays a cold-start weight warm-up (~17GB lazy
// weights). 10 minutes covers a cold start + a long 90s clip without
// false-timing-out a job that's actually still running.
const POLL_TIMEOUT_MS = 600_000;

// Live inference is wired up (Stage 2: HF Space + Lambda). Set false to fall
// back to the coming-soon panel pointing at the precomputed gallery.
const LIVE_INFERENCE_ENABLED: boolean = true;

// A single failed status poll (a 5xx blip, a dropped packet) shouldn't kill the
// run — keep ticking and recover on the next success. Surface a connection
// error only after this many in a row; the timeout cap is the ultimate backstop.
const MAX_CONSECUTIVE_POLL_FAILURES = 5;

// User-facing copy for the contract error_codes (CONTRACTS.md §13.2). Shared by
// the picker's inline validation, the confirm-time 400 fallback, and the error
// panel.
const ERROR_CODE_COPY: Record<string, string> = {
  bad_segment: "Pick a start and end with the start before the end.",
  segment_too_long: `Keep the window to ${MAX_SEGMENT_SEC} seconds or less.`,
  segment_out_of_bounds: "That window runs past the end of your clip — pick an earlier one.",
};

// Read the contract `error_code` off an ApiV2Error's parsed 400 body.
function errorCodeFromBody(body: unknown): string | undefined {
  if (typeof body === "object" && body !== null) {
    const code = (body as Record<string, unknown>).error_code;
    if (typeof code === "string") return code;
  }
  return undefined;
}

// Probe a local video File's duration without keeping the element around. Used
// to bound the segment picker at the real file length. Rejects on a file we
// can't decode (so the UI can ask for a standard MP4).
function readVideoDuration(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error("no document"));
      return;
    }
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    const done = (fn: () => void) => {
      URL.revokeObjectURL(url);
      fn();
    };
    v.onloadedmetadata = () => {
      const d = Number.isFinite(v.duration) ? v.duration : 0;
      done(() => (d > 0 ? resolve(d) : reject(new Error("zero duration"))));
    };
    v.onerror = () => done(() => reject(new Error("decode error")));
    v.src = url;
  });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SingleVideoPage() {
  const [phase, setPhase] = useState<Phase>({
    kind: "idle",
    file: null,
    fileDurationSec: null,
    startInput: "0",
    endInput: "0",
  });

  // Top-level AbortController for any in-flight fetch (polling loop, result
  // download). Refreshed each time we enter a phase that owns requests.
  const abortRef = useRef<AbortController | null>(null);

  // Guards double-submit / resubmit-while-in-flight (a recruiter mashing
  // Predict or dropping two files). A ref so it updates synchronously.
  const inFlightRef = useRef(false);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // ----- IDLE: pick a file + probe its duration --------------------------

  const onFileSelected = useCallback((file: File) => {
    if (inFlightRef.current) return; // create-chain in flight
    // Show the file immediately in a "reading…" state; probe its duration so
    // the picker can bound End at the real file length.
    setPhase({
      kind: "idle",
      file,
      fileDurationSec: null,
      startInput: "0",
      endInput: "0",
    });
    readVideoDuration(file)
      .then((dur) => {
        // Default to the first ≤30s of the clip — short enough to keep the
        // demo snappy, the user can extend to the 90s cap.
        const end = Math.min(dur, 30);
        setPhase((p) =>
          p.kind === "idle" && p.file === file
            ? {
                ...p,
                fileDurationSec: dur,
                startInput: "0",
                endInput: String(Math.round(end)),
              }
            : p,
        );
      })
      .catch(() => {
        setPhase((p) =>
          p.kind === "idle" && p.file === file
            ? {
                ...p,
                file: null,
                fileDurationSec: null,
                fileError: "Couldn't read that video. Try a standard MP4 (H.264).",
              }
            : p,
        );
      });
  }, []);

  // ----- IDLE: start the prediction --------------------------------------

  const startPrediction = useCallback(
    async (file: File, startSec: number, endSec: number) => {
      if (inFlightRef.current) return; // double-submit / resubmit-in-flight
      inFlightRef.current = true;
      setPhase((p) =>
        p.kind === "idle"
          ? { ...p, submitting: true, submitError: undefined }
          : p,
      );
      try {
        const created = await createUploadJob(file.name, file.type || "video/mp4");
        await putUpload(created.uploadUrl, file);
        await confirmUpload(created.jobId, { startSec, endSec });
        setPhase({
          kind: "tracking",
          jobId: created.jobId,
          status: "pending",
          elapsedSec: 0,
          startedAtMs: Date.now(),
          file,
          startSec,
          endSec,
        });
      } catch (err) {
        // Confirm re-validates the segment (§13.2): map a known error_code to
        // friendly copy; else a calm fallback (never a raw code/status).
        const code =
          err instanceof ApiV2Error ? errorCodeFromBody(err.body) : undefined;
        const msg =
          (code && ERROR_CODE_COPY[code]) ??
          "Upload failed. Check your connection and try again.";
        setPhase((p) =>
          p.kind === "idle" ? { ...p, submitting: false, submitError: msg } : p,
        );
      } finally {
        inFlightRef.current = false;
      }
    },
    [],
  );

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    inFlightRef.current = false;
    setPhase({
      kind: "idle",
      file: null,
      fileDurationSec: null,
      startInput: "0",
      endInput: "0",
    });
  }, []);

  // ----- TRACKING: polling -----------------------------------------------

  // Hoisted, referentially-stable primitives so the polling effect depends on
  // exactly the job identity (jobId + startedAtMs). Per-tick status/elapsed
  // updates keep the same identity, so they must NOT restart polling.
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
        // a sustained streak; the timeout cap is the ultimate backstop.
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
          // Carry the local file + segment start from the tracking phase into
          // the result so the synced player can trim playback to the window.
          setPhase((p) =>
            p.kind === "tracking" && p.jobId === jobId
              ? {
                  kind: "result",
                  jobId,
                  activation,
                  file: p.file,
                  startSec: p.startSec,
                }
              : p,
          );
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
      // elapsedSec if it's monotonically newer; else compute from wall clock.
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
            : "See your brain on a video clip"}
        </h1>
        <p className="mt-3 max-w-[64ch] text-[14px] leading-relaxed text-ink-200">
          {phase.kind === "result"
            ? "Predicted average cortical response for your segment, rendered on the fsaverage5 surface — your clip and brain share one timeline below."
            : LIVE_INFERENCE_ENABLED
              ? `Upload a clip, choose up to ${MAX_SEGMENT_SEC} seconds, and watch the predicted cortical response light up the 3D brain beside your video.`
              : "Soon you'll upload a clip and watch the predicted cortical response light up the 3D brain. For now, explore real precomputed predictions in the gallery."}
        </p>
      </header>

      {phase.kind === "result" ? (
        <div className="mt-10">
          <LiveResultViewer
            file={phase.file}
            startOffsetSec={phase.startSec}
            activation={phase.activation}
            jobId={phase.jobId}
            onReset={reset}
          />
        </div>
      ) : (
        <section className="mt-10 grid gap-8 md:grid-cols-[minmax(0,1fr)_minmax(0,460px)]">
          <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas">
            <BrainMeshLazy activation={0} />
          </div>

          <div className="flex min-h-full flex-col">
            {phase.kind === "idle" &&
              (LIVE_INFERENCE_ENABLED ? (
                <IdlePanel
                  state={phase}
                  onChange={(next) => setPhase(next)}
                  onFileSelected={onFileSelected}
                  onPredict={startPrediction}
                />
              ) : (
                <ComingSoonPanel />
              ))}

            {phase.kind === "tracking" && <TrackingPanel state={phase} />}

            {phase.kind === "error" && (
              <ErrorPanel state={phase} onReset={reset} />
            )}
          </div>
        </section>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Idle panel — upload + segment picker
// ---------------------------------------------------------------------------

interface IdlePanelProps {
  state: IdleState;
  onChange: (next: IdleState) => void;
  onFileSelected: (file: File) => void;
  onPredict: (file: File, startSec: number, endSec: number) => void;
}

function IdlePanel({
  state,
  onChange,
  onFileSelected,
  onPredict,
}: IdlePanelProps) {
  const { file, fileDurationSec } = state;
  const reading = file !== null && fileDurationSec === null;
  const ready = file !== null && fileDurationSec !== null;

  const startSec = Number.parseFloat(state.startInput);
  const endSec = Number.parseFloat(state.endInput);
  // Base segment rules (start<end, ≤90s) + the file-length bound: End can't run
  // past the clip (the Space would reject it as segment_out_of_bounds).
  const baseErr = validateSegment(startSec, endSec);
  const overRun =
    ready && Number.isFinite(endSec) && endSec > (fileDurationSec ?? 0) + 1e-3;
  const segmentError: string | null =
    baseErr ?? (overRun ? "segment_out_of_bounds" : null);
  const canSubmit = ready && segmentError === null && !state.submitting;

  const onStartChange = (e: ChangeEvent<HTMLInputElement>) =>
    onChange({ ...state, startInput: e.target.value, submitError: undefined });
  const onEndChange = (e: ChangeEvent<HTMLInputElement>) =>
    onChange({ ...state, endInput: e.target.value, submitError: undefined });

  const clearFile = () =>
    onChange({
      ...state,
      file: null,
      fileDurationSec: null,
      fileError: undefined,
      submitError: undefined,
    });

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSubmit || !file) return;
    onPredict(file, startSec, endSec);
  };

  return (
    <div className="motion-fade-in flex h-full flex-col gap-6 border border-line bg-surface/40 p-6">
      {!ready ? (
        <>
          <p className="eyebrow">Upload a clip</p>
          <UploadDropzone disabled={reading} onFile={onFileSelected} prominent />
          {reading && (
            <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300">
              Reading video…
            </p>
          )}
          {state.fileError && (
            <p role="alert" className="text-[12px] text-accent">
              {state.fileError}
            </p>
          )}
          <div className="mt-auto text-[12px] leading-relaxed text-ink-300">
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
        <form onSubmit={onSubmit} className="flex h-full flex-col gap-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="eyebrow">Your clip</p>
              <p
                className="truncate font-mono text-[12px] text-ink-100"
                title={file?.name}
              >
                {file?.name}
              </p>
              <p className="font-mono text-[11px] tabular-nums text-ink-400">
                {(fileDurationSec ?? 0).toFixed(1)}s long
              </p>
            </div>
            <button
              type="button"
              onClick={clearFile}
              disabled={state.submitting}
              className="shrink-0 font-mono text-[11px] uppercase tracking-[0.08em] text-ink-300 transition-colors hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              change
            </button>
          </div>

          <fieldset className="space-y-2" disabled={state.submitting}>
            <legend className="eyebrow">
              Segment to analyze (≤{MAX_SEGMENT_SEC}s)
            </legend>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label
                  htmlFor="seg-start-sec"
                  className="block text-[11px] text-ink-300"
                >
                  Start (s)
                </label>
                <input
                  id="seg-start-sec"
                  type="number"
                  min={0}
                  max={fileDurationSec ?? undefined}
                  step={1}
                  inputMode="decimal"
                  value={state.startInput}
                  onChange={onStartChange}
                  aria-describedby="seg-hint"
                  className="mt-1 w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px] tabular-nums text-ink-100 focus-visible:border-accent focus-visible:outline-none"
                />
              </div>
              <div>
                <label
                  htmlFor="seg-end-sec"
                  className="block text-[11px] text-ink-300"
                >
                  End (s)
                </label>
                <input
                  id="seg-end-sec"
                  type="number"
                  min={0}
                  max={fileDurationSec ?? undefined}
                  step={1}
                  inputMode="decimal"
                  value={state.endInput}
                  onChange={onEndChange}
                  aria-describedby="seg-hint"
                  className="mt-1 w-full border border-line bg-canvas px-3 py-2 font-mono text-[13px] tabular-nums text-ink-100 focus-visible:border-accent focus-visible:outline-none"
                />
              </div>
            </div>
            <p id="seg-hint" className="font-mono text-[11px] text-ink-400">
              {segmentError ? (
                <span role="alert" className="text-accent">
                  {ERROR_CODE_COPY[segmentError]}
                </span>
              ) : (
                `${(endSec - startSec).toFixed(0)}s selected · up to ${MAX_SEGMENT_SEC}s of a ${(fileDurationSec ?? 0).toFixed(0)}s clip`
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
            {state.submitting ? "Submitting…" : "Predict"}
          </button>
          {state.submitError && (
            <p role="alert" className="text-[12px] text-accent">
              {state.submitError}
            </p>
          )}

          <div className="mt-auto text-[12px] leading-relaxed text-ink-400">
            We analyze only the window you pick — your file is uploaded over a
            one-time link and auto-deleted after a day.
          </div>
        </form>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Coming-soon panel (used only when LIVE_INFERENCE_ENABLED is false)
// ---------------------------------------------------------------------------

function ComingSoonPanel() {
  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-line bg-surface/40 p-6">
      <p className="eyebrow text-accent">Live prediction — coming soon</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">
        Upload-your-own-clip prediction is on the way
      </p>
      <p className="max-w-[44ch] text-[13px] leading-relaxed text-ink-200">
        Running TRIBE live on a clip you choose needs a GPU backend we&rsquo;re
        still wiring up. Meanwhile, every example in the gallery is a real,
        precomputed TRIBE prediction you can explore right now.
      </p>
      <Link
        href="/gallery"
        className="mt-auto inline-flex w-full items-center justify-center border border-accent bg-accent/10 px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-accent transition-colors hover:bg-accent/20"
      >
        Explore the gallery →
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tracking panel — honest stage stepper (L1)
//
// The raw job status barely moves (it sits on "downloading" for the whole
// multi-minute GPU run), so instead of a fake percentage we show: the real
// coarse phase as a stepper, a per-second elapsed clock (the clearest
// "not stuck" signal — and it ticks even under reduced motion), and an
// indeterminate bar for constant motion. We deliberately DON'T claim a
// sub-stage (transcribe / encode / predict) we can't observe from the browser
// — that's the future L3 work, fed by real progress pings from the Space.
// ---------------------------------------------------------------------------

const TRACK_STEPS: { key: string; label: string }[] = [
  { key: "uploaded", label: "Clip uploaded" },
  { key: "queued", label: "Queued" },
  { key: "running", label: "Running on the GPU" },
  { key: "result", label: "Result ready" },
];

// Map the raw job status onto a stepper index. downloading + inferring both
// just mean "the Space has it and is working" → the single "Running on the GPU"
// step (the status itself mislabels this whole phase as "downloading").
function trackStepIndex(status: JobStatus): number {
  if (status === "pending") return 1;
  if (status === "downloading" || status === "inferring") return 2;
  if (status === "done") return 3;
  return 2; // terminal failures render in ErrorPanel, not here
}

function formatElapsed(totalSec: number): string {
  const m = Math.floor(Math.max(0, totalSec) / 60);
  const s = Math.max(0, totalSec) % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function TrackingPanel({ state }: { state: TrackingState }) {
  // Per-second clock, independent of the 2s status poll, so the timer visibly
  // ticks every second. Driven by JS state (not a CSS animation), so it keeps
  // moving even under prefers-reduced-motion — the primary "not stuck" signal.
  const [nowMs, setNowMs] = useState(() => state.startedAtMs);
  useEffect(() => {
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const elapsedSec = Math.max(
    state.elapsedSec,
    Math.floor((nowMs - state.startedAtMs) / 1000),
  );

  const cur = trackStepIndex(state.status);
  const segLen = Math.max(0, state.endSec - state.startSec);

  return (
    <div className="motion-fade-in flex h-full flex-col gap-5 border border-line bg-surface/40 p-6">
      <div className="flex items-baseline justify-between gap-3">
        <p className="eyebrow">Job {shortId(state.jobId)}</p>
        <p
          className="font-mono text-[12px] tabular-nums text-ink-200"
          aria-label={`${elapsedSec} seconds elapsed`}
        >
          {formatElapsed(elapsedSec)}
        </p>
      </div>
      <p className="font-serif text-[18px] leading-tight text-ink-50">
        {cur <= 1
          ? "Getting your job in line…"
          : "Predicting your brain response…"}
      </p>

      <ol className="flex flex-col gap-3">
        {TRACK_STEPS.map((step, i) => {
          const stepState = i < cur ? "done" : i === cur ? "active" : "todo";
          const isRunning = step.key === "running" && stepState === "active";
          return (
            <li
              key={step.key}
              className="flex flex-col gap-2"
              aria-current={stepState === "active" ? "step" : undefined}
            >
              <div className="flex items-center gap-3">
                <StepDot state={stepState} />
                <span
                  className={[
                    "font-mono text-[12px]",
                    stepState === "done"
                      ? "text-ink-300"
                      : stepState === "active"
                        ? "text-ink-50"
                        : "text-ink-500",
                  ].join(" ")}
                >
                  {step.label}
                </span>
              </div>
              {isRunning && (
                <div className="ml-[27px] flex flex-col gap-2">
                  <div
                    role="progressbar"
                    aria-label="Working"
                    className="relative h-[3px] w-full overflow-hidden bg-line"
                  >
                    <div className="nm-indeterminate absolute inset-y-0 w-1/3 bg-accent" />
                  </div>
                  <p className="max-w-[40ch] text-[12px] leading-relaxed text-ink-400">
                    Transcribing the audio, encoding the video, and running TRIBE
                    on a real GPU. Usually a few minutes — the first run after the
                    model&rsquo;s been idle is the slowest.
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ol>

      <p className="mt-auto font-mono text-[11px] tabular-nums text-ink-500">
        {segLen.toFixed(0)}s segment · your clip + brain appear the moment it lands
      </p>
    </div>
  );
}

function StepDot({ state }: { state: "done" | "active" | "todo" }) {
  if (state === "done") {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-accent/50 bg-accent/15 text-accent">
        <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden>
          <path
            d="M1 4.2 L3 6.2 L7 1.6"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
    );
  }
  if (state === "active") {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-accent">
        <span className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
      </span>
    );
  }
  return (
    <span
      className="h-4 w-4 shrink-0 rounded-full border border-line"
      aria-hidden
    />
  );
}

// ---------------------------------------------------------------------------
// Error panel
// ---------------------------------------------------------------------------

function ErrorPanel({
  state,
  onReset,
}: {
  state: ErrorState;
  onReset: () => void;
}) {
  const isOutOfBounds = state.errorCode === "segment_out_of_bounds";
  const isRejectedDuration = state.errorKind === "rejected_duration";
  const isTimeout = state.errorKind === "timeout";
  const isNetwork = state.errorKind === "network";

  let heading = "Something went wrong";
  let body = "Something tripped up on our end. Start over and give it another go.";
  if (isOutOfBounds) {
    heading = "That window is past the end of the clip";
    body =
      "Your segment runs past where the clip ends. Pick an earlier window and try again.";
  } else if (isRejectedDuration) {
    heading = "Segment too long";
    body = `Segments longer than ${MAX_SEGMENT_SEC} seconds aren't supported. Pick a shorter window.`;
  } else if (state.errorKind === "failed_inference") {
    heading = "We couldn't finish that prediction";
    body =
      "The run didn't complete — usually a transient hiccup. Start over and try again.";
  } else if (state.errorKind === "failed_download") {
    heading = "We couldn't read that clip";
    body =
      "The upload didn't process. Re-pick the file (a standard H.264 MP4 works best) and try again.";
  } else if (isNetwork) {
    heading = "We lost the connection";
    body =
      "We couldn't reach the prediction service. Check your connection and try again.";
  } else if (isTimeout) {
    heading = "This is taking longer than expected";
    body =
      "We stopped checking after 10 minutes. The job may still finish — try again, or come back in a moment.";
  }

  return (
    <div className="motion-fade-in flex h-full flex-col gap-4 border border-accent/40 bg-surface/40 p-6">
      <p className="eyebrow text-accent">Error</p>
      <p className="font-serif text-[18px] leading-tight text-ink-50">{heading}</p>
      <p className="max-w-[44ch] text-[13px] leading-relaxed text-ink-200">
        {body}
      </p>
      <button
        type="button"
        onClick={onReset}
        className="mt-auto w-full border border-line px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-200 transition-colors hover:border-accent hover:text-accent"
      >
        Start over
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
      <span className="text-[13px] text-ink-100">or click to choose a file</span>
      <span className="font-mono text-[10px] text-ink-400">
        you&rsquo;ll pick a ≤{MAX_SEGMENT_SEC}s window next · MP4 preferred
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
