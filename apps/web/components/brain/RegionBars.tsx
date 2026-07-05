"use client";

import {
  REGION_IDS,
  REGION_DESCRIPTIONS,
  type RegionId,
} from "@shared/types";
import {
  cividisStretched,
  stretch,
  type DisplayRange,
} from "@/components/brain/lut";

// ---------------------------------------------------------------------------
// Region activation bars (live readout, off the canvas)
//
// Shared by the precomputed gallery (`app/page`) and the live single-video
// result (`app/predict` → LiveResultViewer): both render the same per-region bars
// under the synced video+brain, driven by the value at the current playhead.
// ---------------------------------------------------------------------------

// Linear interpolation of a per-region series at `t` (brain-seconds) against
// the dense `timestamps`. Mirrors useActivationFrame's interpolateSeries so the
// bars track exactly what the mesh shows.
export function valueAt(
  series: number[],
  timestamps: number[],
  t: number,
): number {
  if (series.length === 0) return 0;
  if (timestamps.length === 0) return series[0];
  if (t <= timestamps[0]) return series[0];
  const last = timestamps.length - 1;
  if (t >= timestamps[last]) return series[Math.min(last, series.length - 1)];
  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (timestamps[mid] <= t) lo = mid;
    else hi = mid;
  }
  const t0 = timestamps[lo];
  const t1 = timestamps[hi];
  const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
  const a = series[lo] ?? 0;
  const b = series[Math.min(hi, series.length - 1)] ?? a;
  return a + (b - a) * f;
}

export function RegionBars({
  byRegion,
  range,
}: {
  byRegion: Record<RegionId, number>;
  range: DisplayRange;
}) {
  const stretched = range.lo > 0 || range.hi < 1;
  return (
    <div className="border border-line bg-surface/40 p-4 sm:p-5">
      <div className="mb-3 flex items-baseline justify-between">
        <span className="eyebrow">Region activation</span>
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-400">
          {stretched ? "normalized" : "live"}
        </span>
      </div>
      <ul className="grid gap-x-8 gap-y-3 md:grid-cols-2">
        {REGION_IDS.map((r) => (
          <RegionBar key={r} region={r} value={byRegion[r] ?? 0} range={range} />
        ))}
      </ul>
    </div>
  );
}

function RegionBar({
  region,
  value,
  range,
}: {
  region: RegionId;
  value: number;
  range: DisplayRange;
}) {
  const fill = stretch(value, range); // 0..1 across the clip's range (dynamic)
  const [r, g, b] = cividisStretched(value, range);
  const color = `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
  return (
    <li className="flex items-center gap-3">
      <div className="w-[124px] shrink-0">
        <div className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-200">
          {region}
        </div>
        <div
          className="truncate text-[10px] leading-tight text-ink-500"
          title={REGION_DESCRIPTIONS[region]}
        >
          {REGION_DESCRIPTIONS[region]}
        </div>
      </div>
      <div className="relative h-2.5 min-w-0 flex-1 overflow-hidden rounded-sm bg-ink-900/60">
        <div
          aria-hidden
          className="absolute inset-y-0 left-0 rounded-sm transition-[width] duration-100 ease-out"
          style={{ width: `${(fill * 100).toFixed(1)}%`, backgroundColor: color }}
        />
      </div>
      <span className="w-[34px] shrink-0 text-right font-mono tabular-nums text-[11px] text-ink-100">
        {value.toFixed(2)}
      </span>
    </li>
  );
}
