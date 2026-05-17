"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import clsx from "clsx";
import { api, ApiError } from "@/lib/api";
import type { ImportJob } from "@shared/types";
import { ApiOfflineState } from "@/components/ApiOfflineState";

// /import — drag-and-drop only. There is no file picker, no
// <input type="file">, and no "or click to browse" fallback. The user
// drops a TikTok user_data.json or the original .zip onto the zone, the
// browser POSTs it to /api/v1/import as multipart, and this page polls
// the job until it terminates.

type Phase =
  | { kind: "idle" }
  | {
      kind: "rejected";
      message: string;
      // True when the server's 400 explicitly cites the [real] extra —
      // we surface a setup hint below the headline message.
      realModeSetup?: boolean;
    }
  | { kind: "starting"; filename: string }
  | { kind: "tracking"; job: ImportJob }
  | { kind: "complete"; job: ImportJob }
  | { kind: "failed"; job: ImportJob }
  | { kind: "offline"; url: string; message: string };

// Pull the FastAPI `detail` field off an HTTP error body when the server
// returned one. Used to surface real-mode setup errors verbatim instead
// of the useless "HTTP 400" shape.
function extractDetail(body: unknown): string | undefined {
  if (typeof body !== "object" || body === null) return undefined;
  const detail = (body as Record<string, unknown>).detail;
  return typeof detail === "string" ? detail : undefined;
}

const POLL_INTERVAL_MS = 500;
const MAX_CONSECUTIVE_POLL_FAILURES = 5; // 2.5s of silence → flip to offline.
const COMPLETION_HOLD_MS = 1100; // Brief acknowledgement frame before redirect.

function isAcceptableFile(file: File): boolean {
  const name = file.name.toLowerCase();
  // .txt covers the newer "Watch History.txt" export — a flat
  // Date:/Link: block format the backend importer auto-detects.
  return name.endsWith(".json") || name.endsWith(".zip") || name.endsWith(".txt");
}

// Time-window units the UI exposes. Wire format is always an ISO
// `since` timestamp computed from (amount, unit) at submit time — the
// backend doesn't need to know the unit. "minutes" is here because
// real-mode demos against the past hour or two are the realistic
// scale at which TRIBE v2 + yt-dlp on a home IP actually completes.
type WindowUnit = "minutes" | "hours" | "days";
const UNIT_TO_MS: Record<WindowUnit, number> = {
  minutes: 60 * 1000,
  hours: 60 * 60 * 1000,
  days: 24 * 60 * 60 * 1000,
};

// Default window. 14 days yields ~10k videos on the reference history —
// fine for mock mode, capped enough that real mode is at least
// approachable. 0 disables the window.
const DEFAULT_WINDOW_AMOUNT = 14;
const DEFAULT_WINDOW_UNIT: WindowUnit = "days";

type RunMode = "mock" | "real";

// Heuristic count of videos a typical TikTok history yields per window.
// Numbers from the integration lead's reference history; this is a hint,
// not a guarantee — real exports vary wildly. Keyed by total hours so we
// can interpolate across the (amount, unit) input.
const WINDOW_ESTIMATE_TABLE: Array<{ hours: number; videos: number }> = [
  { hours: 1, videos: 30 },
  { hours: 6, videos: 180 },
  { hours: 24, videos: 700 },
  { hours: 24 * 7, videos: 10_000 },
  { hours: 24 * 30, videos: 40_000 },
];

function estimateVideos(amount: number, unit: WindowUnit): number {
  const hours = (amount * UNIT_TO_MS[unit]) / UNIT_TO_MS.hours;
  if (hours <= 0) return 0;
  const table = WINDOW_ESTIMATE_TABLE;
  if (hours <= table[0].hours) {
    // Linear from 0 to first anchor — short windows scale roughly with time.
    return Math.max(1, Math.round((hours / table[0].hours) * table[0].videos));
  }
  if (hours >= table[table.length - 1].hours) {
    return table[table.length - 1].videos;
  }
  for (let i = 1; i < table.length; i += 1) {
    const lo = table[i - 1];
    const hi = table[i];
    if (hours <= hi.hours) {
      const t = (hours - lo.hours) / (hi.hours - lo.hours);
      return Math.round(lo.videos + t * (hi.videos - lo.videos));
    }
  }
  return table[table.length - 1].videos;
}

function formatEstimate(n: number): string {
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString();
}

function progressLabel(job: ImportJob): string {
  const { current, total, phase } = job.progress;
  const phaseSuffix = phase ? ` — ${phase}` : "";
  if (total == null || total <= 0) {
    return `Importing ${current}${phaseSuffix}`;
  }
  return `Importing ${current} / ${total}${phaseSuffix}`;
}

function statusVerb(status: ImportJob["status"]): string {
  switch (status) {
    case "queued":
      return "Queued";
    case "running":
      return "Running";
    case "complete":
      return "Complete";
    case "partial":
      return "Partial";
    case "failed":
      return "Failed";
  }
}

export default function ImportPage() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [dragOver, setDragOver] = useState(false);
  const [windowAmount, setWindowAmount] = useState<number>(DEFAULT_WINDOW_AMOUNT);
  const [windowUnit, setWindowUnit] = useState<WindowUnit>(DEFAULT_WINDOW_UNIT);
  const [mode, setMode] = useState<RunMode>("mock");
  // Track the active polling token so an in-flight loop from a
  // previous upload can't write into a fresh one.
  const pollTokenRef = useRef(0);

  const stopPolling = useCallback(() => {
    pollTokenRef.current += 1;
  }, []);

  const startPolling = useCallback(
    (jobId: string) => {
      const token = ++pollTokenRef.current;
      let consecutiveFailures = 0;

      const tick = async () => {
        if (token !== pollTokenRef.current) return;
        try {
          const job = await api.importJob(jobId);
          consecutiveFailures = 0;
          if (token !== pollTokenRef.current) return;
          if (job.status === "complete" || job.status === "partial") {
            // Hold on a brief acknowledgement frame so the user sees the
            // terminal "X / X" land before the route changes. "partial"
            // means some videos failed but at least one succeeded — show
            // the dashboard so the user can see what did process.
            stopPolling();
            setPhase({ kind: "complete", job });
            window.setTimeout(() => router.push("/"), COMPLETION_HOLD_MS);
            return;
          }
          if (job.status === "failed") {
            setPhase({ kind: "failed", job });
            stopPolling();
            return;
          }
          setPhase({ kind: "tracking", job });
          setTimeout(tick, POLL_INTERVAL_MS);
        } catch (err) {
          consecutiveFailures += 1;
          if (err instanceof ApiError && consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
            stopPolling();
            setPhase({ kind: "offline", url: err.url, message: err.message });
            return;
          }
          setTimeout(tick, POLL_INTERVAL_MS);
        }
      };

      tick();
    },
    [router, stopPolling],
  );

  // Cancel any pending poll on unmount.
  useEffect(() => stopPolling, [stopPolling]);

  const uploadFile = useCallback(
    async (file: File) => {
      setPhase({ kind: "starting", filename: file.name });
      // Resolve the window to an ISO `since`. Amount of 0 (or any
      // non-positive value) disables the filter — the backend treats
      // a missing `since` the same as "import everything".
      const since =
        windowAmount > 0
          ? new Date(Date.now() - windowAmount * UNIT_TO_MS[windowUnit]).toISOString()
          : undefined;
      try {
        const job = await api.importStart(file, { since, mode });
        startPolling(job.id);
      } catch (err) {
        if (err instanceof ApiError) {
          // 409 — one already running. The body is the running job; pick
          // up its id and keep polling from there.
          if (err.status === 409 && isImportJob(err.body)) {
            startPolling((err.body as ImportJob).id);
            return;
          }
          if (err.kind === "offline") {
            setPhase({ kind: "offline", url: err.url, message: err.message });
            return;
          }
          // 400 / 422: FastAPI puts the human-readable reason on `detail`.
          // Surface it verbatim — the most common 400 today is
          // "real mode requires the [real] inference extra".
          const detail = extractDetail(err.body);
          const cited = detail?.toLowerCase() ?? "";
          const isRealModeSetup =
            mode === "real" &&
            err.status === 400 &&
            (cited.includes("real") || cited.includes("[real]") || cited.includes("license"));
          setPhase({
            kind: "rejected",
            message:
              err.kind === "http"
                ? detail ?? `Server rejected the upload (HTTP ${err.status}).`
                : err.message,
            realModeSetup: isRealModeSetup,
          });
          return;
        }
        throw err;
      }
    },
    [startPolling, windowAmount, windowUnit, mode],
  );

  const acceptingDrop =
    phase.kind === "idle" || phase.kind === "rejected" || phase.kind === "failed";

  const onDragOver = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!acceptingDrop) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      setDragOver(true);
    },
    [acceptingDrop],
  );

  const onDragLeave = useCallback(() => {
    setDragOver(false);
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!acceptingDrop) return;
      e.preventDefault();
      setDragOver(false);
      const files = e.dataTransfer.files;
      if (!files || files.length === 0) return;
      const first = files[0];
      if (!isAcceptableFile(first)) {
        setPhase({
          kind: "rejected",
          message: `${first.name} isn't a .json, .txt, or .zip file. Drop your TikTok user_data.json, Watch History.txt, or the export archive.`,
        });
        return;
      }
      void uploadFile(first);
    },
    [acceptingDrop, uploadFile],
  );

  const onWindowAmountChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const parsed = Number.parseInt(e.target.value, 10);
      // NaN → fall through to 0 (means "no window"); negative clamped
      // to 0 too. uploadFile treats <=0 as "send no since".
      setWindowAmount(Number.isFinite(parsed) ? Math.max(0, parsed) : 0);
    },
    [],
  );

  const onWindowUnitChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const value = e.target.value;
      if (value === "minutes" || value === "hours" || value === "days") {
        setWindowUnit(value);
      }
    },
    [],
  );

  const onModeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const value = e.target.value;
      if (value === "mock" || value === "real") setMode(value);
    },
    [],
  );

  if (phase.kind === "offline") {
    return <ApiOfflineState url={phase.url} message={phase.message} />;
  }

  return (
    <main className="mx-auto max-w-[1280px] px-8 pb-10 pt-12">
      <p className="eyebrow mb-4">Import</p>
      <h1 className="font-serif text-[40px] leading-[1.1] tracking-tightish text-ink-50">
        Drop your TikTok export to begin.
      </h1>
      <p className="mt-5 max-w-[60ch] text-[14px] leading-relaxed text-ink-200">
        Request your archive from{" "}
        <a
          href="https://www.tiktok.com/setting/download-your-data"
          rel="noreferrer noopener"
          target="_blank"
          className="text-ink-100 underline underline-offset-2 hover:text-accent focus:text-accent"
        >
          tiktok.com/setting/download-your-data
        </a>
        . When it arrives, drop the{" "}
        <code className="font-mono text-ink-100">user_data.json</code>,{" "}
        the newer <code className="font-mono text-ink-100">Watch History.txt</code>,
        or the entire <code className="font-mono text-ink-100">.zip</code>{" "}
        archive onto the zone below. The pipeline runs entirely on this
        machine; nothing is uploaded anywhere else.
      </p>

      <div className="mt-8 grid gap-x-6 gap-y-4 sm:grid-cols-[auto_1fr]">
        <div className="text-[12px] uppercase tracking-wider text-ink-400 sm:pt-2">
          Window
        </div>
        <div className="flex flex-wrap items-center gap-3 text-[12px] text-ink-300">
          <label htmlFor="window-amount" className="text-ink-200">
            Last
          </label>
          <input
            id="window-amount"
            type="number"
            min={0}
            step={1}
            value={windowAmount}
            onChange={onWindowAmountChange}
            disabled={!acceptingDrop}
            className="w-20 border border-line bg-canvas px-2 py-1 text-center font-mono tabular-nums text-ink-100 focus:border-accent focus:outline-none disabled:opacity-50"
            aria-describedby="window-hint"
          />
          <select
            id="window-unit"
            value={windowUnit}
            onChange={onWindowUnitChange}
            disabled={!acceptingDrop}
            className="border border-line bg-canvas px-2 py-1 text-ink-100 focus:border-accent focus:outline-none disabled:opacity-50"
            aria-label="Window unit"
          >
            <option value="minutes">minutes</option>
            <option value="hours">hours</option>
            <option value="days">days</option>
          </select>
          <span className="text-ink-200">of history.</span>
          <span id="window-hint" className="basis-full text-ink-400 sm:basis-auto">
            Set 0 to import everything. Real mode runs TRIBE v2 once per video; keep this small.
          </span>
        </div>

        <div className="text-[12px] uppercase tracking-wider text-ink-400 sm:pt-2">
          Mode
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-ink-300">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="mode"
              value="mock"
              checked={mode === "mock"}
              onChange={onModeChange}
              disabled={!acceptingDrop}
              className="accent-accent disabled:opacity-50"
            />
            <span className="text-ink-100">Mock</span>
            <span className="text-ink-400">fast, synthetic outputs (no download, no GPU)</span>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="mode"
              value="real"
              checked={mode === "real"}
              onChange={onModeChange}
              disabled={!acceptingDrop}
              className="accent-accent disabled:opacity-50"
            />
            <span className="text-ink-100">Real</span>
            <span className="text-ink-400">
              yt-dlp + ffmpeg + TRIBE v2 (needs GPU + <code className="font-mono">[real]</code> install + accepted licenses)
            </span>
          </label>
        </div>
      </div>

      <div
        role="region"
        aria-label="Import drop zone"
        aria-disabled={!acceptingDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={clsx(
          "mt-6 flex min-h-[400px] flex-col items-center justify-center border px-8 text-center transition-colors duration-100",
          dragOver && acceptingDrop
            ? "border-accent text-ink-50"
            : "border-line text-ink-300",
          !acceptingDrop && "opacity-70",
        )}
      >
        <p className="font-serif text-[24px] tracking-tightish text-ink-100">
          {acceptingDrop
            ? "Drop file here"
            : phase.kind === "starting"
              ? "Uploading"
              : "Importing"}
        </p>
        <p className="mt-3 max-w-[44ch] text-[12px] leading-relaxed">
          {acceptingDrop
            ? "user_data.json, Watch History.txt, or the .zip archive. Drag-and-drop only."
            : phase.kind === "starting"
              ? `Sending ${phase.filename} to the local API.`
              : "Keep this tab open. You'll be redirected when the run completes."}
        </p>
      </div>

      {/* Pre-submission hint. Static lookup; presented as a soft
          estimate rather than a guarantee. Hides when the window is
          set to "everything" (amount = 0). */}
      {acceptingDrop && windowAmount > 0 && (
        <p className="mt-3 text-[11px] text-ink-400">
          ≈ <span className="font-mono tabular-nums text-ink-200">
            {formatEstimate(estimateVideos(windowAmount, windowUnit))}
          </span>{" "}
          videos in the selected window on a typical history. Real exports vary.
        </p>
      )}

      <div
        className="mt-6 min-h-[44px] border-t border-line pt-4 font-mono text-[12px] tabular-nums"
        role="status"
        aria-live="polite"
      >
        <StatusLine phase={phase} />
      </div>

      <section className="mt-12 border-t border-line pt-6 text-[11px] leading-relaxed text-ink-400">
        <p className="eyebrow mb-2">Prefer the CLI?</p>
        <p>
          Power users can skip this page and run the importer directly:{" "}
          <code className="font-mono text-ink-200">
            python -m neural_media_pipeline path/to/Watch&nbsp;History.txt --mock --days 1 --purge-after-inference
          </code>
          . The same pipeline runs either way.
        </p>
      </section>

      <AnimatePresence>
        {phase.kind === "complete" && (
          <CompletionOverlay job={phase.job} />
        )}
      </AnimatePresence>
    </main>
  );
}

function CompletionOverlay({ job }: { job: ImportJob }) {
  const { current, total } = job.progress;
  const label =
    total != null && total > 0
      ? `Imported ${current} / ${total}`
      : `Imported ${current}`;

  return (
    <motion.div
      // Fixed-position fade-in panel between the terminal poll frame and
      // router.push. ~250ms in, holds for ~600ms, ~200ms out as the route
      // changes underneath. Honest acknowledgement — no celebration.
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.25, ease: "easeOut" }}
      className="fixed inset-0 z-40 flex items-center justify-center bg-canvas/85 backdrop-blur-[2px]"
      role="status"
      aria-live="polite"
    >
      <div className="text-center">
        <p className="eyebrow mb-3">Import complete</p>
        <p className="font-serif text-[26px] tracking-tightish text-ink-50">
          <span data-num>{label}</span>{" "}
          <span className="text-ink-300">— opening dashboard</span>
        </p>
      </div>
    </motion.div>
  );
}

function StatusLine({ phase }: { phase: Phase }) {
  switch (phase.kind) {
    case "idle":
      return (
        <span className="text-ink-400">
          Awaiting a dropped file.
        </span>
      );
    case "rejected":
      return (
        <span className="block text-accent">
          {phase.message}
          {phase.realModeSetup && (
            <span className="mt-1 block font-sans text-[11px] normal-case tracking-normal text-ink-300">
              Install with{" "}
              <code className="font-mono text-ink-200">
                pip install -e &apos;services/inference[real]&apos;
              </code>
              {" "}— see{" "}
              <span className="text-ink-200">README → &ldquo;Real TRIBE v2 (GPU required)&rdquo;</span>
              . Or switch to Mock mode above for a demo run.
            </span>
          )}
        </span>
      );
    case "starting":
      return (
        <span className="text-ink-200">
          Uploading {phase.filename} —{" "}
          <span className="text-ink-400">contacting the API</span>
        </span>
      );
    case "tracking":
      return (
        <span className="text-ink-100">
          {progressLabel(phase.job)}{" "}
          <span className="text-ink-400">
            ({statusVerb(phase.job.status).toLowerCase()})
          </span>
        </span>
      );
    case "failed":
      return (
        <span className="text-accent">
          Import failed: {phase.job.error ?? "no error message provided"}.
          Drop another file to retry.
        </span>
      );
    case "offline":
      // Handled by the early return above.
      return null;
  }
}

function isImportJob(value: unknown): value is ImportJob {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.status === "string" &&
    ["queued", "running", "complete", "partial", "failed"].includes(
      candidate.status as string,
    )
  );
}
