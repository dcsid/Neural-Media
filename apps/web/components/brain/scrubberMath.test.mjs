// Unit tests for the pure TimelineScrubber math (scrubberMath.ts).
//
// Runner: Node's built-in `node:test` — zero extra dependencies, so this runs
// without touching apps/web/package.json (adding a web test runner is a
// coordinated change owned elsewhere; see the brain-viz report).
//
//   node --test apps/web/components/brain/scrubberMath.test.mjs
//
// Requires Node ≥ 22.18 (or 22.6+ with --experimental-strip-types), which
// strips the types from the imported .ts module natively. The file is .mjs
// on purpose: apps/web/tsconfig.json compiles only **/*.ts(x), so a JS test
// stays invisible to `tsc --noEmit` / `next build` while remaining runnable.

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  AT_END_EPSILON,
  DEFAULT_SNAP_FRACTION,
  clampSeconds,
  isAtEnd,
  resolveSnapTolerance,
  secondsFromClientX,
  shouldAutoPauseAtEnd,
  snapToKeyframe,
} from "./scrubberMath.ts";

const close = (actual, expected, eps = 1e-9) =>
  assert.ok(
    Math.abs(actual - expected) <= eps,
    `expected ${actual} ≈ ${expected} (±${eps})`,
  );

describe("clampSeconds — bounds clamping", () => {
  test("passes a value already in range through unchanged", () => {
    assert.equal(clampSeconds(5, 10), 5);
  });
  test("floors below 0", () => {
    assert.equal(clampSeconds(-3, 10), 0);
  });
  test("caps at duration", () => {
    assert.equal(clampSeconds(12, 10), 10);
  });
  test("clamps to 0 for non-positive duration", () => {
    assert.equal(clampSeconds(5, 0), 0);
    assert.equal(clampSeconds(5, -4), 0);
  });
  test("keeps the exact endpoints", () => {
    assert.equal(clampSeconds(0, 10), 0);
    assert.equal(clampSeconds(10, 10), 10);
  });
});

describe("secondsFromClientX — seekFromClientX mapping", () => {
  // Track spans clientX 100..300 (left=100, width=200), duration 10s.
  const left = 100;
  const width = 200;
  const duration = 10;

  test("maps the midpoint to half the duration", () => {
    close(secondsFromClientX(200, left, width, duration), 5);
  });
  test("maps the left edge to 0", () => {
    close(secondsFromClientX(100, left, width, duration), 0);
  });
  test("maps the right edge to the full duration", () => {
    close(secondsFromClientX(300, left, width, duration), 10);
  });
  test("clamps a pointer left of the track to 0", () => {
    assert.equal(secondsFromClientX(40, left, width, duration), 0);
  });
  test("clamps a pointer right of the track to the duration", () => {
    assert.equal(secondsFromClientX(9999, left, width, duration), 10);
  });
  test("returns 0 (not NaN) for a zero-width track", () => {
    assert.equal(secondsFromClientX(150, left, 0, duration), 0);
  });
  test("returns 0 for non-positive duration", () => {
    assert.equal(secondsFromClientX(200, left, width, 0), 0);
  });
});

describe("resolveSnapTolerance — snap tolerance resolution", () => {
  test("honours an explicit tolerance", () => {
    assert.equal(resolveSnapTolerance(2.5, 10), 2.5);
  });
  test("floors a negative explicit tolerance at 0 (disables snap)", () => {
    assert.equal(resolveSnapTolerance(-1, 10), 0);
  });
  test("respects an explicit 0 even when duration is positive", () => {
    assert.equal(resolveSnapTolerance(0, 10), 0);
  });
  test("falls back to the default fraction of duration", () => {
    close(resolveSnapTolerance(undefined, 10), 10 * DEFAULT_SNAP_FRACTION);
  });
  test("falls back to 0 when duration is non-positive", () => {
    assert.equal(resolveSnapTolerance(undefined, 0), 0);
  });
});

describe("snapToKeyframe — snap behavior", () => {
  const keyframes = [0, 2, 5, 9];

  test("snaps to the nearest keyframe within tolerance", () => {
    assert.equal(snapToKeyframe(2.3, keyframes, 0.5), 2);
    assert.equal(snapToKeyframe(4.8, keyframes, 0.5), 5);
  });
  test("leaves the time unchanged when nothing is within tolerance", () => {
    assert.equal(snapToKeyframe(3.5, keyframes, 0.4), 3.5);
  });
  test("returns the input when there are no keyframes", () => {
    assert.equal(snapToKeyframe(3.5, [], 1), 3.5);
    assert.equal(snapToKeyframe(3.5, null, 1), 3.5);
    assert.equal(snapToKeyframe(3.5, undefined, 1), 3.5);
  });
  test("returns the input when tolerance is non-positive", () => {
    assert.equal(snapToKeyframe(2.01, keyframes, 0), 2.01);
    assert.equal(snapToKeyframe(2.01, keyframes, -1), 2.01);
  });
  test("snaps exactly on a keyframe to itself", () => {
    assert.equal(snapToKeyframe(5, keyframes, 0.5), 5);
  });
  test("resolves an equidistant tie to the last in-range keyframe", () => {
    // 3.5 is 1.5 from both 2 and 5; iteration order makes 5 win.
    assert.equal(snapToKeyframe(3.5, keyframes, 1.5), 5);
  });
});

describe("shouldAutoPauseAtEnd — play-loop auto-pause", () => {
  test("pauses when playing and the playhead reaches the end", () => {
    assert.equal(shouldAutoPauseAtEnd(true, 10, 10), true);
    assert.equal(shouldAutoPauseAtEnd(true, 11, 10), true);
  });
  test("does not pause before the end", () => {
    assert.equal(shouldAutoPauseAtEnd(true, 9.9, 10), false);
  });
  test("does not pause when not playing", () => {
    assert.equal(shouldAutoPauseAtEnd(false, 10, 10), false);
  });
  test("does not pause when duration is non-positive", () => {
    assert.equal(shouldAutoPauseAtEnd(true, 0, 0), false);
  });
});

describe("isAtEnd — end detection", () => {
  test("is true at the exact end", () => {
    assert.equal(isAtEnd(10, 10), true);
  });
  test("is true within epsilon of the end", () => {
    assert.equal(isAtEnd(10 - AT_END_EPSILON / 2, 10), true);
  });
  test("is false comfortably before the end", () => {
    assert.equal(isAtEnd(9.5, 10), false);
  });
  test("is false for non-positive duration", () => {
    assert.equal(isAtEnd(0, 0), false);
  });
  test("honours a custom epsilon", () => {
    assert.equal(isAtEnd(9.5, 10, 0.6), true);
    assert.equal(isAtEnd(9.5, 10, 0.4), false);
  });
});
