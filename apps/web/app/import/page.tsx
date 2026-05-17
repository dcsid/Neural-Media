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
  | { kind: "rejected"; message: string }
  | { kind: "starting"; filename: string }
  | { kind: "tracking"; job: ImportJob }
  | { kind: "complete"; job: ImportJob }
  | { kind: "failed"; job: ImportJob }
  | { kind: "offline"; url: string; message: string };

const POLL_INTERVAL_MS = 500;
const MAX_CONSECUTIVE_POLL_FAILURES = 5; // 2.5s of silence → flip to offline.
const COMPLETION_HOLD_MS = 1100; // Brief acknowledgement frame before redirect.

function isAcceptableFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return name.endsWith(".json") || name.endsWith(".zip");
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
      try {
        const job = await api.importStart(file);
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
          setPhase({
            kind: "rejected",
            message:
              err.kind === "http"
                ? `Server rejected the upload (HTTP ${err.status}).`
                : err.message,
          });
          return;
        }
        throw err;
      }
    },
    [startPolling],
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
          message: `${first.name} isn't a .json or .zip file. Drop your TikTok user_data.json or the export archive.`,
        });
        return;
      }
      void uploadFile(first);
    },
    [acceptingDrop, uploadFile],
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
        <code className="font-mono text-ink-100">user_data.json</code> file
        — or the entire <code className="font-mono text-ink-100">.zip</code>{" "}
        archive — onto the zone below. The pipeline runs entirely on this
        machine; nothing is uploaded anywhere else.
      </p>

      <div
        role="region"
        aria-label="Import drop zone"
        aria-disabled={!acceptingDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={clsx(
          "mt-10 flex min-h-[400px] flex-col items-center justify-center border px-8 text-center transition-colors duration-100",
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
            ? "user_data.json or the .zip archive. Drag-and-drop only."
            : phase.kind === "starting"
              ? `Sending ${phase.filename} to the local API.`
              : "Keep this tab open. You'll be redirected when the run completes."}
        </p>
      </div>

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
            python -m neural_media_pipeline.importer data/raw/user_data.json
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
      return <span className="text-accent">{phase.message}</span>;
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
