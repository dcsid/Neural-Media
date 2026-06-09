// Warm-only "hot" sequential LUT — the classic fMRI activation palette:
// deep dark red → red → red-orange → orange-yellow → pale yellow-orange.
//
// Sequential and warm-only, so it satisfies docs/scientific-framing.md's
// constraint of no red↔green dichotomies (the only banned palette family
// for colour-blind accessibility). The previous default was cividis
// (perceptually uniform blue→yellow); we moved to this hot ramp because
// it matches the convention demo viewers expect for predicted BOLD
// activation maps — the "orange-red brain" look readers associate with
// fMRI. Function names below stay `cividisFill` etc. for caller
// stability; the implementation is the only thing that changed.
//
// Sixteen RGB stops on [0, 1]. Linearly interpolated between stops at
// sample time. Bounded fp32 in [0, 1] both per channel and per input —
// out-of-range inputs are clamped before lookup.

const STOPS: ReadonlyArray<readonly [number, number, number]> = [
  [0.060, 0.020, 0.020],
  [0.180, 0.030, 0.020],
  [0.310, 0.040, 0.020],
  [0.450, 0.050, 0.020],
  [0.600, 0.070, 0.030],
  [0.750, 0.110, 0.040],
  [0.870, 0.170, 0.050],
  [0.945, 0.250, 0.060],
  [0.985, 0.340, 0.080],
  [1.000, 0.440, 0.110],
  [1.000, 0.540, 0.150],
  [1.000, 0.640, 0.200],
  [1.000, 0.740, 0.270],
  [1.000, 0.830, 0.360],
  [1.000, 0.900, 0.500],
  [1.000, 0.960, 0.700],
];

// Perceptual gamma applied as `pow(t, GAMMA)` before the LUT lookup (see
// `cividis` below). With GAMMA < 1 the curve is concave: low inputs are
// pushed UP the ramp, so the dim 0.2–0.4 activations that dominate the mock
// predictions spread into visibly distinct reds instead of collapsing toward
// the near-black dark end. GAMMA = 1.0 is linear (no remap); GAMMA > 1 would
// do the opposite — crush low values toward black — which we don't want here.
//
// Meaningful range: (0, 1]. 0.85 is a deliberately mild correction chosen
// empirically against the mock activations — enough to separate the low-mid
// band without washing out the high end or distorting the relative ordering
// of activations (the map stays monotonic for any GAMMA > 0). Set to 1.0 to
// disable.
const GAMMA = 0.85;

export function cividis(t: number): [number, number, number] {
  const clamped = t < 0 ? 0 : t > 1 ? 1 : t;
  const adjusted = Math.pow(clamped, GAMMA);
  const u = adjusted * (STOPS.length - 1);
  const lo = Math.floor(u);
  const hi = Math.min(STOPS.length - 1, lo + 1);
  const f = u - lo;
  const a = STOPS[lo];
  const b = STOPS[hi];
  return [
    a[0] + (b[0] - a[0]) * f,
    a[1] + (b[1] - a[1]) * f,
    a[2] + (b[2] - a[2]) * f,
  ];
}

// Fills `out` (length === verts × 3) in-place. Avoids allocations in the hot
// path so the render loop can call this every frame without GC churn.
export function cividisFill(
  values: ArrayLike<number>,
  out: Float32Array,
): void {
  const n = values.length;
  for (let i = 0; i < n; i++) {
    const [r, g, b] = cividis(values[i]);
    const j = i * 3;
    out[j] = r;
    out[j + 1] = g;
    out[j + 2] = b;
  }
}

// CSS-formatted version of the same ramp at N evenly-spaced sample
// points. Used by ActivationScale to render the legend gradient in HTML
// so it visually matches the WebGL surface exactly.
export function cividisCssStops(samples = 32): string[] {
  const stops: string[] = [];
  for (let i = 0; i < samples; i++) {
    const t = i / (samples - 1);
    const [r, g, b] = cividis(t);
    stops.push(
      `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`,
    );
  }
  return stops;
}

// ---------------------------------------------------------------------------
// Display-time contrast stretch
// ---------------------------------------------------------------------------
//
// Real TRIBE predictions occupy a narrow band — after the sigmoid + region
// averaging the per-region means cluster within ~0.01 of 0.50 (≈0.43–0.57
// across a clip). Fed raw into the [0,1] LUT every region renders the same
// mid-tone and temporal change is invisible. We contrast-stretch at DISPLAY
// time only: map the clip's robust value range onto the full ramp.
//
// This is a monotonic, order-preserving display normalization — the standard
// brain-viz move. It never reorders regions or invents structure; the legend
// is relabelled with the absolute range the colours now span, and the numbers
// shown anywhere (tooltip, region readings) stay raw.

export interface DisplayRange {
  lo: number;
  hi: number;
}

// Identity range — no stretch (the single-scalar hero path uses this).
export const IDENTITY_RANGE: DisplayRange = { lo: 0, hi: 1 };

// Floor on the stretched span. Guards both ends: a near-constant clip can't be
// blown up so tiny noise fills the scale, and — paired with robust percentiles
// below — a model that already spans a wide range maps ~identity instead of
// being over-stretched.
export const MIN_DISPLAY_SPAN = 0.08;

function clamp01(x: number): number {
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

// Robust per-clip display range: the [lowPct, highPct] percentile band of the
// clip's values (default 2nd–98th — resistant to a few outlier vertices),
// clamped into [0, 1] and widened to MIN_DISPLAY_SPAN. Returns IDENTITY_RANGE
// for empty input.
export function computeDisplayRange(
  values: ArrayLike<number>,
  lowPct = 0.02,
  highPct = 0.98,
): DisplayRange {
  const n = values.length;
  if (n === 0) return { ...IDENTITY_RANGE };

  const sorted = Float64Array.from(values as ArrayLike<number>);
  sorted.sort();
  const at = (p: number): number => {
    let idx = Math.round(p * (n - 1));
    if (idx < 0) idx = 0;
    else if (idx > n - 1) idx = n - 1;
    return sorted[idx];
  };

  let lo = clamp01(at(lowPct));
  let hi = clamp01(at(highPct));
  if (hi < lo) {
    const t = lo;
    lo = hi;
    hi = t;
  }

  if (hi - lo < MIN_DISPLAY_SPAN) {
    // Widen symmetrically around the midpoint, then shift back inside [0, 1]
    // if the widening overran an edge. A truly flat clip collapses to
    // identity (uniform mid-tone) rather than amplifying floating-point noise.
    const mid = (lo + hi) / 2;
    lo = mid - MIN_DISPLAY_SPAN / 2;
    hi = mid + MIN_DISPLAY_SPAN / 2;
    if (lo < 0) {
      hi -= lo;
      lo = 0;
    }
    if (hi > 1) {
      lo -= hi - 1;
      hi = 1;
    }
    lo = clamp01(lo);
    hi = clamp01(hi);
    if (hi - lo < 1e-6) return { ...IDENTITY_RANGE };
  }

  return { lo, hi };
}

// Map a raw value into [0, 1] across the display range. Monotonic; clamped.
export function stretch(value: number, range: DisplayRange): number {
  const span = range.hi - range.lo;
  if (span <= 1e-6) return clamp01(value);
  return clamp01((value - range.lo) / span);
}

// cividis after a display stretch — used for the per-region legend swatches so
// they match the mesh.
export function cividisStretched(
  value: number,
  range: DisplayRange,
): [number, number, number] {
  return cividis(stretch(value, range));
}

// Like cividisFill, but contrast-stretches each value across `range` before
// the LUT. With IDENTITY_RANGE this is byte-for-byte identical to cividisFill.
// Allocation-free for the per-frame hot path.
export function cividisFillStretched(
  values: ArrayLike<number>,
  out: Float32Array,
  range: DisplayRange,
): void {
  const lo = range.lo;
  const span = range.hi - range.lo;
  const inv = span > 1e-6 ? 1 / span : 0;
  const n = values.length;
  for (let i = 0; i < n; i++) {
    const t = inv === 0 ? values[i] : (values[i] - lo) * inv;
    const [r, g, b] = cividis(t); // cividis clamps to [0,1] + applies gamma
    const j = i * 3;
    out[j] = r;
    out[j + 1] = g;
    out[j + 2] = b;
  }
}
