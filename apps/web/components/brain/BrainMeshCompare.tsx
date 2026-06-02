"use client";

// Side-by-side cortical-mesh comparison. Two BrainMesh panes share a single
// TimelineScrubber and a single hover state; the user picks a second video
// via a small <select> above the right pane.
//
// Ownership: brain-viz. frontend-dashboard wires this into /v/[id] (it
// fetches the videos list and decides what `videoIdB` is). We fetch the
// per-video activation and (when activation is 404'd) the region_metrics
// fallback ourselves so this component is fully self-contained.
//
// BrainMesh is loaded via next/dynamic with ssr:false to match the pattern
// in BrainDetailViewer.tsx — R3F's reconciler can't run server-side.

import dynamic from "next/dynamic";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError } from "@/lib/api";
import { videoTitle } from "@/lib/format";
import type {
  ActivationOutput,
  RegionId,
  RegionMetrics,
  VideoMetadata,
} from "@shared/types";
import { TimelineScrubber } from "./TimelineScrubber";
import type { CorticalHoverEvent } from "./CorticalSurface";
import type { BrainMeshProps } from "./BrainMesh";

const BrainMeshLazy = dynamic<BrainMeshProps>(
  () =>
    import("./BrainMesh").then((mod) => ({
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

export interface BrainMeshCompareProps {
  videoIdA: string;
  videoIdB: string | null;
  // The full list of videos the user can pick from for side B. Caller hands
  // it in (it's already fetched on the page).
  videos: VideoMetadata[];
  // Called when the user picks a different video for side B.
  onChangeB: (videoId: string | null) => void;
}

// Internal per-side fetch result. We pull both the activation and a
// metrics fallback in case the .npz was purged after aggregation.
interface SideData {
  status: "loading" | "ready" | "notFound" | "error";
  activation: ActivationOutput | null;
  // Set when activation came back as 404 (purged after aggregation). We
  // forward this into BrainMesh as `activationPurged`, which makes the
  // mesh fall back to the placeholder + banner.
  purged: boolean;
  // Region metrics, used both as a fallback when the activation is purged
  // (so the placeholder mesh still has per-region values) and as a source
  // of the per-region mean for the hero scalar.
  metrics: RegionMetrics[] | null;
  // Video metadata for the title above the pane.
  video: VideoMetadata | null;
  // Per-side total duration (seconds). Used to cap the playhead.
  durationS: number;
  // Last error message, surfaced in the empty/error state.
  errorMessage: string | null;
}

const INITIAL_SIDE: SideData = {
  status: "loading",
  activation: null,
  purged: false,
  metrics: null,
  video: null,
  durationS: 0,
  errorMessage: null,
};

function buildKeyframesFromMetrics(
  metrics: RegionMetrics[],
): Record<string, number[]> {
  // When per-vertex .npz is gone we still want the scrubber to advance
  // *something*. Reconstruct a keyframe_vertices map from the region
  // timeseries so BrainMesh's useActivationFrame hook can drive the
  // legend + region-level pulse. activationPurged=true still forces the
  // placeholder mesh; this only keeps the per-region readings live.
  //
  // Shape MUST be region-keyed (shape-a): keys are RegionIds, each value is
  // that region's mean-activation timeseries, resolved against the separate
  // `timestamps` array at render time. The earlier form keyed by timestamp
  // with length-(#regions) value arrays, which useActivationFrame
  // (reasonably) read as shape-b per-vertex slices — each "vertex" vector
  // was 8 long, so it collapsed to a single global mean and painted the
  // placeholder a flat uniform colour. Keying by region is what makes the
  // regions read distinctly.
  const out: Record<string, number[]> = {};
  for (const m of metrics) out[m.region_id] = m.timeseries;
  return out;
}

function meanOfRegionMetrics(metrics: RegionMetrics[]): number {
  if (metrics.length === 0) return 0;
  let sum = 0;
  for (const m of metrics) sum += m.mean;
  return sum / metrics.length;
}

function meanOfRegionMeans(region_means: Record<RegionId, number[]>): number {
  let sum = 0;
  let n = 0;
  for (const key in region_means) {
    const arr = region_means[key as RegionId];
    for (const v of arr) {
      sum += v;
      n += 1;
    }
  }
  return n > 0 ? sum / n : 0;
}

// Capped playhead — neither side runs past its own duration even when the
// shared scrubber has marched past it. Without this, a longer-duration
// video would drive a shorter one off the end of its own keyframes and
// pin the mesh at its final frame for the remainder of the scrub.
function capPlayhead(playhead: number, sideDuration: number): number {
  if (sideDuration <= 0) return 0;
  return Math.min(playhead, sideDuration);
}

interface PaneProps {
  side: "A" | "B";
  data: SideData;
  playheadSec: number;
  hover: CorticalHoverEvent | null;
  onHover: (e: CorticalHoverEvent | null) => void;
  headerExtra?: ReactNode;
}

function ComparePane({
  side,
  data,
  playheadSec,
  hover,
  onHover,
  headerExtra,
}: PaneProps) {
  const title = data.video ? videoTitle(data.video) : "—";
  const capped = capPlayhead(playheadSec, data.durationS);

  // When activation is present we drive the mesh from the real keyframes.
  // When it's purged, fall back to a metrics-derived keyframe map so the
  // scrubber still animates region-level pulses (BrainMesh renders the
  // placeholder anyway because activationPurged=true).
  const kv = useMemo(() => {
    if (data.activation) return data.activation.keyframe_vertices;
    if (data.purged && data.metrics && data.activation === null) {
      // Region-keyed (shape-a) fallback. The time axis is the synthesized
      // `ts` below; here we only need the per-region series. Bail when
      // there's nothing to drive, matching the `ts` guard so we never hand
      // BrainMesh a keyframe map with no timeline.
      if (data.metrics.length === 0 || data.durationS <= 0) return undefined;
      return buildKeyframesFromMetrics(data.metrics);
    }
    return undefined;
  }, [data.activation, data.purged, data.metrics, data.durationS]);

  const ts = useMemo(() => {
    if (data.activation) return data.activation.timestamps;
    if (data.purged && data.metrics) {
      const T = Math.max(...data.metrics.map((m) => m.timeseries.length), 0);
      if (T === 0 || data.durationS <= 0) return undefined;
      const out: number[] = new Array(T);
      for (let i = 0; i < T; i++) out[i] = (i / Math.max(T - 1, 1)) * data.durationS;
      return out;
    }
    return undefined;
  }, [data.activation, data.purged, data.metrics, data.durationS]);

  const meanActivation = useMemo(() => {
    if (data.activation) return meanOfRegionMeans(data.activation.region_means);
    if (data.metrics) return meanOfRegionMetrics(data.metrics);
    return 0;
  }, [data.activation, data.metrics]);

  return (
    <div className="flex flex-col">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">Side {side}</p>
          <h3 className="truncate font-serif text-[15px] leading-tight text-ink-50">
            {title}
          </h3>
        </div>
        {headerExtra}
      </div>
      <div className="relative aspect-[5/4] w-full overflow-hidden border border-line bg-canvas">
        {data.status === "loading" && (
          <BrainMeshLazy activation={0} hideLegend />
        )}
        {data.status === "ready" && (
          <BrainMeshLazy
            activation={meanActivation}
            keyframeVertices={kv}
            timestamps={ts}
            playheadSec={capped}
            activationPurged={data.purged}
            onHover={onHover}
            externalHover={hover}
          />
        )}
        {data.status === "notFound" && (
          <div className="absolute inset-0 flex items-center justify-center px-6 text-center">
            <p className="text-[12px] leading-relaxed text-ink-300">
              No activation data for this video yet.
            </p>
          </div>
        )}
        {data.status === "error" && (
          <div className="absolute inset-0 flex items-center justify-center px-6 text-center">
            <p className="text-[12px] leading-relaxed text-ink-300">
              {data.errorMessage ?? "Failed to load activation."}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyPane({
  picker,
}: {
  picker: ReactNode;
}) {
  return (
    <div className="flex flex-col">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">Side B</p>
          <h3 className="truncate font-serif text-[15px] leading-tight text-ink-300">
            Select a video to compare
          </h3>
        </div>
        {picker}
      </div>
      <div className="relative flex aspect-[5/4] w-full items-center justify-center overflow-hidden border border-dashed border-line bg-canvas/40">
        <p className="max-w-[28ch] px-6 text-center text-[12px] leading-relaxed text-ink-400">
          Pick a second video above to overlay its predicted cortical activation
          beside this one.
        </p>
      </div>
    </div>
  );
}

export function BrainMeshCompare({
  videoIdA,
  videoIdB,
  videos,
  onChangeB,
}: BrainMeshCompareProps) {
  const [sideA, setSideA] = useState<SideData>(INITIAL_SIDE);
  const [sideB, setSideB] = useState<SideData>(INITIAL_SIDE);
  const [playheadSec, setPlayheadSec] = useState(0);
  const [hoverA, setHoverA] = useState<CorticalHoverEvent | null>(null);
  const [hoverB, setHoverB] = useState<CorticalHoverEvent | null>(null);

  // Reset playhead whenever the active video pair changes. Mixing
  // playheads across video swaps produces a brief flash of the mesh at
  // the wrong frame.
  useEffect(() => {
    setPlayheadSec(0);
  }, [videoIdA, videoIdB]);

  // Reset hover state when either video changes, otherwise we hold onto a
  // stale vertex index that may not exist on the new mesh.
  useEffect(() => {
    setHoverA(null);
    setHoverB(null);
  }, [videoIdA, videoIdB]);

  // Generic fetch routine — pulls activation + metrics + video metadata in
  // parallel, normalizes the 404 (purged) case, and writes the resulting
  // SideData into the supplied setter.
  const loadSide = useCallback(
    (
      id: string | null,
      setter: (s: SideData) => void,
      signal: AbortSignal,
    ) => {
      if (!id) {
        setter({ ...INITIAL_SIDE, status: "ready" });
        return;
      }
      setter({ ...INITIAL_SIDE, status: "loading" });
      Promise.allSettled([
        api.videoActivation(id, { signal }),
        api.videoMetrics(id, { signal }),
        api.video(id, { signal }),
      ]).then(([actRes, metricsRes, videoRes]) => {
        if (signal.aborted) return;

        // Video metadata is the ground truth for duration; if even *that*
        // 404s, treat the whole side as not-found.
        if (videoRes.status === "rejected") {
          const err = videoRes.reason;
          if (err instanceof ApiError && err.kind === "http" && err.status === 404) {
            setter({
              ...INITIAL_SIDE,
              status: "notFound",
              errorMessage: "Video not found.",
            });
            return;
          }
          setter({
            ...INITIAL_SIDE,
            status: "error",
            errorMessage:
              err instanceof Error ? err.message : "Failed to load video.",
          });
          return;
        }
        const video = videoRes.value;

        let activation: ActivationOutput | null = null;
        let purged = false;
        let activationErr: ApiError | null = null;
        if (actRes.status === "fulfilled") {
          activation = actRes.value;
        } else {
          const err = actRes.reason;
          if (err instanceof ApiError && err.kind === "http" && err.status === 404) {
            purged = true;
          } else if (err instanceof ApiError) {
            activationErr = err;
          }
        }

        let metrics: RegionMetrics[] | null = null;
        if (metricsRes.status === "fulfilled") {
          metrics = metricsRes.value;
        }

        // No activation AND no metrics — nothing useful to render.
        if (!activation && (!metrics || metrics.length === 0)) {
          if (activationErr) {
            setter({
              ...INITIAL_SIDE,
              status: "error",
              video,
              durationS: video.duration_s,
              errorMessage: activationErr.message,
            });
            return;
          }
          setter({
            ...INITIAL_SIDE,
            status: "notFound",
            video,
            durationS: video.duration_s,
          });
          return;
        }

        setter({
          status: "ready",
          activation,
          purged,
          metrics,
          video,
          durationS: video.duration_s,
          errorMessage: null,
        });
      });
    },
    [],
  );

  // Independent AbortControllers per side so swapping B doesn't cancel A.
  const abortRefA = useRef<AbortController | null>(null);
  const abortRefB = useRef<AbortController | null>(null);

  useEffect(() => {
    abortRefA.current?.abort();
    const ctrl = new AbortController();
    abortRefA.current = ctrl;
    loadSide(videoIdA, setSideA, ctrl.signal);
    return () => ctrl.abort();
  }, [videoIdA, loadSide]);

  useEffect(() => {
    abortRefB.current?.abort();
    const ctrl = new AbortController();
    abortRefB.current = ctrl;
    loadSide(videoIdB, setSideB, ctrl.signal);
    return () => ctrl.abort();
  }, [videoIdB, loadSide]);

  // Shared scrubber span = max(durationA, durationB). Empty B contributes
  // zero so the scrubber matches A exactly while no comparison is loaded.
  const sharedDuration = Math.max(sideA.durationS, sideB.durationS);

  // Merged timeline points — union the timestamps from both sides so the
  // scrubber ticks reflect the densest available cadence.
  const sharedTimestamps = useMemo(() => {
    const a = sideA.activation?.timestamps ?? [];
    const b = sideB.activation?.timestamps ?? [];
    if (a.length === 0) return b;
    if (b.length === 0) return a;
    const merged = new Set<number>();
    for (const t of a) merged.add(t);
    for (const t of b) merged.add(t);
    return Array.from(merged).sort((x, y) => x - y);
  }, [sideA.activation, sideB.activation]);

  // Picker: list every video. The currently-active side-A video is in the
  // list but disabled so the user can see why they can't pick it.
  const picker = (
    <label className="inline-flex shrink-0 items-center gap-2 text-[11px] text-ink-300">
      <span className="eyebrow">Compare with…</span>
      <select
        value={videoIdB ?? ""}
        onChange={(e) => {
          const v = e.target.value;
          onChangeB(v === "" ? null : v);
        }}
        className={[
          "max-w-[180px] truncate border border-line bg-surface px-2 py-1",
          "font-mono text-[11px] text-ink-100 outline-none",
          "focus-visible:border-accent",
        ].join(" ")}
      >
        <option value="">Select…</option>
        {videos.map((v) => (
          <option key={v.id} value={v.id} disabled={v.id === videoIdA}>
            {videoTitle(v)}
            {v.id === videoIdA ? " (current)" : ""}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div>
      <div className="grid gap-6 md:grid-cols-2">
        <ComparePane
          side="A"
          data={sideA}
          playheadSec={playheadSec}
          hover={hoverB}
          onHover={setHoverA}
        />
        {videoIdB === null ? (
          <EmptyPane picker={picker} />
        ) : (
          <ComparePane
            side="B"
            data={sideB}
            playheadSec={playheadSec}
            hover={hoverA}
            onHover={setHoverB}
            headerExtra={picker}
          />
        )}
      </div>
      <div className="mt-4">
        <TimelineScrubber
          timestamps={sharedTimestamps}
          duration={sharedDuration}
          playheadSec={playheadSec}
          onSeek={setPlayheadSec}
        />
        <div className="mt-2 flex justify-between font-mono text-[10px] tabular-nums text-ink-400">
          <span>{playheadSec.toFixed(2)}s</span>
          <span>{sharedDuration.toFixed(2)}s</span>
        </div>
      </div>
    </div>
  );
}

export default BrainMeshCompare;
