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
