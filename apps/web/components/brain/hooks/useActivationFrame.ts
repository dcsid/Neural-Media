"use client";

import { useMemo } from "react";
import { REGION_IDS, type RegionId } from "@shared/types";

// Resolves the per-region activation values to render at a given playhead.
//
// Two driving modes per the brain-viz brief:
//   - Hero (Dashboard): no keyframeVertices / timestamps; the global
//     `activation` scalar uniformly modulates every region.
//   - Detail (Video): `keyframeVertices` carries the per-region time series
//     (keyed by RegionId) and we linearly interpolate against `timestamps`
//     using `playheadSec`.
//
// The on-the-wire `keyframe_vertices` field is `Record<string, number[]>`.
// The exact key semantics are not fully nailed down in CONTRACTS.md §4; the
// two plausible shapes are:
//   (a) keys = RegionId, values = per-timepoint mean activation (parallel to
//       region_means but kept around because of vertex-aware downsampling).
//   (b) keys = stringified keyframe timestamps, values = per-vertex
//       activations at that keyframe.
// TODO(coord): confirm with ml-inference whether (a) or (b) is canonical.
// For now we accept either: if every key matches a RegionId we use shape (a);
// otherwise we collapse shape (b) keyframes to a single region series by
// averaging the vertex slice so the placeholder still renders sensibly.

export interface RegionFrame {
  byRegion: Record<RegionId, number>;
  // Coarse "overall" activation for the hero mode + as a fallback when only
  // a global scalar is available.
  global: number;
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

  // Binary search for the surrounding keyframes.
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

export function useActivationFrame(
  activation: number,
  keyframeVertices: Record<string, number[]> | undefined,
  timestamps: number[] | undefined,
  playheadSec: number | undefined,
): RegionFrame {
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
      return { byRegion, global: baseGlobal };
    }

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
      return { byRegion, global: n > 0 ? sum / n : baseGlobal };
    }

    // Shape (b): keys are stringified keyframe timestamps, values are
    // per-vertex slices. Collapse each slice to a single magnitude so the
    // placeholder mesh still has something to render; the real surface
    // will route these through the vertex-aware path instead.
    const entries = Object.entries(keyframeVertices)
      .map(([k, v]): [number, number] => {
        const t = Number(k);
        if (!Number.isFinite(t) || v.length === 0) {
          return [Number.NaN, 0];
        }
        let s = 0;
        for (let i = 0; i < v.length; i++) s += v[i];
        return [t, s / v.length];
      })
      .filter(([t]) => Number.isFinite(t))
      .sort((a, b) => a[0] - b[0]);

    if (entries.length === 0) {
      return { byRegion, global: baseGlobal };
    }
    const ts = entries.map(([t]) => t);
    const series = entries.map(([, v]) => v);
    const g = clamp01(interpolateSeries(series, ts, playheadSec));
    for (const r of REGION_IDS) byRegion[r] = g;
    return { byRegion, global: g };
  }, [activation, keyframeVertices, timestamps, playheadSec]);
}
