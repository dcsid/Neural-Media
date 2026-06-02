// Pure, framework-free timeline math for <TimelineScrubber>. Extracted so the
// seek / snap / clamp / auto-pause rules can be unit-tested without a DOM or a
// React renderer — TimelineScrubber is a thin interactive shell over these.
//
// Unit-tested in `scrubberMath.test.mjs` via Node's built-in test runner (no
// extra dependencies). See that file's header for how to run it.

/** Default keyframe-snap tolerance, as a fraction of the total duration. */
export const DEFAULT_SNAP_FRACTION = 0.04;

/** Slop (seconds) within which the playhead counts as "at the end". */
export const AT_END_EPSILON = 1e-3;

/**
 * Clamp a time (seconds) to the playable range [0, duration]. A non-positive
 * duration clamps everything to 0.
 */
export function clampSeconds(seconds: number, duration: number): number {
  return Math.max(0, Math.min(duration, seconds));
}

/**
 * Map a horizontal pointer position to a clamped, pre-snap seek time.
 * `trackLeft` / `trackWidth` come from the track element's bounding rect.
 * Returns 0 for a zero-width track or non-positive duration instead of the
 * NaN/Infinity a raw division would yield.
 */
export function secondsFromClientX(
  clientX: number,
  trackLeft: number,
  trackWidth: number,
  duration: number,
): number {
  if (trackWidth <= 0 || duration <= 0) return 0;
  const fraction = (clientX - trackLeft) / trackWidth;
  return clampSeconds(fraction * duration, duration);
}

/**
 * Resolve the effective snap tolerance (seconds). An explicit value wins
 * (floored at 0 so a negative never inverts the range check); otherwise fall
 * back to DEFAULT_SNAP_FRACTION of the duration. A result of 0 disables snap.
 */
export function resolveSnapTolerance(
  snapToleranceSec: number | undefined,
  duration: number,
): number {
  if (snapToleranceSec !== undefined) return Math.max(0, snapToleranceSec);
  return duration > 0 ? duration * DEFAULT_SNAP_FRACTION : 0;
}

/**
 * Snap a seek time to the nearest keyframe within `tolerance`. Returns the
 * input unchanged when there are no keyframes, the tolerance is non-positive,
 * or nothing is in range. Equidistant ties resolve to the last in-range
 * keyframe (keyframes are expected pre-sorted, but correctness does not
 * depend on it).
 */
export function snapToKeyframe(
  seconds: number,
  keyframes: ReadonlyArray<number> | null | undefined,
  tolerance: number,
): number {
  if (!keyframes || keyframes.length === 0 || tolerance <= 0) return seconds;
  let bestTime = seconds;
  let bestDelta = tolerance;
  for (const k of keyframes) {
    const delta = Math.abs(k - seconds);
    if (delta <= bestDelta) {
      bestDelta = delta;
      bestTime = k;
    }
  }
  return bestTime;
}

/** Whether an active play loop has reached the end and should auto-pause. */
export function shouldAutoPauseAtEnd(
  playing: boolean,
  playheadSec: number,
  duration: number,
): boolean {
  return playing && duration > 0 && playheadSec >= duration;
}

/** Whether the playhead is at (or within `epsilon` of) the end. */
export function isAtEnd(
  playheadSec: number,
  duration: number,
  epsilon: number = AT_END_EPSILON,
): boolean {
  return duration > 0 && playheadSec >= duration - epsilon;
}
