"use client";

import { useMemo, useRef } from "react";
import { NUM_VERTICES, REGION_IDS, type RegionId } from "@shared/types";

// Resolves the per-region (and optionally per-vertex) activation values to
// render at a given playhead.
//
// Two driving modes per the brain-viz brief:
//   - Hero (Dashboard): no keyframeVertices / timestamps; the global
//     `activation` scalar uniformly modulates every region.
//   - Detail (Video): `keyframeVertices` carries downsampled keyframes;
//     the renderer interpolates between them by `playheadSec`.
//
// The on-the-wire `keyframe_vertices` field is `Record<string, number[]>`.
// Confirmed against live ml-inference output (CONTRACTS.md §4): keys are
// stringified keyframe timestamps in seconds (e.g. "0.000", "14.000",
// "28.667", …), each value is a per-vertex slice of length NUM_VERTICES.
// This is shape (b) below. We still accept shape (a) for robustness in
// case a future producer keys by RegionId instead.
//
// Shapes:
//   (a) keys = RegionId, values = per-timepoint per-region mean.
//       Used as-is.
//   (b) keys = stringified keyframe timestamps, values = per-vertex
//       activations at that keyframe (length NUM_VERTICES).
//       The renderer:
//         - Linearly interpolates per-vertex between the two surrounding
//           keyframes → `perVertex` (Float32Array, length NUM_VERTICES).
//         - Reduces that vector through the region mask to a per-region
//           mean → `byRegion`. Region-mean is what the hover tooltip
//           reports, so the tooltip agrees with the readings table.
//       If no region mask is available (still fetching), falls back to a
//       single global mean and applies it uniformly to every region.

export interface RegionFrame {
  byRegion: Record<RegionId, number>;
  // Coarse "overall" activation for the hero mode + as a fallback when only
  // a global scalar is available.
  global: number;
  // Per-vertex activation, length === NUM_VERTICES. Only populated when
  // shape-(b) keyframe_vertices + valid timestamps + playheadSec are all
  // available. CorticalSurface uses this to colour the mesh per-vertex
  // and to look up the exact activation at the hovered vertex.
  perVertex: Float32Array | null;
}

function clamp01(x: number): number {
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

function isRegionKeyed(kv: Record<string, number[]>): boolean {
  const keys = Object.keys(kv);
  if (keys.length === 0) return false;
  const regionSet = new Set<string>(REGION_IDS);
  return keys.every((k) => regionSet.has(k));
}

function interpolateSeries(
  series: number[],
  timestamps: number[],
  playheadSec: number,
): number {
  if (series.length === 0) return 0;
  if (timestamps.length === 0) return series[0];
  if (playheadSec <= timestamps[0]) return series[0];
  const last = timestamps.length - 1;
  if (playheadSec >= timestamps[last]) return series[Math.min(last, series.length - 1)];

  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (timestamps[mid] <= playheadSec) lo = mid;
    else hi = mid;
  }
  const t0 = timestamps[lo];
  const t1 = timestamps[hi];
  const f = t1 === t0 ? 0 : (playheadSec - t0) / (t1 - t0);
  const a = series[lo] ?? 0;
  const b = series[Math.min(hi, series.length - 1)] ?? a;
  return a + (b - a) * f;
}

interface SortedKeyframes {
  ts: number[];          // sorted keyframe timestamps
  values: number[][];    // parallel per-vertex slices (length NUM_VERTICES each)
}

function sortKeyframes(
  kv: Record<string, number[]>,
): SortedKeyframes | null {
  const entries: [number, number[]][] = [];
  for (const [k, v] of Object.entries(kv)) {
    const t = Number(k);
    if (!Number.isFinite(t) || v.length === 0) continue;
    entries.push([t, v]);
  }
  if (entries.length === 0) return null;
  entries.sort((a, b) => a[0] - b[0]);
  return {
    ts: entries.map(([t]) => t),
    values: entries.map(([, v]) => v),
  };
}

export function useActivationFrame(
  activation: number,
  keyframeVertices: Record<string, number[]> | undefined,
  timestamps: number[] | undefined,
  playheadSec: number | undefined,
  regionMask: Uint8Array | null,
  regionOrder: ReadonlyArray<RegionId>,
): RegionFrame {
  // Scratch buffer for per-vertex output. Allocated once.
  const perVertexRef = useRef<Float32Array | null>(null);
  if (!perVertexRef.current) {
    perVertexRef.current = new Float32Array(NUM_VERTICES);
  }

  // Pre-sort keyframes when keyframeVertices identity changes. Sort is
  // O(K log K) on 5 keyframes — cheap, but doing it once per video is
  // cleaner than per-frame.
  const sorted = useMemo(() => {
    if (!keyframeVertices) return null;
    if (isRegionKeyed(keyframeVertices)) return null;
    return sortKeyframes(keyframeVertices);
  }, [keyframeVertices]);

  return useMemo(() => {
    const baseGlobal = clamp01(activation);
    const byRegion = Object.fromEntries(
      REGION_IDS.map((r) => [r, baseGlobal]),
    ) as Record<RegionId, number>;

    if (
      !keyframeVertices ||
      !timestamps ||
      timestamps.length === 0 ||
      playheadSec === undefined
    ) {
      return { byRegion, global: baseGlobal, perVertex: null };
    }

    // ----- Shape (a): region-keyed per-timepoint series. --------------
    if (isRegionKeyed(keyframeVertices)) {
      let sum = 0;
      let n = 0;
      for (const r of REGION_IDS) {
        const series = keyframeVertices[r];
        if (!series || series.length === 0) continue;
        const v = clamp01(interpolateSeries(series, timestamps, playheadSec));
        byRegion[r] = v;
        sum += v;
        n += 1;
      }
      return {
        byRegion,
        global: n > 0 ? sum / n : baseGlobal,
        perVertex: null,
      };
    }

    // ----- Shape (b): timestamp-keyed per-vertex keyframes. -----------
    if (!sorted) {
      return { byRegion, global: baseGlobal, perVertex: null };
    }
    const { ts, values } = sorted;

    // Locate the two surrounding keyframes and the lerp fraction.
    let lo = 0;
    let hi = ts.length - 1;
    if (playheadSec <= ts[0]) {
      lo = hi = 0;
    } else if (playheadSec >= ts[ts.length - 1]) {
      lo = hi = ts.length - 1;
    } else {
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (ts[mid] <= playheadSec) lo = mid;
        else hi = mid;
      }
    }
    const t0 = ts[lo];
    const t1 = ts[hi];
    const f = t1 === t0 ? 0 : (playheadSec - t0) / (t1 - t0);
    const k0 = values[lo];
    const k1 = values[hi];

    const perVertex = perVertexRef.current!;
    const vertCount = Math.min(perVertex.length, k0.length, k1.length);

    // Per-vertex linear interpolation. If the mask is available, also
    // accumulate per-region sums for byRegion. Otherwise compute a global
    // mean as a fallback.
    if (regionMask && regionMask.length === vertCount) {
      const regionSum = new Float64Array(regionOrder.length);
      const regionCount = new Uint32Array(regionOrder.length);
      for (let i = 0; i < vertCount; i++) {
        const v = clamp01(k0[i] + (k1[i] - k0[i]) * f);
        perVertex[i] = v;
        const ri = regionMask[i];
        if (ri < regionOrder.length) {
          regionSum[ri] += v;
          regionCount[ri] += 1;
        }
      }
      let gsum = 0;
      let gn = 0;
      for (let r = 0; r < regionOrder.length; r++) {
        const cnt = regionCount[r];
        const mean = cnt > 0 ? regionSum[r] / cnt : baseGlobal;
        byRegion[regionOrder[r]] = mean;
        gsum += mean * cnt;
        gn += cnt;
      }
      return {
        byRegion,
        global: gn > 0 ? gsum / gn : baseGlobal,
        perVertex,
      };
    }

    // No mask yet — fill per-vertex and emit a single global mean.
    let s = 0;
    for (let i = 0; i < vertCount; i++) {
      const v = clamp01(k0[i] + (k1[i] - k0[i]) * f);
      perVertex[i] = v;
      s += v;
    }
    const g = vertCount > 0 ? s / vertCount : baseGlobal;
    for (const r of REGION_IDS) byRegion[r] = g;
    return { byRegion, global: g, perVertex };
  }, [
    activation,
    keyframeVertices,
    timestamps,
    playheadSec,
    sorted,
    regionMask,
    regionOrder,
  ]);
}
