"use client";

import { REGION_DESCRIPTIONS, REGION_IDS, type RegionId } from "@shared/types";
import {
  cividis,
  cividisStretched,
  IDENTITY_RANGE,
  type DisplayRange,
} from "./lut";

// Corner overlay legend for the brain mesh. Cividis is a sequential LUT —
// regions don't have intrinsic colors, they share one ramp keyed by
// activation. So each swatch reflects the region's CURRENT mean activation
// (driven by useActivationFrame.byRegion), and a small ramp at the bottom
// keys the 0..1 → color mapping. That way the legend tells two stories at
// once: which region sits where (via the description) and how loud it is
// right now (via the swatch).
//
// Hover handling: hovering a row highlights it; we don't fire a back-pointer
// into the mesh because regions are partitions of the cortical surface and
// "hover this region" doesn't map cleanly to a single vertex. The mesh's
// own RegionTooltip handles the inverse direction.

interface RegionLegendProps {
  // Per-region mean activation values, same shape as
  // useActivationFrame's `byRegion` output. Required so the swatches are
  // informative; pass a zero-filled map for the dashboard hero where there
  // is no per-region resolution.
  byRegion: Record<RegionId, number>;
  // Clip-wide display range the mesh colours are stretched across. Swatches use
  // it so they match the surface, and the ramp is relabelled with it. The
  // per-region NUMBERS shown stay raw. Defaults to identity (no stretch).
  range?: DisplayRange;
}

function swatchStyle(value: number, range: DisplayRange): React.CSSProperties {
  const [r, g, b] = cividisStretched(value, range);
  return {
    backgroundColor: `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`,
  };
}

function rampStops(n: number): string {
  const stops: string[] = [];
  for (let i = 0; i < n; i++) {
    const v = i / (n - 1);
    const [r, g, b] = cividis(v);
    const pct = (v * 100).toFixed(1);
    stops.push(`rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}) ${pct}%`);
  }
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

// Computed once. 9 stops is more than enough for an 8-bit CSS gradient.
const RAMP_GRADIENT = rampStops(9);

export function RegionLegend({
  byRegion,
  range = IDENTITY_RANGE,
}: RegionLegendProps) {
  // Colours are contrast-stretched whenever the clip's display range is
  // narrower than the full [0,1] — relabel the ramp to say so.
  const stretched = range.lo > 0 || range.hi < 1;
  return (
    <div
      role="figure"
      aria-label="Cortical region legend"
      className={[
        "pointer-events-auto absolute right-3 top-3 z-10",
        // Narrower on phones so it doesn't blanket a small canvas.
        "w-[170px] border border-line bg-surface/95 px-3 py-2.5 backdrop-blur-sm sm:w-[200px]",
        "text-[11px] leading-tight text-ink-100 shadow-sm",
      ].join(" ")}
    >
      <div className="mb-1.5 font-mono uppercase tracking-[0.08em] text-ink-300">
        Regions
      </div>
      <ul className="space-y-1.5">
        {REGION_IDS.map((r) => (
          <RegionRow
            key={r}
            region={r}
            value={byRegion[r] ?? 0}
            range={range}
          />
        ))}
      </ul>

      <div className="mt-2.5 border-t border-line/70 pt-2">
        <div
          aria-hidden
          className="h-1.5 w-full"
          style={{ backgroundImage: RAMP_GRADIENT }}
        />
        <div className="mt-1 flex justify-between font-mono text-[9px] tabular-nums text-ink-400">
          <span>{range.lo.toFixed(2)}</span>
          <span className="tracking-normal">
            {stretched ? "normalized" : "activation"}
          </span>
          <span>{range.hi.toFixed(2)}</span>
        </div>
      </div>
    </div>
  );
}

interface RegionRowProps {
  region: RegionId;
  value: number;
  range: DisplayRange;
}

function RegionRow({ region, value, range }: RegionRowProps) {
  return (
    <li className="flex items-center gap-2">
      <span
        aria-hidden
        className="h-2.5 w-2.5 shrink-0 border border-line/60"
        style={swatchStyle(value, range)}
      />
      <span className="min-w-[44px] font-mono text-[10px] uppercase tracking-[0.06em] text-ink-200">
        {region}
      </span>
      <span className="flex-1 truncate text-[10.5px] text-ink-400" title={REGION_DESCRIPTIONS[region]}>
        {REGION_DESCRIPTIONS[region]}
      </span>
      <span className="font-mono tabular-nums text-[10px] text-ink-200">
        {value.toFixed(2)}
      </span>
    </li>
  );
}
