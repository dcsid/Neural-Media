// Cividis sequential LUT — perceptually uniform, colour-blind safe.
// Required by docs/scientific-framing.md: no red↔green dichotomies.
//
// Sixteen RGB control points sampled from matplotlib's cividis (Nuñez,
// Anderton, Renslow 2018) at evenly-spaced fractional positions. The
// denser sampling versus the original 9-stop table yields visibly smoother
// shading on the cortical mesh's gentle gradients and tightens the
// midpoint band where the cool→warm transition lives.
//
// A mild gamma (`< 1`) is applied before the lookup so the low end of the
// activation range stretches into more of the LUT's darker stops, making
// modest activations more readable on dark backgrounds without crushing
// the high end. This keeps the colormap "honest" — order is preserved,
// no values are clipped, only the perceptual emphasis shifts.

const STOPS: ReadonlyArray<readonly [number, number, number]> = [
  [0.000, 0.135, 0.305],
  [0.012, 0.167, 0.336],
  [0.073, 0.196, 0.353],
  [0.150, 0.218, 0.354],
  [0.224, 0.246, 0.350],
  [0.292, 0.273, 0.346],
  [0.357, 0.302, 0.342],
  [0.421, 0.331, 0.340],
  [0.486, 0.361, 0.341],
  [0.553, 0.394, 0.341],
  [0.622, 0.428, 0.336],
  [0.692, 0.465, 0.326],
  [0.764, 0.504, 0.310],
  [0.836, 0.547, 0.287],
  [0.907, 0.594, 0.252],
  [0.996, 0.910, 0.176],
];

// Slight perceptual stretch toward the dark end. 0.85 chosen empirically
// against the mock activations — keeps the brightest peaks at full LUT
// luminance while making 0.2–0.4 values visibly distinguishable from 0.
// Set to 1.0 to disable. Bounds intentionally permissive — values are
// clamped to [0, 1] before the lookup so out-of-range inputs degrade
// gracefully.
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
