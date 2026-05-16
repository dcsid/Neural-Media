// Cividis sequential LUT — perceptually uniform, colour-blind safe.
// Required by docs/scientific-framing.md: no red↔green dichotomies.
//
// Nine RGB control points sampled from matplotlib's cividis (Nuñez, Anderton,
// Renslow 2018). Intermediate values are linearly interpolated. Nine stops is
// enough for smooth shading at 8-bit perceptual resolution; bumping this up
// gains no visible fidelity.

const STOPS: ReadonlyArray<readonly [number, number, number]> = [
  [0.0, 0.135, 0.305],
  [0.149, 0.196, 0.353],
  [0.265, 0.262, 0.345],
  [0.349, 0.337, 0.341],
  [0.443, 0.42, 0.348],
  [0.553, 0.51, 0.341],
  [0.671, 0.604, 0.318],
  [0.804, 0.706, 0.273],
  [0.996, 0.91, 0.176],
];

export function cividis(t: number): [number, number, number] {
  const u = Math.max(0, Math.min(1, t)) * (STOPS.length - 1);
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
