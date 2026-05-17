"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { REGION_IDS, type RegionId } from "@shared/types";

// Cinematic auto-pilot for the brain mesh. Twenty seconds of:
//   1. A single 360° camera azimuth sweep around the cortical surface.
//   2. The timeline playhead linearly traversing [0, totalDurationSec].
//   3. Eight 2-second region highlight slots that paint one region at a
//      time so a first-time viewer can match a region to its name.
//
// Implementation notes:
//   - rAF-driven. We do NOT use useFrame here because this hook needs to
//     live outside the Canvas (the Tour button lives in the DOM overlay
//     and needs the same state). The Canvas-side TourCameraDriver reads
//     `frame` from props and applies camera transforms inside its own
//     useFrame.
//   - `frame` is committed via setState so React re-renders propagate the
//     playheadSec / highlightedRegion downstream. rAF cadence is fine for
//     state writes here — every frame we write a fresh object; React's
//     bail-out doesn't kick in because the object identity changes, but
//     the consumers (CorticalSurface, useActivationFrame) are memoised
//     against their primitive inputs.
//   - We honour `prefers-reduced-motion` by zeroing the camera azimuth
//     delta and damping the highlight boost — see useTour callers for
//     the camera + colour-boost branches.
//
// Public API is intentionally minimal so the Canvas-internal driver and
// the DOM-side button can share the same state instance.

export interface TourFrame {
  // Camera spherical coordinates around the scene origin. Measured in
  // radians. Polar is held near the equator on purpose; the brain reads
  // best from a near-horizontal sweep. The TourCameraDriver consumes
  // these and writes the camera position; this hook does no Three.js
  // work itself.
  cameraAzimuth: number;
  cameraPolar: number;
  // Which region is currently in the spotlight, or null during the
  // intro/outro fade where no region is highlighted.
  highlightedRegion: RegionId | null;
  // The playhead time the parent should feed back into useActivationFrame
  // while the tour is active. Linearly traverses [0, totalDurationSec].
  playheadSec: number;
  // 0..1, only useful for callers that want to dim or pulse alongside
  // the tour. Currently consumed by TourCameraDriver to ease the camera
  // in and out at the boundaries so it doesn't snap on start/stop.
  progress: number;
}

export interface UseTourOptions {
  // Length of the source video / activation timeline, in seconds. Used
  // only to map tour-elapsed → playheadSec. If 0 the playhead stays at 0.
  totalDurationSec: number;
  // Caller's gate. When false `start()` is a no-op and `active` stays
  // false. The BrainMesh wires this to `hasKeyframes` so the tour can't
  // run in the Dashboard hero where there's nothing to scrub.
  enabled: boolean;
  // Total tour wall-clock duration. Default 20s. Exposed mostly so tests
  // / debug overlays can shorten it.
  durationSec?: number;
}

export interface UseTourResult {
  active: boolean;
  start: () => void;
  cancel: () => void;
  frame: TourFrame;
}

const DEFAULT_FRAME: TourFrame = {
  cameraAzimuth: 0,
  cameraPolar: Math.PI / 2,
  highlightedRegion: null,
  playheadSec: 0,
  progress: 0,
};

// Intro/outro slots where no region is spotlit. 2s in + 2s out = 4s,
// leaving 16s for the 8 regions × 2s each. Adjust together if you
// change `durationSec` — the maths are scale-invariant via fractions
// of the total elapsed.
const INTRO_FRAC = 0.1;  // 2s of 20s
const OUTRO_FRAC = 0.1;  // 2s of 20s

function easeInOutCubic(x: number): number {
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

export function useTour({
  totalDurationSec,
  enabled,
  durationSec = 20,
}: UseTourOptions): UseTourResult {
  const [active, setActive] = useState(false);
  const [frame, setFrame] = useState<TourFrame>(DEFAULT_FRAME);

  // We hold the rAF handle + start time in refs so cancel() can stop the
  // loop synchronously from arbitrary call-sites (escape key handler,
  // pointer down, parent unmount) without waiting for a re-render.
  const rafRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);

  // Latest options as a ref so the rAF loop can read them without
  // having to be torn down + rebuilt on every prop change.
  const optsRef = useRef({ totalDurationSec, durationSec });
  optsRef.current = { totalDurationSec, durationSec };

  const cancel = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setActive(false);
    setFrame(DEFAULT_FRAME);
  }, []);

  const start = useCallback(() => {
    if (!enabled) return;
    if (rafRef.current !== null) {
      // Already running — re-clicking the button is a "stop" upstream,
      // so this branch should be unreachable, but guard anyway.
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    startedAtRef.current = performance.now();
    setActive(true);

    const tick = (now: number) => {
      const { totalDurationSec: dur, durationSec: total } = optsRef.current;
      const elapsed = (now - startedAtRef.current) / 1000;
      const t = Math.max(0, Math.min(1, elapsed / total));

      // 360° azimuth sweep, easing in/out so the start and end don't
      // feel like a jump-cut even though we're returning to the origin.
      const cameraAzimuth = easeInOutCubic(t) * Math.PI * 2;

      // Slightly above the equator, fixed. Polar = π/2 is the equator
      // in our convention. Drop a few degrees so the top of the brain
      // is visible during the sweep.
      const cameraPolar = Math.PI / 2 - 0.18;

      // Region spotlight slot. The intro/outro windows return null so
      // the highlight ramps in/out rather than snapping on at t=0.
      let highlightedRegion: RegionId | null = null;
      if (t > INTRO_FRAC && t < 1 - OUTRO_FRAC) {
        const inner = (t - INTRO_FRAC) / (1 - INTRO_FRAC - OUTRO_FRAC);
        const idx = Math.min(
          REGION_IDS.length - 1,
          Math.floor(inner * REGION_IDS.length),
        );
        highlightedRegion = REGION_IDS[idx];
      }

      const playheadSec = dur > 0 ? t * dur : 0;

      setFrame({
        cameraAzimuth,
        cameraPolar,
        highlightedRegion,
        playheadSec,
        progress: t,
      });

      if (t >= 1) {
        rafRef.current = null;
        // Hold the active flag for one tick at the end so the camera
        // driver can ease back to the default view; cancel() then
        // clears everything. Equivalent to: schedule a microtask.
        setActive(false);
        return;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [enabled]);

  // Always clean up on unmount. The `cancel` identity is stable.
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  // If `enabled` flips to false while a tour is running (e.g. keyframes
  // unload), stop the tour rather than driving a now-meaningless playhead.
  useEffect(() => {
    if (!enabled && rafRef.current !== null) {
      cancel();
    }
  }, [enabled, cancel]);

  return useMemo(
    () => ({ active, start, cancel, frame }),
    [active, start, cancel, frame],
  );
}
