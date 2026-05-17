"use client";

import { REGION_DESCRIPTIONS, type RegionId } from "@shared/types";

export interface RegionHoverInfo {
  // `null` when the hovered vertex sits in the unassigned medial-wall /
  // off-atlas territory (sentinel 255 in the .regions.bin convention).
  // The tooltip still renders so the user sees that a hover registered;
  // it just labels the spot as "(unassigned)" and omits the activation
  // readout (no calibrated value exists for those vertices).
  regionId: RegionId | null;
  activation: number;
  // Index into the 20,484-vertex fsaverage5 ordering (left hemisphere then
  // right). Surfaced so the user can tie a tooltip readout back to an
  // exported activation row by row number — useful for sanity-checking
  // against the .npz / Parquet file.
  vertexIndex: number;
  // Cursor position relative to the BrainMesh container's bounding box.
  // BrainMesh translates the global pointer into container space before
  // handing it here so the tooltip can use absolute positioning.
  x: number;
  y: number;
}

interface RegionTooltipProps {
  hover: RegionHoverInfo | null;
}

// Small floating readout for hover state. Four lines max, tabular numerals,
// no editorialising — the value reported is exactly what the model predicted
// for that vertex (or region mean when per-vertex data isn't available),
// rounded to 3 decimals. The unassigned-vertex branch swaps the activation
// row for an explanation rather than fabricating a number we don't have.

export function RegionTooltip({ hover }: RegionTooltipProps) {
  if (!hover) return null;

  // Nudge tooltip a few px off the cursor so it doesn't sit under the
  // pointer (which would block raycasting on the next frame).
  const style: React.CSSProperties = {
    left: hover.x + 14,
    top: hover.y + 14,
  };

  const unassigned = hover.regionId === null;
  const activationDisplay =
    Number.isFinite(hover.activation) ? hover.activation.toFixed(3) : "—";

  return (
    <div
      role="status"
      aria-live="polite"
      style={style}
      className={[
        "pointer-events-none absolute z-10",
        "min-w-[180px] border border-line bg-surface px-3 py-2",
        "text-[11px] leading-tight text-ink-100 shadow-sm",
      ].join(" ")}
    >
      <div className="font-mono uppercase tracking-[0.08em] text-ink-300">
        {unassigned ? "(unassigned)" : hover.regionId}
      </div>
      <div className="mt-0.5 text-ink-100">
        {unassigned
          ? "Outside the eight TRIBE regions"
          : REGION_DESCRIPTIONS[hover.regionId as RegionId]}
      </div>
      <div className="mt-1.5 flex items-baseline justify-between gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-400">
          vertex
        </span>
        <span className="font-mono tabular-nums text-ink-200">
          {hover.vertexIndex.toLocaleString("en-US")}
        </span>
      </div>
      {!unassigned && (
        <div className="mt-0.5 flex items-baseline justify-between gap-3">
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-400">
            activation
          </span>
          <span className="font-mono tabular-nums text-accent">
            {activationDisplay}
          </span>
        </div>
      )}
    </div>
  );
}
