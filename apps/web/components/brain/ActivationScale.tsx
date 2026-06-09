import { cividisCssStops } from "./lut";

interface ActivationScaleProps {
  // Optional axis labels. Defaults read as "Low activity ↔ High activity"
  // which matches the language the dashboard prose uses elsewhere.
  lowLabel?: string;
  highLabel?: string;
  // Orientation. Vertical bar (default) sits next to the brain mesh;
  // horizontal version is for narrow viewports / cards.
  orientation?: "vertical" | "horizontal";
  // When true, note that the colour scale is contrast-stretched per clip (the
  // mesh maps the clip's value range across the full ramp) rather than an
  // absolute 0..1. Default false. See lut.ts:computeDisplayRange.
  normalized?: boolean;
}

// Renders the cortical-mesh colormap as a labelled gradient bar so
// viewers can read "this region is hotter than that one" in concrete
// activation terms rather than guessing at the colour. Pulls CSS stops
// from the same LUT function the WebGL shader uses (`cividisCssStops`)
// — visually identical to the mesh surface by construction.
//
// Sequential warm-only ramp: dark red → orange → pale yellow. Matches
// the classic fMRI convention readers expect. Direction is honest:
// brighter = higher predicted activation. Absolute magnitude is
// uncalibrated (per scientific-framing.md); the bar deliberately omits
// numeric tick marks for that reason.
export function ActivationScale({
  lowLabel = "Low activity",
  highLabel = "High activity",
  orientation = "vertical",
  normalized = false,
}: ActivationScaleProps) {
  const stops = cividisCssStops(48);
  const gradient = `linear-gradient(${
    orientation === "vertical" ? "to top" : "to right"
  }, ${stops.join(", ")})`;
  const scaleLabel = normalized
    ? "Activation colour scale (normalized per clip)"
    : "Activation colour scale";

  if (orientation === "horizontal") {
    return (
      <div className="flex items-center gap-3 text-[10px] uppercase tracking-wider text-ink-300">
        <span>{lowLabel}</span>
        <div
          className="h-2 flex-1 rounded-sm"
          style={{ background: gradient }}
          aria-label={scaleLabel}
        />
        <span>{highLabel}</span>
        {normalized && (
          <span className="normal-case tracking-normal text-ink-400">
            normalized
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-[160px] flex-col items-center gap-2 text-[10px] uppercase tracking-wider text-ink-300">
      <span className="text-ink-200">{highLabel}</span>
      <div
        className="w-3 flex-1 rounded-sm"
        style={{ background: gradient }}
        aria-label={scaleLabel}
      />
      <span className="text-ink-200">{lowLabel}</span>
      {normalized && (
        <span className="normal-case tracking-normal text-ink-400">
          normalized
        </span>
      )}
    </div>
  );
}
