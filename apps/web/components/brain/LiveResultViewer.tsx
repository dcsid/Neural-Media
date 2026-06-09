"use client";

import dynamic from "next/dynamic";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { ActivationPayload } from "@/lib/api-v2";
import { REGION_IDS, type RegionId } from "@shared/types";
import { BrainCanvasSkeleton } from "@/components/brain/BrainCanvasStates";
import { TimelineScrubber } from "@/components/brain/TimelineScrubber";
import { useReducedMotion } from "@/components/brain/hooks/useReducedMotion";
import { GalleryVideo } from "@/components/gallery/GalleryVideo";
import { RegionBars, valueAt } from "@/components/brain/RegionBars";
import {
  computeDisplayRange,
  IDENTITY_RANGE,
  type DisplayRange,
} from "@/components/brain/lut";

// Same SSR-off dynamic import as the gallery + hero — R3F touches browser
// globals at mount, so it can't enter the server render path.
const BrainMeshLazy = dynamic(
  () =>
    import("@/components/brain/BrainMesh").then((mod) => ({
      default: mod.BrainMesh,
    })),
  { ssr: false, loading: () => <BrainCanvasSkeleton /> },
);

export interface LiveResultViewerProps {
  /** The MP4 the user uploaded — played locally (never re-downloaded). */
  file: File;
  /**
   * Where the analyzed segment starts inside the file (seconds). The video
   * plays only `[startOffsetSec, startOffsetSec + activation.videoDurationSec)`
   * — the same window the Space trimmed to before running TRIBE.
   */
  startOffsetSec: number;
  activation: ActivationPayload;
  jobId: string;
  onReset: () => void;
}

// First-frame mean across regions → BrainMesh's scalar `activation` prop.
function meanFromActivation(a: ActivationPayload): number {
  let sum = 0;
  let count = 0;
  for (const series of Object.values(a.byRegion)) {
    if (series.length === 0) continue;
    sum += series[0] ?? 0;
    count += 1;
  }
  if (count === 0) return 0;
  return Math.max(0, Math.min(1, sum / count));
}

function shortId(id: string): string {
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}

// The live counterpart to the gallery's <Viewer>: same video-beside-brain +
// one-slider sync, but the video source is the user's local File and the
// playable window is offset into it by `startOffsetSec`. `videoSec` (0 ..
// durationSec, in *segment* seconds) is the single source of truth; the file's
// own currentTime is always `startOffsetSec + videoSec`.
export function LiveResultViewer({
  file,
  startOffsetSec,
  activation,
  jobId,
  onReset,
}: LiveResultViewerProps) {
  const objectUrl = useMemo(() => URL.createObjectURL(file), [file]);
  useEffect(() => () => URL.revokeObjectURL(objectUrl), [objectUrl]);

  const reduceMotion = useReducedMotion();

  // Segment length (= endSec - startSec). The Space reports it as the analyzed
  // duration, so the brain timeline and the trimmed video share this span.
  const durationSec = activation.videoDurationSec;
  const timestamps = activation.timestamps;
  const tsMax = timestamps.length > 0 ? timestamps[timestamps.length - 1] : 0;
  const segEnd = startOffsetSec + durationSec;

  // --- sync: `videoSec` (segment-seconds) is the single source of truth ------
  const [videoSec, setVideoSec] = useState(0);
  const [playing, setPlaying] = useState(false);
  // Seed the seek at the segment start so GalleryVideo opens at `startOffsetSec`
  // (its seek effect runs for nonce 0 on mount, parking it until metadata).
  const [seekReq, setSeekReq] = useState<{ sec: number; nonce: number }>({
    sec: startOffsetSec,
    nonce: 0,
  });
  const scrubbingRef = useRef(false);

  // Brain timeline overshoots the segment (fixed TR × N timepoints), so this is
  // deliberately NOT 1:1 — exactly the gallery's rescale.
  const brainPlayheadSec =
    durationSec > 0 ? (videoSec / durationSec) * tsMax : 0;
  const tickSeconds = useMemo(
    () =>
      tsMax > 0 ? timestamps.map((t) => (t * durationSec) / tsMax) : timestamps,
    [timestamps, durationSec, tsMax],
  );

  const displayRange = useMemo<DisplayRange>(() => {
    const all: number[] = [];
    for (const s of Object.values(activation.byRegion))
      for (const v of s) all.push(v);
    return all.length > 0 ? computeDisplayRange(all) : IDENTITY_RANGE;
  }, [activation]);
  const currentByRegion = useMemo(() => {
    const out = {} as Record<RegionId, number>;
    for (const r of REGION_IDS) {
      out[r] = valueAt(activation.byRegion[r] ?? [], timestamps, brainPlayheadSec);
    }
    return out;
  }, [activation, timestamps, brainPlayheadSec]);

  // Auto-play the muted clip on mount (unless reduced-motion). GalleryVideo
  // handles the autoplay-policy / reduced-motion fallback to tap-to-play.
  useEffect(() => {
    if (!reduceMotion) setPlaying(true);
  }, [reduceMotion]);

  // Native playback is the clock: map the file's currentTime back into segment
  // space, and stop the instant we reach the segment's end (the file itself is
  // longer, so GalleryVideo's own `ended` never fires for our window).
  const handleTime = useCallback(
    (fileSec: number) => {
      if (scrubbingRef.current) return;
      if (durationSec > 0 && fileSec >= segEnd - 1e-3) {
        setVideoSec(durationSec);
        setPlaying(false);
        return;
      }
      setVideoSec(Math.max(0, Math.min(durationSec, fileSec - startOffsetSec)));
    },
    [durationSec, startOffsetSec, segEnd],
  );

  // User scrub: drive videoSec + request a seek to the matching file time.
  const handleSeek = useCallback(
    (sec: number) => {
      setPlaying(false);
      const clamped = Math.max(0, Math.min(durationSec, sec));
      setVideoSec(clamped);
      setSeekReq((r) => ({ sec: startOffsetSec + clamped, nonce: r.nonce + 1 }));
    },
    [durationSec, startOffsetSec],
  );
  const handleScrubStart = useCallback(() => {
    scrubbingRef.current = true;
    setPlaying(false);
  }, []);
  const handleScrubEnd = useCallback(() => {
    scrubbingRef.current = false;
  }, []);

  const atEnd = durationSec > 0 && videoSec >= durationSec - 1e-3;
  const togglePlay = useCallback(() => {
    setPlaying((p) => {
      const next = !p;
      if (next && atEnd) {
        setVideoSec(0);
        setSeekReq((r) => ({ sec: startOffsetSec, nonce: r.nonce + 1 }));
      }
      return next;
    });
  }, [atEnd, startOffsetSec]);

  return (
    <section
      aria-labelledby="result-heading"
      className="motion-fade-in flex flex-col gap-4"
    >
      <h2 id="result-heading" className="sr-only">
        Your predicted brain
      </h2>

      {/* Video beside the brain on desktop, stacked on mobile — mirrors the
          gallery's synced viewer. */}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-black">
          <GalleryVideo
            src={objectUrl}
            playing={playing}
            seekRequest={seekReq}
            onTime={handleTime}
            onEnded={() => setPlaying(false)}
            muted
            className="h-full w-full object-cover"
          />
        </div>

        <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas">
          <BrainMeshLazy
            activation={meanFromActivation(activation)}
            keyframeVertices={activation.byRegion as Record<RegionId, number[]>}
            timestamps={activation.timestamps}
            playheadSec={brainPlayheadSec}
            hideLegend
          />
        </div>
      </div>

      {/* One shared transport drives both panes. TimelineScrubber's bundled
          play button is hidden ([&_button]) so this is the only control. */}
      {durationSec > 0 && (
        <div className="flex items-center gap-3">
          <button
            type="button"
            aria-label={playing ? "Pause" : "Play"}
            aria-pressed={playing}
            onClick={togglePlay}
            className="flex h-6 w-6 shrink-0 items-center justify-center border border-line bg-surface text-ink-200 transition-colors hover:border-accent hover:text-accent focus-visible:border-accent focus-visible:text-accent focus-visible:outline-none"
          >
            {playing ? (
              <svg width="9" height="10" viewBox="0 0 9 10" aria-hidden>
                <rect x="0.5" y="0.5" width="2.5" height="9" fill="currentColor" />
                <rect x="6" y="0.5" width="2.5" height="9" fill="currentColor" />
              </svg>
            ) : (
              <svg width="9" height="10" viewBox="0 0 9 10" aria-hidden>
                <path d="M1 0.5 L8.5 5 L1 9.5 Z" fill="currentColor" />
              </svg>
            )}
          </button>
          <div className="min-w-0 flex-1 [&_button]:hidden">
            <TimelineScrubber
              timestamps={tickSeconds}
              duration={durationSec}
              playheadSec={videoSec}
              onSeek={handleSeek}
              onScrubStart={handleScrubStart}
              onScrubEnd={handleScrubEnd}
            />
          </div>
        </div>
      )}

      <RegionBars byRegion={currentByRegion} range={displayRange} />

      <div className="flex flex-wrap items-center justify-between gap-3 border border-line bg-surface/40 px-4 py-3">
        <dl className="flex flex-wrap items-center gap-x-6 gap-y-1 text-[12px]">
          <div className="flex items-center gap-2">
            <dt className="text-ink-400">Result</dt>
            <dd className="font-mono text-ink-100">{shortId(jobId)}</dd>
          </div>
          <div className="flex items-center gap-2">
            <dt className="text-ink-400">Segment</dt>
            <dd className="font-mono tabular-nums text-ink-100">
              {durationSec.toFixed(1)}s
            </dd>
          </div>
          <div className="flex items-center gap-2">
            <dt className="text-ink-400">Timepoints</dt>
            <dd className="font-mono tabular-nums text-ink-100">
              {timestamps.length}
            </dd>
          </div>
          <div className="flex items-center gap-2">
            <dt className="text-ink-400">Model</dt>
            <dd className="font-mono text-ink-100">{activation.modelVersion}</dd>
          </div>
        </dl>
        <button
          type="button"
          onClick={onReset}
          className="border border-line px-4 py-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-200 transition-colors hover:border-accent hover:text-accent"
        >
          Try another
        </button>
      </div>

      <p className="text-[12px] leading-relaxed text-ink-400">
        Predicted average BOLD response across{" "}
        {Object.keys(activation.byRegion).length} regions of the fsaverage5
        cortical surface — not your individual brain.
      </p>
    </section>
  );
}
