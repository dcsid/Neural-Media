"use client";

import { REGION_DESCRIPTIONS, type RegionId } from "@shared/types";

export interface RegionHoverInfo {
  regionId: RegionId;
  activation: number;
  // Cursor position relative to the BrainMesh container's bounding box.
  // BrainMesh translates the global pointer into container space before
  // handing it here so the tooltip can use absolute positioning.
  x: number;
  y: number;
}

interface RegionTooltipProps {
  hover: RegionHoverInfo | null;
}

// Small floating readout for hover state. Three lines max, tabular numerals,
// no editorialising — the value reported is exactly what the model predicted
// for that region, no rounding interpretation.

export function RegionTooltip({ hover }: RegionTooltipProps) {
  if (!hover) return null;

  // Nudge tooltip a few px off the cursor so it doesn't sit under the
  // pointer (which would block raycasting on the next frame).
  const style: React.CSSProperties = {
    left: hover.x + 14,
    top: hover.y + 14,
  };

  return (
    <div
      role="status"
      aria-live="polite"
      style={style}
      className={[
        "pointer-events-none absolute z-10",
        "min-w-[160px] border border-line bg-surface px-3 py-2",
        "text-[11px] leading-tight text-ink-100 shadow-sm",
      ].join(" ")}
    >
      <div className="font-mono uppercase tracking-[0.08em] text-ink-300">
        {hover.regionId}
      </div>
      <div className="mt-0.5 text-ink-100">
        {REGION_DESCRIPTIONS[hover.regionId]}
      </div>
      <div className="mt-1 font-mono tabular-nums text-accent">
        {hover.activation.toFixed(2)}
      </div>
    </div>
  );
}
