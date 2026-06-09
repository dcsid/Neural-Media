import { describe, expect, it } from "vitest";
import {
  IDENTITY_RANGE,
  MIN_DISPLAY_SPAN,
  cividisFill,
  cividisFillStretched,
  computeDisplayRange,
  stretch,
} from "@/components/brain/lut";

describe("computeDisplayRange", () => {
  it("stretches a narrow real-like band (~0.43–0.57) onto near the full range", () => {
    const vals = Array.from({ length: 200 }, (_, i) => 0.43 + (0.14 * i) / 199);
    const { lo, hi } = computeDisplayRange(vals);
    expect(lo).toBeGreaterThanOrEqual(0.42);
    expect(lo).toBeLessThan(0.46);
    expect(hi).toBeGreaterThan(0.54);
    expect(hi).toBeLessThanOrEqual(0.58);
    expect(hi - lo).toBeGreaterThanOrEqual(MIN_DISPLAY_SPAN);
  });

  it("ignores outliers via robust percentiles", () => {
    const vals = [0.0, ...Array.from({ length: 100 }, () => 0.5), 1.0];
    const { lo, hi } = computeDisplayRange(vals);
    // The lone 0/1 outliers sit outside the 2nd–98th band, so the range hugs
    // ~0.5 (widened to MIN span) rather than spanning all of [0,1].
    expect(lo).toBeGreaterThan(0.4);
    expect(hi).toBeLessThan(0.6);
  });

  it("does not over-stretch a model that already spans the range (~identity)", () => {
    const vals = Array.from({ length: 1000 }, (_, i) => i / 999); // uniform [0,1]
    const range = computeDisplayRange(vals);
    expect(range.hi - range.lo).toBeGreaterThan(0.9);
    expect(stretch(0.5, range)).toBeCloseTo(0.5, 1);
  });

  it("guards a flat clip to the minimum span (no noise amplification)", () => {
    const range = computeDisplayRange(Array.from({ length: 50 }, () => 0.5));
    expect(range.hi - range.lo).toBeGreaterThanOrEqual(MIN_DISPLAY_SPAN - 1e-9);
    expect(stretch(0.5, range)).toBeCloseTo(0.5, 5); // mid-scale, not saturated
  });

  it("returns identity for empty input", () => {
    expect(computeDisplayRange([])).toEqual(IDENTITY_RANGE);
  });

  it("clamps the range into [0,1]", () => {
    const { lo, hi } = computeDisplayRange([-0.5, 0.4, 0.5, 0.6, 1.5]);
    expect(lo).toBeGreaterThanOrEqual(0);
    expect(hi).toBeLessThanOrEqual(1);
  });
});

describe("stretch", () => {
  const range = { lo: 0.4, hi: 0.6 };

  it("maps the endpoints to 0 and 1", () => {
    expect(stretch(0.4, range)).toBeCloseTo(0, 6);
    expect(stretch(0.5, range)).toBeCloseTo(0.5, 6);
    expect(stretch(0.6, range)).toBeCloseTo(1, 6);
  });

  it("clamps values outside the range", () => {
    expect(stretch(0.2, range)).toBe(0);
    expect(stretch(0.9, range)).toBe(1);
  });

  it("is monotonic / order-preserving", () => {
    const ys = [0.41, 0.45, 0.5, 0.55, 0.59].map((x) => stretch(x, range));
    for (let i = 1; i < ys.length; i++) {
      expect(ys[i]).toBeGreaterThanOrEqual(ys[i - 1]);
    }
  });

  it("is the identity (clamp) under IDENTITY_RANGE", () => {
    expect(stretch(0.37, IDENTITY_RANGE)).toBeCloseTo(0.37, 6);
    expect(stretch(-1, IDENTITY_RANGE)).toBe(0);
    expect(stretch(2, IDENTITY_RANGE)).toBe(1);
  });
});

describe("cividisFillStretched", () => {
  it("equals cividisFill byte-for-byte under IDENTITY_RANGE", () => {
    const values = [0, 0.1, 0.25, 0.5, 0.73, 0.9, 1];
    const a = new Float32Array(values.length * 3);
    const b = new Float32Array(values.length * 3);
    cividisFill(values, a);
    cividisFillStretched(values, b, IDENTITY_RANGE);
    expect(Array.from(b)).toEqual(Array.from(a));
  });

  it("makes a near-identical narrow band visibly distinct", () => {
    const values = [0.45, 0.5, 0.55];
    const range = computeDisplayRange(
      Array.from({ length: 100 }, (_, i) => 0.45 + (0.1 * i) / 99),
    );
    const raw = new Float32Array(9);
    const stretched = new Float32Array(9);
    cividisFill(values, raw);
    cividisFillStretched(values, stretched, range);
    // Red-channel spread between first/last region is far larger after stretch.
    expect(Math.abs(stretched[0] - stretched[6])).toBeGreaterThan(
      Math.abs(raw[0] - raw[6]),
    );
  });
});
